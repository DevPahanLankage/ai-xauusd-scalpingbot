from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .ai_models import AdvisoryResult
from .config import AIConfig
from .timestamps import TimestampError, canonical_timestamp


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
    estimated_spend_today_usd: float
    calls_this_week: int
    estimated_spend_this_week_usd: float

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


class SQLiteStateStore:
    """Atomic candidate reservation and conservative API usage accounting."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
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
                    output_tokens INTEGER,
                    reasoning_tokens INTEGER,
                    estimated_cost_usd REAL,
                    status TEXT NOT NULL,
                    latency_ms REAL,
                    FOREIGN KEY (symbol, completed_m1_time)
                        REFERENCES candidate_reservations(symbol, completed_m1_time)
                );
                CREATE INDEX IF NOT EXISTS idx_api_usage_timestamp
                    ON api_usage(timestamp);
                """
            )

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
                           COALESCE(SUM(estimated_cost_usd), 0.0) AS spend
                    FROM api_usage WHERE timestamp >= ?
                    """,
                    (today_start,),
                ).fetchone()
                weekly = connection.execute(
                    """
                    SELECT COUNT(*) AS calls,
                           COALESCE(SUM(estimated_cost_usd), 0.0) AS spend
                    FROM api_usage WHERE timestamp >= ?
                    """,
                    (week_start,),
                ).fetchone()
                if int(daily["calls"]) >= config.max_calls_per_day:
                    connection.rollback()
                    return AttemptReservation(
                        False, "daily_call_limit", None, normalized_symbol, candle_time
                    )
                if float(daily["spend"]) >= config.daily_spend_cap_usd:
                    connection.rollback()
                    return AttemptReservation(
                        False, "daily_spend_limit", None, normalized_symbol, candle_time
                    )
                if float(weekly["spend"]) >= config.weekly_spend_cap_usd:
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
                        (timestamp, symbol, completed_m1_time, model, status)
                    VALUES (?, ?, ?, ?, 'reserved')
                    """,
                    (reserved_at, normalized_symbol, candle_time, config.model),
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
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    UPDATE api_usage SET
                        input_tokens = ?, cached_input_tokens = ?,
                        output_tokens = ?, reasoning_tokens = ?,
                        estimated_cost_usd = ?, status = ?, latency_ms = ?
                    WHERE id = ?
                    """,
                    (
                        usage.input_tokens if usage else None,
                        usage.cached_input_tokens if usage else None,
                        usage.output_tokens if usage else None,
                        usage.reasoning_tokens if usage else None,
                        result.estimated_cost_usd,
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
        today_start, week_start = _period_starts(timestamp)
        with closing(self._connect()) as connection:
            daily = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS spend
                FROM api_usage WHERE timestamp >= ?
                """,
                (today_start,),
            ).fetchone()
            weekly = connection.execute(
                """
                SELECT COUNT(*) AS calls,
                       COALESCE(SUM(estimated_cost_usd), 0.0) AS spend
                FROM api_usage WHERE timestamp >= ?
                """,
                (week_start,),
            ).fetchone()
        return UsageSummary(
            calls_today=int(daily["calls"]),
            estimated_spend_today_usd=round(float(daily["spend"]), 8),
            calls_this_week=int(weekly["calls"]),
            estimated_spend_this_week_usd=round(float(weekly["spend"]), 8),
        )
