from __future__ import annotations

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
from .timestamps import TimestampError, canonical_timestamp


LEGACY_MIGRATION_RESERVE_USD = 0.05


@dataclass(frozen=True, slots=True)
class AttemptReservation:
    reserved: bool
    reason: str | None
    usage_id: int | None
    symbol: str
    completed_m1_time: str


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
                    budget_reserved_usd REAL NOT NULL DEFAULT 0.0,
                    budget_accounted_usd REAL NOT NULL DEFAULT 0.0,
                    status TEXT NOT NULL,
                    latency_ms REAL,
                    FOREIGN KEY (symbol, completed_m1_time)
                        REFERENCES candidate_reservations(symbol, completed_m1_time)
                );
                CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp
                    ON api_usage(timestamp);
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
                connection.commit()
                return AttemptReservation(
                    True, None, usage_id, normalized_symbol, candle_time
                )
            except Exception:
                connection.rollback()
                raise

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
