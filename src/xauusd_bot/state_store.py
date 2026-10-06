from __future__ import annotations

import json
import math
import sqlite3
import stat
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ai_models import AdvisoryResult
from .config import AIConfig
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot
from .timestamps import TimestampError, canonical_timestamp, parse_timestamp, seconds_between


LEGACY_MIGRATION_RESERVE_USD = 0.05


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    reserved: bool
    reason: str | None
    usage_id: int | None
    symbol: str
    completed_m1_time: str
    last_auto_call_time: str | None = None
    next_auto_call_time: str | None = None


@dataclass(frozen=True, slots=True)
class AutoSpacingStatus:
    last_call_time: str | None
    next_eligible_time: str | None
    seconds_since_last_call: float | None
    seconds_until_eligible: float


@dataclass(frozen=True, slots=True)
class UsageSummary:
    calls_today: int
    known_spend_today_usd: float
    budget_accounted_spend_today_usd: float
    calls_this_week: int
    known_spend_this_week_usd: float
    budget_accounted_spend_this_week_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReadOnlyStateInspection:
    state_available: bool
    usage: UsageSummary | None
    candidate_consumed: bool | None


class PersistentStateUnavailableError(RuntimeError):
    """Raised when an existing state database cannot be safely inspected."""


@dataclass(frozen=True, slots=True)
class BudgetStatus:
    can_reserve: bool
    rejection_reasons: tuple[str, ...]
    reserve_per_call_usd: float
    daily_spend_cap_usd: float
    weekly_spend_cap_usd: float
    max_calls_per_day: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("Persistent usage timestamps must be timezone-aware")
    return result.astimezone(timezone.utc)


def _period_starts(now: datetime) -> tuple[str, str]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = today - timedelta(days=today.weekday())
    return today.isoformat(), week.isoformat()


def evaluate_budget(summary: UsageSummary, config: AIConfig) -> BudgetStatus:
    """Evaluate visible budget state; the write transaction stays authoritative."""

    reserve = config.budget_reserve_per_call_usd
    reasons: list[str] = []
    if summary.calls_today >= config.max_calls_per_day:
        reasons.append("daily_call_limit")
    if summary.budget_accounted_spend_today_usd + reserve > config.daily_spend_cap_usd:
        reasons.append("daily_spend_limit")
    if (
        summary.budget_accounted_spend_this_week_usd + reserve
        > config.weekly_spend_cap_usd
    ):
        reasons.append("weekly_spend_limit")
    return BudgetStatus(
        can_reserve=not reasons,
        rejection_reasons=tuple(reasons),
        reserve_per_call_usd=reserve,
        daily_spend_cap_usd=config.daily_spend_cap_usd,
        weekly_spend_cap_usd=config.weekly_spend_cap_usd,
        max_calls_per_day=config.max_calls_per_day,
    )


def _paper_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decisions = {name: sum(row["decision"] == name for row in rows) for name in ("BUY", "SELL", "NO_TRADE")}
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
    completed = [
        row for row in rows
        if row["decision"] in {"BUY", "SELL"}
        and row["status"] in {"TP_HIT", "SL_HIT", "EXPIRED_OPEN"}
        and row["final_r"] is not None
    ]
    returns = [float(row["final_r"]) for row in completed]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    by_side: dict[str, dict[str, Any]] = {}
    for side in ("BUY", "SELL"):
        side_rows = [row for row in completed if row["decision"] == side]
        side_r = [float(row["final_r"]) for row in side_rows]
        by_side[side] = {
            "completed": len(side_r),
            "wins": sum(value > 0 for value in side_r),
            "average_r": sum(side_r) / len(side_r) if side_r else None,
        }
    confidence: dict[str, dict[str, Any]] = {}
    for label, lower, upper in (("0-59", 0, 59), ("60-79", 60, 79), ("80-100", 80, 100)):
        bucket = [row for row in completed if lower <= int(row["confidence"]) <= upper]
        bucket_r = [float(row["final_r"]) for row in bucket]
        confidence[label] = {
            "completed": len(bucket_r),
            "wins": sum(value > 0 for value in bucket_r),
            "average_r": sum(bucket_r) / len(bucket_r) if bucket_r else None,
        }
    def average(column: str) -> float | None:
        values = [float(row[column]) for row in completed if row[column] is not None]
        return sum(values) / len(values) if values else None
    holding_seconds: list[float] = []
    for row in completed:
        if row.get("fill_at") and row.get("close_at"):
            try:
                holding_seconds.append(
                    seconds_between(
                        parse_timestamp(str(row["close_at"])),
                        parse_timestamp(str(row["fill_at"])),
                    )
                )
            except TimestampError:
                pass
    return {
        "decision_counts": decisions,
        "status_counts": status_counts,
        "pending": status_counts.get("PENDING_ENTRY", 0),
        "open": status_counts.get("OPEN", 0),
        "completed": len(completed),
        "expired": status_counts.get("EXPIRED_UNFILLED", 0) + status_counts.get("EXPIRED_OPEN", 0),
        "ambiguous": status_counts.get("AMBIGUOUS", 0),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(completed) if completed else None,
        "average_r": sum(returns) / len(returns) if returns else None,
        "cumulative_r": sum(returns),
        "average_win_r": sum(wins) / len(wins) if wins else None,
        "average_loss_r": sum(losses) / len(losses) if losses else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "average_mfe_r": average("mfe_r"),
        "average_mae_r": average("mae_r"),
        "average_holding_seconds": (
            sum(holding_seconds) / len(holding_seconds) if holding_seconds else None
        ),
        "side_performance": by_side,
        "confidence_buckets": confidence,
    }


def _summary_from_connection(
    connection: sqlite3.Connection, now: datetime
) -> UsageSummary:
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    if "api_usage" not in tables:
        return UsageSummary(0, 0.0, 0.0, 0, 0.0, 0.0)
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(api_usage)")
    }
    budget_expression = (
        "COALESCE(SUM(budget_accounted_usd), 0.0)"
        if "budget_accounted_usd" in columns
        else "COALESCE(SUM(estimated_cost_usd), 0.0)"
    )
    today_start, week_start = _period_starts(now)

    def period(start: str) -> sqlite3.Row:
        return connection.execute(
            f"""
            SELECT COUNT(*) AS calls,
                   COALESCE(SUM(estimated_cost_usd), 0.0) AS known_spend,
                   {budget_expression} AS budget_spend
            FROM api_usage WHERE timestamp >= ?
            """,
            (start,),
        ).fetchone()

    daily = period(today_start)
    weekly = period(week_start)
    return UsageSummary(
        calls_today=int(daily["calls"]),
        known_spend_today_usd=round(float(daily["known_spend"]), 8),
        budget_accounted_spend_today_usd=round(float(daily["budget_spend"]), 8),
        calls_this_week=int(weekly["calls"]),
        known_spend_this_week_usd=round(float(weekly["known_spend"]), 8),
        budget_accounted_spend_this_week_usd=round(float(weekly["budget_spend"]), 8),
    )


class SQLiteStateStore:
    """Atomic candidate reservation and conservative API usage accounting."""

    def __init__(
        self,
        path: Path,
        legacy_reserve_usd: float = LEGACY_MIGRATION_RESERVE_USD,
    ) -> None:
        if not math.isfinite(legacy_reserve_usd) or legacy_reserve_usd < 0:
            raise ValueError("Legacy migration reserve must be finite and non-negative")
        self.path = path
        self._legacy_reserve_usd = legacy_reserve_usd
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _connect_read_only(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_reservations (
                    symbol TEXT NOT NULL,
                    completed_m1_time TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    result TEXT NOT NULL,
                    PRIMARY KEY (symbol, completed_m1_time)
                );

                CREATE TABLE IF NOT EXISTS api_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    completed_m1_time TEXT NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER,
                    cached_input_tokens INTEGER,
                    cache_write_tokens INTEGER,
                    output_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    estimated_cost_usd REAL,
                    advisory_json TEXT,
                    budget_reserved_usd REAL NOT NULL DEFAULT 0.0,
                    budget_accounted_usd REAL NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    latency_ms REAL,
                    FOREIGN KEY (symbol, completed_m1_time)
                        REFERENCES candidate_reservations(symbol, completed_m1_time)
                );
                CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp
                    ON api_usage(timestamp);

                CREATE TABLE IF NOT EXISTS paper_evaluations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    advisory_usage_id INTEGER NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    candidate_time TEXT NOT NULL,
                    advisory_completed_at TEXT NOT NULL,
                    tracking_start_market_time TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    confidence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    entry_price REAL,
                    entry_zone_low REAL,
                    entry_zone_high REAL,
                    stop_loss REAL,
                    take_profit REAL,
                    fill_at TEXT,
                    fill_price REAL,
                    close_at TEXT,
                    close_price REAL,
                    final_r REAL,
                    mfe_price REAL,
                    mae_price REAL,
                    mfe_r REAL,
                    mae_r REAL,
                    last_observed_market_time TEXT,
                    context_json TEXT NOT NULL,
                    notes TEXT,
                    FOREIGN KEY (advisory_usage_id) REFERENCES api_usage(id)
                );
                CREATE INDEX IF NOT EXISTS idx_paper_status
                    ON paper_evaluations(status);
                CREATE TABLE IF NOT EXISTS paper_checkpoints (
                    paper_id INTEGER NOT NULL,
                    checkpoint_minutes INTEGER NOT NULL,
                    observed_at TEXT NOT NULL,
                    bid REAL NOT NULL,
                    ask REAL NOT NULL,
                    midpoint REAL NOT NULL,
                    movement_points REAL,
                    favorable_r REAL,
                    adverse_r REAL,
                    PRIMARY KEY (paper_id, checkpoint_minutes),
                    FOREIGN KEY (paper_id) REFERENCES paper_evaluations(id)
                );

                CREATE TABLE IF NOT EXISTS research_candidates (
                    symbol TEXT NOT NULL,
                    completed_m1_time TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    trade_server_time TEXT NOT NULL,
                    bid REAL NOT NULL,
                    ask REAL NOT NULL,
                    spread_price REAL NOT NULL,
                    spread_points REAL,
                    direction_m1 TEXT NOT NULL,
                    direction_m5 TEXT NOT NULL,
                    directions_aligned INTEGER NOT NULL,
                    m1_latest_range_points REAL,
                    m1_average_range_points REAL,
                    m5_latest_range_points REAL,
                    m5_average_range_points REAL,
                    m1_spike_ratio REAL,
                    m5_spike_ratio REAL,
                    spread_to_m1_range_ratio REAL,
                    market_gate_eligible INTEGER NOT NULL,
                    market_rejection_reasons_json TEXT NOT NULL,
                    news_safe INTEGER NOT NULL,
                    news_rejection_reasons_json TEXT NOT NULL,
                    nearest_event_json TEXT,
                    candidate_hash TEXT,
                    disposition TEXT NOT NULL,
                    disposition_reason TEXT,
                    advisory_usage_id INTEGER,
                    decision TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, completed_m1_time),
                    FOREIGN KEY (advisory_usage_id) REFERENCES api_usage(id)
                );
                CREATE INDEX IF NOT EXISTS idx_research_candidates_captured
                    ON research_candidates(captured_at);

                CREATE TABLE IF NOT EXISTS auto_advisory_calls (
                    usage_id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    completed_m1_time TEXT NOT NULL,
                    reserved_at TEXT NOT NULL,
                    called_at TEXT,
                    FOREIGN KEY (usage_id) REFERENCES api_usage(id)
                );
                CREATE INDEX IF NOT EXISTS idx_auto_advisory_calls_called
                    ON auto_advisory_calls(called_at);
                """
            )
            # Serialize and atomically apply additive migrations. If the process
            # exits midway, closing the uncommitted connection rolls them back.
            connection.execute("BEGIN IMMEDIATE")
            columns = {
                str(row[1]) for row in connection.execute("PRAGMA table_info(api_usage)")
            }
            if "cache_write_tokens" not in columns:
                connection.execute(
                    "ALTER TABLE api_usage ADD COLUMN cache_write_tokens INTEGER"
                )
            if "advisory_json" not in columns:
                connection.execute(
                    "ALTER TABLE api_usage ADD COLUMN advisory_json TEXT"
                )
            if "budget_reserved_usd" not in columns:
                connection.execute(
                    "ALTER TABLE api_usage ADD COLUMN budget_reserved_usd "
                    "REAL NOT NULL DEFAULT 0.0"
                )
                connection.execute(
                    "UPDATE api_usage SET budget_reserved_usd = ?",
                    (self._legacy_reserve_usd,),
                )
            if "budget_accounted_usd" not in columns:
                connection.execute(
                    "ALTER TABLE api_usage ADD COLUMN budget_accounted_usd "
                    "REAL NOT NULL DEFAULT 0.0"
                )
                connection.execute(
                    """
                    UPDATE api_usage SET budget_accounted_usd =
                        CASE
                            WHEN estimated_cost_usd IS NOT NULL
                                 AND estimated_cost_usd >= 0
                                THEN estimated_cost_usd
                            ELSE ?
                        END
                    """,
                    (self._legacy_reserve_usd,),
                )
            connection.commit()

    def begin_ai_attempt(
        self,
        *,
        symbol: str,
        completed_m1_time: str,
        input_hash: str,
        config: AIConfig,
        now: datetime | None = None,
        automatic: bool = False,
        min_interval_minutes: float = 0.0,
    ) -> AttemptReservation:
        normalized_symbol = symbol.upper()
        try:
            candle_time = canonical_timestamp(completed_m1_time)
        except TimestampError:
            return AttemptReservation(
                False,
                "invalid_completed_m1_timestamp",
                None,
                normalized_symbol,
                completed_m1_time,
            )
        timestamp = _utc_now(now)
        today_start, week_start = _period_starts(timestamp)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                duplicate = connection.execute(
                    """
                    SELECT 1 FROM candidate_reservations
                    WHERE symbol = ? AND completed_m1_time = ?
                    """,
                    (normalized_symbol, candle_time),
                ).fetchone()
                if duplicate is not None:
                    connection.rollback()
                    return AttemptReservation(
                        False,
                        "completed_m1_already_consumed",
                        None,
                        normalized_symbol,
                        candle_time,
                    )

                last_auto: str | None = None
                next_auto: str | None = None
                if automatic and min_interval_minutes > 0:
                    last_row = connection.execute(
                        """
                        SELECT COALESCE(called_at, reserved_at) AS spacing_time
                        FROM auto_advisory_calls
                        ORDER BY COALESCE(called_at, reserved_at) DESC LIMIT 1
                        """
                    ).fetchone()
                    if last_row is not None:
                        last_time = parse_timestamp(str(last_row["spacing_time"]))
                        if last_time.tzinfo is None or last_time.utcoffset() is None:
                            connection.rollback()
                            return AttemptReservation(
                                False, "invalid_auto_advisory_timestamp", None,
                                normalized_symbol, candle_time,
                            )
                        last_time = last_time.astimezone(timezone.utc)
                        next_time = last_time + timedelta(minutes=min_interval_minutes)
                        last_auto = last_time.isoformat()
                        next_auto = next_time.isoformat()
                        if timestamp < next_time:
                            connection.rollback()
                            return AttemptReservation(
                                False, "rate_spacing_blocked", None,
                                normalized_symbol, candle_time, last_auto, next_auto,
                            )

                daily = connection.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(budget_accounted_usd), 0.0) AS spend
                    FROM api_usage WHERE timestamp >= ?
                    """,
                    (today_start,),
                ).fetchone()
                weekly = connection.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(budget_accounted_usd), 0.0) AS spend
                    FROM api_usage WHERE timestamp >= ?
                    """,
                    (week_start,),
                ).fetchone()
                reserve = config.budget_reserve_per_call_usd
                if int(daily["calls"]) >= config.max_calls_per_day:
                    connection.rollback()
                    return AttemptReservation(
                        False, "daily_call_limit", None, normalized_symbol, candle_time
                    )
                if float(daily["spend"]) + reserve > config.daily_spend_cap_usd:
                    connection.rollback()
                    return AttemptReservation(
                        False, "daily_spend_limit", None, normalized_symbol, candle_time
                    )
                if float(weekly["spend"]) + reserve > config.weekly_spend_cap_usd:
                    connection.rollback()
                    return AttemptReservation(
                        False, "weekly_spend_limit", None, normalized_symbol, candle_time
                    )

                reserved_at = timestamp.isoformat()
                connection.execute(
                    """
                    INSERT INTO candidate_reservations
                        (symbol, completed_m1_time, reserved_at, model, input_hash, result)
                    VALUES (?, ?, ?, ?, ?, 'reserved')
                    """,
                    (
                        normalized_symbol,
                        candle_time,
                        reserved_at,
                        config.model,
                        input_hash,
                    ),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO api_usage
                        (timestamp, symbol, completed_m1_time, model, status,
                         budget_reserved_usd, budget_accounted_usd)
                    VALUES (?, ?, ?, ?, 'reserved', ?, ?)
                    """,
                    (
                        reserved_at,
                        normalized_symbol,
                        candle_time,
                        config.model,
                        reserve,
                        reserve,
                    ),
                )
                usage_id = int(cursor.lastrowid)
                if automatic:
                    connection.execute(
                        """
                        INSERT INTO auto_advisory_calls
                            (usage_id, symbol, completed_m1_time, reserved_at, called_at)
                        VALUES (?, ?, ?, ?, NULL)
                        """,
                        (usage_id, normalized_symbol, candle_time, reserved_at),
                    )
                connection.commit()
                return AttemptReservation(
                    True, None, usage_id, normalized_symbol, candle_time,
                    last_auto, next_auto,
                )
            except Exception:
                connection.rollback()
                raise

    def mark_auto_call_started(
        self, usage_id: int, now: datetime | None = None
    ) -> str:
        """Mark the point immediately before the external automatic API call."""

        called_at = _utc_now(now).isoformat()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE auto_advisory_calls SET called_at = ?
                WHERE usage_id = ? AND called_at IS NULL
                """,
                (called_at, usage_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Automatic advisory reservation is missing or already started")
        return called_at

    def auto_spacing_status(
        self,
        min_interval_minutes: float,
        now: datetime | None = None,
    ) -> AutoSpacingStatus:
        timestamp = _utc_now(now)
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(called_at, reserved_at) AS spacing_time
                FROM auto_advisory_calls
                ORDER BY COALESCE(called_at, reserved_at) DESC LIMIT 1
                """
            ).fetchone()
        if row is None:
            return AutoSpacingStatus(None, None, None, 0.0)
        parsed = parse_timestamp(str(row["spacing_time"]))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Persisted automatic advisory timestamp is not timezone-aware")
        parsed = parsed.astimezone(timezone.utc)
        next_time = parsed + timedelta(minutes=max(0.0, min_interval_minutes))
        elapsed = (timestamp - parsed).total_seconds()
        return AutoSpacingStatus(
            parsed.isoformat(),
            next_time.isoformat(),
            max(0.0, elapsed),
            max(0.0, (next_time - timestamp).total_seconds()),
        )

    def record_research_candidate(
        self,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
        *,
        candidate_hash: str | None,
        disposition: str,
        reason: str | None = None,
    ) -> bool:
        """Insert one safe research row for a completed M1 candle."""

        if not market_gate.completed_m1_time:
            return False
        candle_time = canonical_timestamp(market_gate.completed_m1_time)
        symbol = snapshot.symbol.symbol.upper()
        metrics = market_gate.metrics
        nearest = (
            json.dumps(news_gate.nearest_event.to_dict(), allow_nan=False, separators=(",", ":"))
            if news_gate.nearest_event else None
        )
        values = (
            symbol, candle_time, snapshot.captured_at_utc, snapshot.trade_server_time,
            snapshot.symbol.bid, snapshot.symbol.ask, snapshot.metrics.spread_price,
            metrics.spread_points, market_gate.direction_m1, market_gate.direction_m5,
            int(market_gate.directions_aligned), metrics.m1_latest_completed_range_points,
            metrics.m1_baseline_range_points, metrics.m5_latest_completed_range_points,
            metrics.m5_baseline_range_points, metrics.m1_spike_ratio,
            metrics.m5_spike_ratio, metrics.spread_to_m1_range_ratio,
            int(market_gate.eligible_for_ai),
            json.dumps(market_gate.rejection_reasons, separators=(",", ":")),
            int(news_gate.safe_for_ai),
            json.dumps(news_gate.rejection_reasons, separators=(",", ":")),
            nearest, candidate_hash, disposition, reason,
            datetime.now(timezone.utc).isoformat(),
        )
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO research_candidates (
                    symbol, completed_m1_time, captured_at, trade_server_time,
                    bid, ask, spread_price, spread_points, direction_m1, direction_m5,
                    directions_aligned, m1_latest_range_points, m1_average_range_points,
                    m5_latest_range_points, m5_average_range_points, m1_spike_ratio,
                    m5_spike_ratio, spread_to_m1_range_ratio, market_gate_eligible,
                    market_rejection_reasons_json, news_safe,
                    news_rejection_reasons_json, nearest_event_json, candidate_hash,
                    disposition, disposition_reason, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                values,
            )
            return cursor.rowcount == 1

    def update_research_candidate(
        self,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
        *,
        candidate_hash: str | None,
        disposition: str,
        reason: str | None = None,
        advisory_usage_id: int | None = None,
        decision: str | None = None,
    ) -> None:
        if not market_gate.completed_m1_time:
            return
        metrics = market_gate.metrics
        nearest = (
            json.dumps(news_gate.nearest_event.to_dict(), allow_nan=False, separators=(",", ":"))
            if news_gate.nearest_event else None
        )
        with closing(self._connect()) as connection:
            connection.execute(
                """
                UPDATE research_candidates SET
                    captured_at=?, trade_server_time=?, bid=?, ask=?, spread_price=?,
                    spread_points=?, direction_m1=?, direction_m5=?, directions_aligned=?,
                    m1_latest_range_points=?, m1_average_range_points=?,
                    m5_latest_range_points=?, m5_average_range_points=?,
                    m1_spike_ratio=?, m5_spike_ratio=?, spread_to_m1_range_ratio=?,
                    market_gate_eligible=?, market_rejection_reasons_json=?,
                    news_safe=?, news_rejection_reasons_json=?, nearest_event_json=?,
                    candidate_hash=COALESCE(?, candidate_hash), disposition=?,
                    disposition_reason=?, advisory_usage_id=COALESCE(?, advisory_usage_id),
                    decision=COALESCE(?, decision), updated_at=?
                WHERE symbol=? AND completed_m1_time=?
                """,
                (
                    snapshot.captured_at_utc, snapshot.trade_server_time,
                    snapshot.symbol.bid, snapshot.symbol.ask, snapshot.metrics.spread_price,
                    metrics.spread_points, market_gate.direction_m1, market_gate.direction_m5,
                    int(market_gate.directions_aligned), metrics.m1_latest_completed_range_points,
                    metrics.m1_baseline_range_points, metrics.m5_latest_completed_range_points,
                    metrics.m5_baseline_range_points, metrics.m1_spike_ratio,
                    metrics.m5_spike_ratio, metrics.spread_to_m1_range_ratio,
                    int(market_gate.eligible_for_ai),
                    json.dumps(market_gate.rejection_reasons, separators=(",", ":")),
                    int(news_gate.safe_for_ai),
                    json.dumps(news_gate.rejection_reasons, separators=(",", ":")),
                    nearest, candidate_hash, disposition, reason, advisory_usage_id,
                    decision, datetime.now(timezone.utc).isoformat(),
                    snapshot.symbol.symbol.upper(),
                    canonical_timestamp(market_gate.completed_m1_time),
                ),
            )

    def research_candidate(self, symbol: str, completed_m1_time: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM research_candidates WHERE symbol=? AND completed_m1_time=?",
                (symbol.upper(), canonical_timestamp(completed_m1_time)),
            ).fetchone()
            return dict(row) if row is not None else None

    def finish_ai_attempt(
        self,
        reservation: AttemptReservation,
        result: AdvisoryResult,
    ) -> None:
        if not reservation.reserved or reservation.usage_id is None:
            raise ValueError("Cannot finish an unreserved AI attempt")
        usage = result.usage
        actual = result.estimated_cost_usd
        known_actual = actual is not None and math.isfinite(actual) and actual >= 0
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE api_usage SET
                        input_tokens = ?, cached_input_tokens = ?,
                        cache_write_tokens = ?, output_tokens = ?,
                        reasoning_tokens = ?, estimated_cost_usd = ?,
                        advisory_json = ?,
                        budget_accounted_usd = CASE
                            WHEN ? THEN ? ELSE budget_accounted_usd END,
                        status = ?, latency_ms = ?
                    WHERE id = ?
                    """,
                    (
                        usage.input_tokens if usage else None,
                        usage.cached_input_tokens if usage else None,
                        usage.cache_write_tokens if usage else None,
                        usage.output_tokens if usage else None,
                        usage.reasoning_tokens if usage else None,
                        actual if known_actual else None,
                        json.dumps(result.to_dict(), allow_nan=False, separators=(",", ":")),
                        known_actual,
                        actual if known_actual else None,
                        result.status,
                        result.latency_ms,
                        reservation.usage_id,
                    ),
                )
                connection.execute(
                    """
                    UPDATE candidate_reservations SET result = ?
                    WHERE symbol = ? AND completed_m1_time = ?
                    """,
                    (
                        f"{result.status}:{result.decision.decision.value}",
                        reservation.symbol,
                        reservation.completed_m1_time,
                    ),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def usage_summary(self, now: datetime | None = None) -> UsageSummary:
        timestamp = _utc_now(now)
        with closing(self._connect()) as connection:
            return _summary_from_connection(connection, timestamp)

    def create_paper_evaluation(self, values: dict[str, Any]) -> int:
        """Create the one paper record linked to a successful advisory."""

        columns = (
            "advisory_usage_id", "symbol", "candidate_time",
            "advisory_completed_at", "tracking_start_market_time", "decision",
            "confidence", "status", "entry_price", "entry_zone_low",
            "entry_zone_high", "stop_loss", "take_profit", "context_json", "notes",
        )
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    f"INSERT OR IGNORE INTO paper_evaluations ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})",
                    tuple(values.get(column) for column in columns),
                )
                row = connection.execute(
                    "SELECT id FROM paper_evaluations WHERE advisory_usage_id = ?",
                    (values["advisory_usage_id"],),
                ).fetchone()
                connection.commit()
                if row is None:
                    raise RuntimeError("Paper evaluation was not persisted")
                return int(row["id"])
            except Exception:
                connection.rollback()
                raise

    def active_paper_evaluations(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_evaluations
                WHERE status IN ('PENDING_ENTRY', 'OPEN', 'OBSERVING')
                ORDER BY id
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def paper_evaluation(self, paper_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM paper_evaluations WHERE id = ?", (paper_id,)
            ).fetchone()
            return dict(row) if row is not None else None

    def update_paper_evaluation(self, paper_id: int, values: dict[str, Any]) -> None:
        allowed = {
            "status", "fill_at", "fill_price", "close_at", "close_price",
            "final_r", "mfe_price", "mae_price", "mfe_r", "mae_r",
            "last_observed_market_time", "notes",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE paper_evaluations SET "
                    + ", ".join(f"{column} = ?" for column in updates)
                    + " WHERE id = ?",
                    (*updates.values(), paper_id),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def add_paper_checkpoint(self, values: dict[str, Any]) -> None:
        columns = (
            "paper_id", "checkpoint_minutes", "observed_at", "bid", "ask",
            "midpoint", "movement_points", "favorable_r", "adverse_r",
        )
        with closing(self._connect()) as connection:
            connection.execute(
                f"INSERT OR IGNORE INTO paper_checkpoints ({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                tuple(values.get(column) for column in columns),
            )

    def paper_dashboard(self, history_limit: int = 20) -> dict[str, Any]:
        """Return persisted paper state and aggregate statistics."""

        with closing(self._connect()) as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM paper_evaluations ORDER BY id DESC LIMIT ?",
                    (history_limit,),
                ).fetchall()
            ]
            checkpoints: dict[int, list[dict[str, Any]]] = {}
            if rows:
                ids = [int(row["id"]) for row in rows]
                placeholders = ",".join("?" for _ in ids)
                for checkpoint in connection.execute(
                    f"SELECT * FROM paper_checkpoints WHERE paper_id IN ({placeholders}) "
                    "ORDER BY checkpoint_minutes",
                    ids,
                ).fetchall():
                    checkpoints.setdefault(int(checkpoint["paper_id"]), []).append(
                        dict(checkpoint)
                    )
            all_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM paper_evaluations ORDER BY id"
                ).fetchall()
            ]
        for row in rows:
            row["checkpoints"] = checkpoints.get(int(row["id"]), [])
            try:
                row["context"] = json.loads(row.pop("context_json"))
            except (json.JSONDecodeError, TypeError):
                row["context"] = None
        return {"latest": rows[0] if rows else None, "history": rows, "stats": _paper_stats(all_rows)}

    @classmethod
    def usage_summary_read_only(
        cls, path: Path, now: datetime | None = None
    ) -> UsageSummary:
        """Inspect usage without creating a file, applying migrations, or writing WAL."""

        inspection = cls.inspect_read_only(path, now=now)
        if not inspection.state_available or inspection.usage is None:
            raise PersistentStateUnavailableError(
                "Existing persistent state cannot be safely inspected"
            )
        return inspection.usage

    @classmethod
    def candidate_consumed_read_only(
        cls, path: Path, symbol: str, completed_m1_time: str
    ) -> bool:
        """Check candidate state without creating or mutating the SQLite database."""

        inspection = cls.inspect_read_only(
            path,
            symbol=symbol,
            completed_m1_time=completed_m1_time,
        )
        if (
            not inspection.state_available
            or inspection.candidate_consumed is None
        ):
            raise PersistentStateUnavailableError(
                "Existing persistent state cannot be safely inspected"
            )
        return inspection.candidate_consumed

    @classmethod
    def inspect_read_only(
        cls,
        path: Path,
        *,
        symbol: str | None = None,
        completed_m1_time: str | None = None,
        now: datetime | None = None,
    ) -> ReadOnlyStateInspection:
        """Read usage and candidate state together without creating or repairing state."""

        timestamp = _utc_now(now)
        zero = UsageSummary(0, 0.0, 0.0, 0, 0.0, 0.0)
        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return ReadOnlyStateInspection(True, zero, False)
        except OSError:
            return ReadOnlyStateInspection(False, None, None)
        if not stat.S_ISREG(mode):
            return ReadOnlyStateInspection(False, None, None)
        try:
            candidate = (
                canonical_timestamp(completed_m1_time)
                if completed_m1_time is not None
                else None
            )
            with closing(cls._connect_read_only(path)) as connection:
                integrity = connection.execute("PRAGMA quick_check").fetchone()
                if integrity is None or str(integrity[0]).casefold() != "ok":
                    return ReadOnlyStateInspection(False, None, None)
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                if not {"api_usage", "candidate_reservations"} <= tables:
                    return ReadOnlyStateInspection(False, None, None)
                usage_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(api_usage)")
                }
                candidate_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(candidate_reservations)"
                    )
                }
                if not {"timestamp", "estimated_cost_usd"} <= usage_columns:
                    return ReadOnlyStateInspection(False, None, None)
                if not {"symbol", "completed_m1_time"} <= candidate_columns:
                    return ReadOnlyStateInspection(False, None, None)
                usage = _summary_from_connection(connection, timestamp)
                consumed = False
                if symbol is not None and candidate is not None:
                    consumed = (
                        connection.execute(
                            """
                            SELECT 1 FROM candidate_reservations
                            WHERE symbol = ? AND completed_m1_time = ?
                            """,
                            (symbol.upper(), candidate),
                        ).fetchone()
                        is not None
                    )
                return ReadOnlyStateInspection(True, usage, consumed)
        except (OSError, sqlite3.Error, TimestampError):
            return ReadOnlyStateInspection(False, None, None)

    @classmethod
    def last_advisory_read_only(cls, path: Path) -> dict[str, Any] | None:
        """Return the newest persisted advisory metadata without mutating state."""

        try:
            mode = path.stat().st_mode
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise PersistentStateUnavailableError(
                "Existing persistent state cannot be safely inspected"
            ) from exc
        if not stat.S_ISREG(mode):
            raise PersistentStateUnavailableError(
                "Existing persistent state cannot be safely inspected"
            )
        try:
            with closing(cls._connect_read_only(path)) as connection:
                columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(api_usage)")
                }
                required = {
                    "timestamp",
                    "completed_m1_time",
                    "model",
                    "status",
                    "input_tokens",
                    "cached_input_tokens",
                    "output_tokens",
                    "reasoning_tokens",
                    "estimated_cost_usd",
                    "latency_ms",
                }
                if not required <= columns:
                    raise PersistentStateUnavailableError(
                        "Persistent advisory history has an unsupported schema"
                    )
                cache_write = (
                    "u.cache_write_tokens" if "cache_write_tokens" in columns else "NULL"
                )
                advisory_json = (
                    "u.advisory_json" if "advisory_json" in columns else "NULL"
                )
                row = connection.execute(
                    f"""
                    SELECT u.timestamp, u.completed_m1_time, u.model, u.status,
                           u.input_tokens, u.cached_input_tokens,
                           {cache_write} AS cache_write_tokens,
                           u.output_tokens, u.reasoning_tokens,
                           u.estimated_cost_usd, u.latency_ms,
                           {advisory_json} AS advisory_json,
                           c.input_hash, c.result
                    FROM api_usage AS u
                    LEFT JOIN candidate_reservations AS c
                      ON c.symbol = u.symbol
                     AND c.completed_m1_time = u.completed_m1_time
                    ORDER BY u.id DESC LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    return None
                parsed: dict[str, Any] | None = None
                if row["advisory_json"]:
                    value = json.loads(str(row["advisory_json"]))
                    if isinstance(value, dict):
                        parsed = value
                decision = None
                result_text = str(row["result"] or "")
                if ":" in result_text:
                    decision = result_text.rsplit(":", 1)[-1]
                return {
                    "timestamp": row["timestamp"],
                    "candidate_time": row["completed_m1_time"],
                    "input_hash": row["input_hash"],
                    "model": row["model"],
                    "status": row["status"],
                    "decision": decision,
                    "usage": {
                        "input_tokens": row["input_tokens"],
                        "cached_input_tokens": row["cached_input_tokens"],
                        "cache_write_tokens": row["cache_write_tokens"],
                        "output_tokens": row["output_tokens"],
                        "reasoning_tokens": row["reasoning_tokens"],
                    },
                    "estimated_cost_usd": row["estimated_cost_usd"],
                    "latency_ms": row["latency_ms"],
                    "result": parsed,
                }
        except (json.JSONDecodeError, OSError, sqlite3.Error) as exc:
            raise PersistentStateUnavailableError(
                "Existing persistent state cannot be safely inspected"
            ) from exc
