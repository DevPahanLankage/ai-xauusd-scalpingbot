from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import statistics
from collections import Counter, defaultdict
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import AIConfig
from .timestamps import TimestampError, parse_timestamp, seconds_between


CHECKPOINT_MINUTES = (1, 3, 5, 10, 15, 30)
DIRECTIONAL_STATUSES = (
    "PENDING_ENTRY",
    "OPEN",
    "TP_HIT",
    "SL_HIT",
    "EXPIRED_UNFILLED",
    "EXPIRED_OPEN",
    "AMBIGUOUS",
    "INVALID",
    "CANCELLED_BY_SAFETY",
)
COMPLETED_STATUSES = frozenset({"TP_HIT", "SL_HIT", "EXPIRED_OPEN"})
DECISIONS = ("BUY", "SELL", "NO_TRADE")
THEME_STOP_WORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "because", "but", "by",
        "for", "from", "has", "have", "in", "into", "is", "it", "its",
        "of", "on", "or", "the", "this", "to", "trade", "with",
        "xauusd", "m1", "m5", "price", "setup", "market", "current", "would",
    }
)


class PaperReportError(RuntimeError):
    pass


def packaged_state_path(config: AIConfig, *, local_app_data: str | None = None) -> Path:
    """Resolve the same packaged runtime base used by the desktop executable."""

    path = config.state_db_path
    if path.is_absolute():
        return path.resolve()
    root = local_app_data if local_app_data is not None else os.getenv("LOCALAPPDATA", "")
    base = Path(root) / "XAUUSD-AI" if root else Path.cwd() / "data"
    return (base / path).resolve()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise PaperReportError(f"State database does not exist: {path}")
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except sqlite3.Error as exc:
        raise PaperReportError(f"Cannot open state database read-only: {path}") from exc


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _safe_json(value: Any) -> tuple[dict[str, Any] | None, bool]:
    if not value:
        return None, False
    try:
        parsed = json.loads(str(value))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, True
    return (parsed, False) if isinstance(parsed, dict) else (None, True)


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return sum(items) / len(items) if items else None


def _median(values: Iterable[float]) -> float | None:
    items = list(values)
    return statistics.median(items) if items else None


def _percent(count: int, total: int) -> float | None:
    return count / total if total else None


def _confidence_bucket(value: int) -> str:
    if value < 60:
        return "<60"
    if value < 70:
        return "60-69"
    if value < 80:
        return "70-79"
    if value < 90:
        return "80-89"
    return "90+"


def _spread_bucket(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= 20:
        return "<=20"
    if value <= 30:
        return "21-30"
    if value <= 40:
        return "31-40"
    if value <= 50:
        return "41-50"
    return ">50"


def _spike_bucket(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value < 0.75:
        return "<0.75"
    if value <= 1.0:
        return "0.75-1.00"
    if value <= 1.5:
        return "1.00-1.50"
    return ">1.50"


def _ratio_bucket(value: float | None) -> str:
    if value is None:
        return "UNKNOWN"
    if value <= 0.10:
        return "<=0.10"
    if value <= 0.20:
        return "0.10-0.20"
    if value <= 0.30:
        return "0.20-0.30"
    return ">0.30"


def _group_counts(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items(), key=lambda item: (-item[1], item[0])))


def _decision_data(row: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    payload, malformed = _safe_json(row.get("advisory_json"))
    if not payload:
        return None, malformed
    decision = payload.get("decision")
    return (decision, malformed) if isinstance(decision, dict) else (None, True)


def _context_data(row: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    return _safe_json(row.get("context_json"))


def _period_bounds(
    *, today: bool, report_date: date | None, now: datetime | None
) -> tuple[datetime | None, datetime]:
    current = now or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=timezone.utc)
    if report_date is not None:
        start = datetime.combine(report_date, datetime.min.time(), tzinfo=current.tzinfo)
        return start, start + timedelta(days=1) - timedelta(microseconds=1)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0) if today else None
    return start, current


def _in_period(value: Any, start: datetime | None, end: datetime) -> tuple[bool, bool]:
    try:
        parsed = parse_timestamp(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return False, True
        parsed = parsed.astimezone(end.tzinfo)
        return (start is None or parsed >= start) and parsed <= end, False
    except TimestampError:
        return False, True


def _trade_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = {status: sum(row.get("status") == status for row in rows) for status in DIRECTIONAL_STATUSES}
    completed = [
        row for row in rows
        if row.get("status") in COMPLETED_STATUSES and _finite(row.get("final_r")) is not None
    ]
    returns = [float(row["final_r"]) for row in completed]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    holding: list[float] = []
    for row in completed:
        if row.get("fill_at") and row.get("close_at"):
            try:
                holding.append(
                    seconds_between(
                        parse_timestamp(str(row["close_at"])),
                        parse_timestamp(str(row["fill_at"])),
                    )
                )
            except TimestampError:
                pass
    gross_loss = abs(sum(losses))
    return {
        "candidates": len(rows),
        "statuses": statuses,
        "completed": len(completed),
        "wins": len(wins),
        "losses": len(losses),
        "break_even": sum(value == 0 for value in returns),
        "win_rate": _percent(len(wins), len(completed)),
        "average_r": _mean(returns),
        "cumulative_r": sum(returns),
        "average_winning_r": _mean(wins),
        "average_losing_r": _mean(losses),
        "profit_factor_r": sum(wins) / gross_loss if gross_loss else None,
        "average_mfe_r": _mean(
            value for row in completed if (value := _finite(row.get("mfe_r"))) is not None
        ),
        "average_mae_r": _mean(
            value for row in completed if (value := _finite(row.get("mae_r"))) is not None
        ),
        "average_holding_seconds": _mean(holding),
    }


def _checkpoint_analysis(
    paper_rows: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    contexts: dict[int, dict[str, Any] | None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    no_trade_ids = {int(row["id"]) for row in paper_rows if row.get("decision") == "NO_TRADE"}
    values: dict[int, list[dict[str, float]]] = defaultdict(list)
    per_paper: dict[int, list[float]] = defaultdict(list)
    for row in checkpoints:
        paper_id = int(row["paper_id"])
        if paper_id not in no_trade_ids:
            continue
        minute = int(row["checkpoint_minutes"])
        point_value = _finite(row.get("movement_points"))
        context = contexts.get(paper_id) or {}
        point = _finite(context.get("point"))
        quote = context.get("quote") if isinstance(context.get("quote"), dict) else {}
        initial_mid = None
        if isinstance(quote, dict):
            bid, ask = _finite(quote.get("bid")), _finite(quote.get("ask"))
            if bid is not None and ask is not None:
                initial_mid = (bid + ask) / 2
        midpoint = _finite(row.get("midpoint"))
        price_move = midpoint - initial_mid if midpoint is not None and initial_mid is not None else None
        if point_value is None and price_move is not None and point:
            point_value = price_move / point
        if price_move is None and point_value is not None and point is not None:
            price_move = point_value * point
        if point_value is None or price_move is None:
            continue
        market_metrics = context.get("market_metrics") if isinstance(context.get("market_metrics"), dict) else {}
        average_points = _finite(market_metrics.get("m1_average_range_points")) if isinstance(market_metrics, dict) else None
        normalized = abs(point_value) / average_points if average_points and average_points > 0 else None
        values[minute].append(
            {
                "price": price_move,
                "points": point_value,
                "normalized": normalized if normalized is not None else math.nan,
            }
        )
        per_paper[paper_id].append(point_value)

    horizons: dict[str, Any] = {}
    for minute in CHECKPOINT_MINUTES:
        items = values.get(minute, [])
        prices = [item["price"] for item in items]
        points = [item["points"] for item in items]
        positive_prices = [value for value in prices if value > 0]
        negative_prices = [value for value in prices if value < 0]
        positive_points = [value for value in points if value > 0]
        negative_points = [value for value in points if value < 0]
        normalized = [item["normalized"] for item in items if math.isfinite(item["normalized"])]
        horizons[str(minute)] = {
            "observations": len(items),
            "average_absolute_price_movement": _mean(abs(value) for value in prices),
            "median_absolute_price_movement": _median(abs(value) for value in prices),
            "average_signed_price_movement": _mean(prices),
            "median_signed_price_movement": _median(prices),
            "average_signed_movement_points": _mean(points),
            "median_signed_movement_points": _median(points),
            "maximum_positive_price_movement": max(positive_prices) if positive_prices else None,
            "maximum_negative_price_movement": min(negative_prices) if negative_prices else None,
            "maximum_positive_movement_points": max(positive_points) if positive_points else None,
            "maximum_negative_movement_points": min(negative_points) if negative_points else None,
            "average_absolute_move_m1_range": _mean(normalized),
            "median_absolute_move_m1_range": _median(normalized),
            "direction_counts": {
                "UP": sum(value > 0 for value in points),
                "DOWN": sum(value < 0 for value in points),
                "FLAT": sum(value == 0 for value in points),
            },
        }

    upward = [max([0.0, *items]) for items in per_paper.values()]
    downward = [min([0.0, *items]) for items in per_paper.values()]
    ranges = [
        max([0.0, *items]) - min([0.0, *items])
        for items in per_paper.values()
        if items
    ]
    excursion = {
        "basis": "checkpoint-sampled midpoint movement; not intraperiod tick highs/lows",
        "observations": len(per_paper),
        "average_maximum_upward_excursion_points": _mean(upward),
        "average_maximum_downward_excursion_points": _mean(downward),
        "average_observed_high_low_range_points": _mean(ranges),
        "maximum_upward_excursion_points": max(upward) if upward else None,
        "maximum_downward_excursion_points": min(downward) if downward else None,
        "maximum_observed_high_low_range_points": max(ranges) if ranges else None,
    }
    return horizons, excursion


def _themes(
    decisions: list[dict[str, Any]], *, warning_only: bool = False
) -> list[dict[str, Any]]:
    documents: list[tuple[set[str], str]] = []
    for decision in decisions:
        warnings = decision.get("warnings") if isinstance(decision.get("warnings"), list) else []
        parts = (
            [str(value) for value in warnings]
            if warning_only
            else [
                str(decision.get("setup_summary") or ""),
                str(decision.get("invalidation_reason") or ""),
            ]
        )
        example = " | ".join(part.strip() for part in parts if part.strip())[:180]
        phrases: set[str] = set()
        for part in parts:
            tokens = re.findall(r"[a-z][a-z0-9'-]{2,}", part.lower())
            filtered = [token for token in tokens if token not in THEME_STOP_WORDS]
            for size in (2, 3, 4):
                phrases.update(
                    " ".join(filtered[index : index + size])
                    for index in range(len(filtered) - size + 1)
                )
        documents.append((phrases, example))
    counts: Counter[str] = Counter()
    for phrases, _ in documents:
        counts.update(phrases)
    ranked = sorted(
        ((phrase, count) for phrase, count in counts.items() if count >= 2),
        key=lambda item: (-item[1], -len(item[0].split()), item[0]),
    )
    chosen: list[tuple[str, int]] = []
    for phrase, count in ranked:
        words = set(phrase.split())
        if any(
            len(words & set(existing.split())) / len(words | set(existing.split())) >= 0.75
            and abs(count - existing_count) <= 2
            for existing, existing_count in chosen
        ):
            continue
        chosen.append((phrase, count))
        if len(chosen) == 10:
            break
    return [
        {
            "theme": phrase,
            "count": count,
            "percentage": _percent(count, len(documents)),
            "representative_examples": [
                example for phrases, example in documents if phrase in phrases and example
            ][:3],
        }
        for phrase, count in chosen
    ]


def _reason_terms(decisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for decision in decisions:
        text = " ".join(
            (
                str(decision.get("setup_summary") or ""),
                str(decision.get("invalidation_reason") or ""),
            )
        )
        tokens = {
            token
            for token in re.findall(r"[a-z][a-z0-9'-]{2,}", text.lower())
            if token not in THEME_STOP_WORDS
        }
        counts.update(tokens)
    return [
        {"term": term, "count": count, "percentage": _percent(count, len(decisions))}
        for term, count in counts.most_common(20)
    ]


def _context_patterns(
    paper_rows: list[dict[str, Any]], contexts: dict[int, dict[str, Any] | None]
) -> dict[str, Any]:
    values: dict[str, list[str]] = defaultdict(list)
    for row in paper_rows:
        if row.get("decision") != "NO_TRADE":
            continue
        context = contexts.get(int(row["id"])) or {}
        gate = context.get("market_gate") if isinstance(context.get("market_gate"), dict) else {}
        metrics = gate.get("metrics") if isinstance(gate.get("metrics"), dict) else {}
        values["direction"].append(
            f"{gate.get('direction_m1', 'UNKNOWN')} / {gate.get('direction_m5', 'UNKNOWN')}"
        )
        values["spread"].append(_spread_bucket(_finite(metrics.get("spread_points"))))
        values["m1_spike"].append(_spike_bucket(_finite(metrics.get("m1_spike_ratio"))))
        values["m5_spike"].append(_spike_bucket(_finite(metrics.get("m5_spike_ratio"))))
        values["spread_ratio"].append(
            _ratio_bucket(_finite(metrics.get("spread_to_m1_range_ratio")))
        )
        values["confidence"].append(_confidence_bucket(int(row.get("confidence") or 0)))
    return {name: _group_counts(items) for name, items in values.items()}


def _time_distribution(
    api_rows: list[dict[str, Any]],
    decisions_by_usage: dict[int, dict[str, Any]],
    context_by_usage: dict[int, dict[str, Any] | None],
) -> dict[str, Any]:
    groups: dict[str, list[tuple[dict[str, Any], float | None]]] = defaultdict(list)
    for row in api_rows:
        try:
            candidate = parse_timestamp(str(row.get("completed_m1_time")))
            hour = f"{candidate.hour:02d}:00"
        except TimestampError:
            hour = "UNKNOWN"
        decision = decisions_by_usage.get(int(row["id"])) or {}
        context = context_by_usage.get(int(row["id"])) or {}
        gate = context.get("market_gate") if isinstance(context.get("market_gate"), dict) else {}
        metrics = gate.get("metrics") if isinstance(gate.get("metrics"), dict) else {}
        groups[hour].append((decision, _finite(metrics.get("spread_points"))))
    result: dict[str, Any] = {}
    for hour, items in sorted(groups.items()):
        names = [str(decision.get("decision") or "UNKNOWN") for decision, _ in items]
        confidences = [
            value for decision, _ in items
            if (value := _finite(decision.get("confidence"))) is not None
        ]
        spreads = [spread for _, spread in items if spread is not None]
        result[hour] = {
            "calls": len(items),
            **{name: names.count(name) for name in DECISIONS},
            "average_confidence": _mean(confidences),
            "average_spread_points": _mean(spreads),
        }
    return result


def build_paper_report(
    path: Path,
    *,
    today: bool = False,
    report_date: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Analyze advisory and paper history without writing or initializing state."""

    resolved = path.resolve()
    start, end = _period_bounds(today=today, report_date=report_date, now=now)
    quality: dict[str, Any] = {
        "duplicate_advisory_candidates": 0,
        "duplicate_paper_usage_rows": 0,
        "duplicate_checkpoints": 0,
        "malformed_advisory_json": 0,
        "malformed_context_json": 0,
        "timestamp_problems": 0,
        "model_reported_timestamp_warnings": 0,
        "missing_paper_rows": 0,
        "missing_checkpoints": 0,
        "false_complete_missing_checkpoints": 0,
        "observation_gap_records": 0,
        "incomplete_no_trade_observations": 0,
        "advisory_context_mismatches": 0,
        "missing_context_fields": 0,
        "inconsistent_cost_rows": 0,
        "terminal_disposition_mismatches": 0,
        "sent_advisory_disposition_mismatches": 0,
        "legacy_overwritten_terminal_dispositions": 0,
        "missing_sanitized_advisory_inputs": 0,
        "legacy_missing_sanitized_advisory_inputs": 0,
        "missing_prompt_versions": 0,
        "legacy_missing_prompt_versions": 0,
        "warnings": [],
    }
    with closing(_connect_read_only(resolved)) as connection:
        tables = _tables(connection)
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or str(integrity[0]).casefold() != "ok":
            raise PaperReportError("SQLite quick_check did not return ok")
        api_rows: list[dict[str, Any]] = []
        if "api_usage" in tables:
            raw_api = [dict(row) for row in connection.execute("SELECT * FROM api_usage ORDER BY id")]
            for row in raw_api:
                included, invalid = _in_period(row.get("timestamp"), start, end)
                quality["timestamp_problems"] += int(invalid)
                if included:
                    api_rows.append(row)
        else:
            quality["warnings"].append("api_usage table is missing")

        usage_ids = {int(row["id"]) for row in api_rows if row.get("id") is not None}
        paper_rows: list[dict[str, Any]] = []
        if "paper_evaluations" in tables:
            paper_columns = _columns(connection, "paper_evaluations")
            if {"id", "advisory_usage_id"} <= paper_columns:
                paper_rows = [
                    dict(row)
                    for row in connection.execute("SELECT * FROM paper_evaluations ORDER BY id")
                    if int(row["advisory_usage_id"]) in usage_ids
                ]
        else:
            quality["warnings"].append("paper_evaluations table is missing (older database)")
        paper_ids = {int(row["id"]) for row in paper_rows}
        checkpoints: list[dict[str, Any]] = []
        if "paper_checkpoints" in tables and paper_ids:
            checkpoints = [
                dict(row)
                for row in connection.execute("SELECT * FROM paper_checkpoints ORDER BY paper_id, checkpoint_minutes")
                if int(row["paper_id"]) in paper_ids
            ]
        elif "paper_checkpoints" not in tables:
            quality["warnings"].append("paper_checkpoints table is missing (older database)")

        reservations = 0
        if "candidate_reservations" in tables:
            keys = {(str(row.get("symbol", "")).upper(), str(row.get("completed_m1_time", ""))) for row in api_rows}
            reservations = sum(
                (str(row["symbol"]).upper(), str(row["completed_m1_time"])) in keys
                for row in connection.execute("SELECT symbol, completed_m1_time FROM candidate_reservations")
            )

        research_rows: list[dict[str, Any]] = []
        if "research_candidates" in tables:
            for raw in connection.execute(
                "SELECT * FROM research_candidates ORDER BY completed_m1_time"
            ):
                row = dict(raw)
                included, invalid = _in_period(row.get("captured_at"), start, end)
                quality["timestamp_problems"] += int(invalid)
                if included:
                    research_rows.append(row)
        else:
            quality["warnings"].append(
                "research_candidates table is missing (older database)"
            )

        observation_rows: list[dict[str, Any]] = []
        if "research_candidate_observations" in tables:
            observation_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM research_candidate_observations"
                )
            ]

        advisory_input_rows: dict[int, dict[str, Any]] = {}
        legacy_input_max_id = max(usage_ids, default=0)
        if "advisory_inputs" in tables:
            advisory_input_rows = {
                int(row["usage_id"]): dict(row)
                for row in connection.execute("SELECT * FROM advisory_inputs")
                if int(row["usage_id"]) in usage_ids
            }
            if "state_metadata" in tables:
                boundary = connection.execute(
                    """
                    SELECT value FROM state_metadata
                    WHERE key='advisory_input_legacy_max_usage_id'
                    """
                ).fetchone()
                if boundary is not None:
                    try:
                        legacy_input_max_id = int(boundary["value"])
                    except (TypeError, ValueError):
                        quality["timestamp_problems"] += 1

    decisions_by_usage: dict[int, dict[str, Any]] = {}
    for row in api_rows:
        decision, malformed = _decision_data(row)
        quality["malformed_advisory_json"] += int(malformed)
        if decision:
            decisions_by_usage[int(row["id"])] = decision

    paper_by_usage = defaultdict(list)
    contexts: dict[int, dict[str, Any] | None] = {}
    context_by_usage: dict[int, dict[str, Any] | None] = {}
    for row in paper_rows:
        usage_id, paper_id = int(row["advisory_usage_id"]), int(row["id"])
        paper_by_usage[usage_id].append(row)
        context, malformed = _context_data(row)
        quality["malformed_context_json"] += int(malformed)
        contexts[paper_id] = context
        context_by_usage[usage_id] = context
        if row.get("notes") == "observation_gap_exceeded":
            quality["observation_gap_records"] += 1
        if row.get("decision") == "NO_TRADE" and row.get("status") in {
            "OBSERVING", "OBSERVATION_INCOMPLETE"
        }:
            quality["incomplete_no_trade_observations"] += 1
        required_context = (
            isinstance(context, dict)
            and isinstance(context.get("market_gate"), dict)
            and isinstance(context.get("market_metrics"), dict)
            and isinstance(context.get("quote"), dict)
        )
        if not required_context:
            quality["missing_context_fields"] += 1
        if isinstance(context, dict):
            if str(context.get("candidate_time")) != str(row.get("candidate_time")):
                quality["advisory_context_mismatches"] += 1
            decision = decisions_by_usage.get(usage_id)
            if decision and decision.get("decision") != row.get("decision"):
                quality["advisory_context_mismatches"] += 1

    quality["duplicate_paper_usage_rows"] = sum(max(0, len(rows) - 1) for rows in paper_by_usage.values())
    successful_ids = {int(row["id"]) for row in api_rows if row.get("status") == "success"}
    quality["missing_paper_rows"] = sum(usage_id not in paper_by_usage for usage_id in successful_ids)
    candidate_counts = Counter(
        (str(row.get("symbol", "")).upper(), str(row.get("completed_m1_time", "")))
        for row in api_rows
    )
    quality["duplicate_advisory_candidates"] = sum(max(0, count - 1) for count in candidate_counts.values())
    checkpoint_counts = Counter(
        (int(row["paper_id"]), int(row["checkpoint_minutes"])) for row in checkpoints
    )
    quality["duplicate_checkpoints"] = sum(max(0, count - 1) for count in checkpoint_counts.values())
    checkpoint_keys = set(checkpoint_counts)
    for row in paper_rows:
        if row.get("decision") == "NO_TRADE" and row.get("status") == "OBSERVATION_COMPLETE":
            missing = sum(
                (int(row["id"]), minute) not in checkpoint_keys for minute in CHECKPOINT_MINUTES
            )
            quality["missing_checkpoints"] += missing
            quality["false_complete_missing_checkpoints"] += missing

    missing_input_ids = usage_ids - set(advisory_input_rows)
    for usage_id in missing_input_ids:
        if usage_id <= legacy_input_max_id:
            quality["legacy_missing_sanitized_advisory_inputs"] += 1
            quality["legacy_missing_prompt_versions"] += 1
        else:
            quality["missing_sanitized_advisory_inputs"] += 1
            quality["missing_prompt_versions"] += 1
    quality["missing_prompt_versions"] += sum(
        not str(row.get("prompt_version") or "").strip()
        or not str(row.get("system_prompt_sha256") or "").strip()
        for row in advisory_input_rows.values()
    )

    for row in research_rows:
        linked = row.get("advisory_usage_id") is not None
        terminal = row.get("terminal_disposition")
        if terminal:
            if row.get("disposition") != terminal:
                quality["terminal_disposition_mismatches"] += 1
            if linked and terminal not in {"SENT_TO_AI", "AI_ERROR"}:
                quality["sent_advisory_disposition_mismatches"] += 1
        elif linked and row.get("decision") in DECISIONS:
            if row.get("disposition") not in {"SENT_TO_AI", "AI_ERROR"}:
                quality["legacy_overwritten_terminal_dispositions"] += 1

    if quality["legacy_missing_sanitized_advisory_inputs"]:
        quality["warnings"].append(
            "Legacy advisories predate exact sanitized-input and prompt-version persistence."
        )
    if quality["legacy_overwritten_terminal_dispositions"]:
        quality["warnings"].append(
            "Legacy candidate dispositions were overwritten after linked AI calls; "
            "advisory linkage is used as terminal-event evidence."
        )
    for row in api_rows:
        cost = row.get("estimated_cost_usd")
        numeric = _finite(cost)
        if (cost is not None and numeric is None) or (numeric is not None and numeric < 0):
            quality["inconsistent_cost_rows"] += 1
        if row.get("status") == "success" and numeric is None:
            quality["inconsistent_cost_rows"] += 1

    decision_names = [
        str(decision.get("decision")) for decision in decisions_by_usage.values()
        if decision.get("decision") in DECISIONS
    ]
    decision_summary = {
        name: {"count": decision_names.count(name), "percentage": _percent(decision_names.count(name), len(decision_names))}
        for name in DECISIONS
    }
    confidences: dict[str, list[float]] = {name: [] for name in DECISIONS}
    buckets = {
        label: {"advisories": 0, "BUY": 0, "SELL": 0, "NO_TRADE": 0}
        for label in ("<60", "60-69", "70-79", "80-89", "90+")
    }
    for decision in decisions_by_usage.values():
        name = str(decision.get("decision"))
        confidence = _finite(decision.get("confidence"))
        if name not in DECISIONS or confidence is None:
            continue
        confidences[name].append(confidence)
        bucket = buckets[_confidence_bucket(int(confidence))]
        bucket["advisories"] += 1
        bucket[name] += 1

    directional = [row for row in paper_rows if row.get("decision") in {"BUY", "SELL"}]
    no_trade_decisions = [
        decisions_by_usage[int(row["advisory_usage_id"])]
        for row in paper_rows
        if row.get("decision") == "NO_TRADE"
        and int(row["advisory_usage_id"]) in decisions_by_usage
    ]
    quality["model_reported_timestamp_warnings"] = sum(
        any(
            "timestamp" in str(warning).lower() or "negative tick age" in str(warning).lower()
            for warning in (
                decision.get("warnings")
                if isinstance(decision.get("warnings"), list)
                else []
            )
        )
        for decision in no_trade_decisions
    )
    if quality["model_reported_timestamp_warnings"]:
        quality["warnings"].append(
            "AI text flagged a minor tick/server timestamp skew; persisted timestamps remain parseable and comparable."
        )
    horizons, excursions = _checkpoint_analysis(paper_rows, checkpoints, contexts)
    known_costs = [
        value for row in api_rows
        if (value := _finite(row.get("estimated_cost_usd"))) is not None
    ]
    latencies = [
        value for row in api_rows if (value := _finite(row.get("latency_ms"))) is not None
    ]
    all_confidences = [value for values in confidences.values() for value in values]
    first_timestamp = api_rows[0].get("timestamp") if api_rows else None
    last_timestamp = api_rows[-1].get("timestamp") if api_rows else None
    eligible_persisted = 0
    for context in context_by_usage.values():
        gate = (context or {}).get("market_gate")
        if isinstance(gate, dict) and gate.get("eligible_for_ai"):
            eligible_persisted += 1
    server_hours = _time_distribution(api_rows, decisions_by_usage, context_by_usage)
    candidate_hours = Counter()
    for row in research_rows:
        try:
            candidate = parse_timestamp(str(row.get("completed_m1_time")))
            candidate_hours[f"{candidate.hour:02d}:00"] += 1
        except TimestampError:
            candidate_hours["UNKNOWN"] += 1
    for hour in sorted(set(server_hours) | set(candidate_hours)):
        server_hours.setdefault(
            hour,
            {
                "calls": 0, "BUY": 0, "SELL": 0, "NO_TRADE": 0,
                "average_confidence": None, "average_spread_points": None,
            },
        )["candidates"] = candidate_hours.get(hour, 0)
    if len(api_rows) >= 2 and len(server_hours) == 1:
        quality["warnings"].append(
            "All advisories fall in one MT5 server-time hour; overlapping horizons are not independent samples."
        )
    quality["trustworthy"] = not any(
        quality[key]
        for key in (
            "duplicate_advisory_candidates", "duplicate_paper_usage_rows",
            "duplicate_checkpoints", "malformed_advisory_json", "malformed_context_json",
            "timestamp_problems", "missing_paper_rows", "missing_checkpoints",
            "observation_gap_records", "incomplete_no_trade_observations",
            "advisory_context_mismatches", "missing_context_fields", "inconsistent_cost_rows",
            "terminal_disposition_mismatches",
            "sent_advisory_disposition_mismatches",
            "missing_sanitized_advisory_inputs",
            "missing_prompt_versions",
        )
    )
    patterns = _context_patterns(paper_rows, contexts)
    patterns["market_regime"] = _group_counts(
        str(decision.get("market_regime") or "UNKNOWN") for decision in no_trade_decisions
    )
    def terminal_disposition(row: dict[str, Any]) -> str:
        persisted = row.get("terminal_disposition")
        if persisted:
            return str(persisted)
        if row.get("advisory_usage_id") is not None and row.get("decision") in DECISIONS:
            # Backward-compatible evidence for legacy rows affected by the old
            # same-candle overwrite bug. This is reporting only; no row is repaired.
            return "SENT_TO_AI"
        return str(row.get("disposition"))

    disposition_counts = Counter(terminal_disposition(row) for row in research_rows)
    def candidate_market_eligible(row: dict[str, Any]) -> bool:
        usage_id = row.get("advisory_usage_id")
        context = context_by_usage.get(int(usage_id)) if usage_id is not None else None
        gate = (context or {}).get("market_gate")
        if isinstance(gate, dict) and "eligible_for_ai" in gate:
            return bool(gate["eligible_for_ai"])
        return bool(row.get("market_gate_eligible"))

    def candidate_news_safe(row: dict[str, Any]) -> bool:
        usage_id = row.get("advisory_usage_id")
        context = context_by_usage.get(int(usage_id)) if usage_id is not None else None
        gate = (context or {}).get("news_gate")
        if isinstance(gate, dict) and "safe_for_ai" in gate:
            return bool(gate["safe_for_ai"])
        return bool(row.get("news_safe"))

    market_eligible = sum(candidate_market_eligible(row) for row in research_rows)
    market_rejected = len(research_rows) - market_eligible
    news_blocked = sum(
        candidate_market_eligible(row) and not candidate_news_safe(row)
        for row in research_rows
    )
    fully_eligible = sum(
        candidate_market_eligible(row) and candidate_news_safe(row)
        for row in research_rows
    )
    sent = disposition_counts["SENT_TO_AI"] + disposition_counts["AI_ERROR"]
    sent_decisions = [
        str(row.get("decision")) for row in research_rows
        if terminal_disposition(row) == "SENT_TO_AI" and row.get("decision") in DECISIONS
    ]
    research_keys = {
        (str(row.get("symbol", "")).upper(), str(row.get("completed_m1_time", "")))
        for row in research_rows
    }
    latest_observation_counts = Counter(
        str(row.get("observation_disposition"))
        for row in observation_rows
        if (
            str(row.get("symbol", "")).upper(),
            str(row.get("completed_m1_time", "")),
        )
        in research_keys
    )
    directional_sent = sum(value in {"BUY", "SELL"} for value in sent_decisions)
    return {
        "schema_version": "1.0",
        "report_period": {
            "scope": report_date.isoformat() if report_date else "today" if today else "all",
            "timezone": str(end.tzinfo),
            "start": start.isoformat() if start else first_timestamp,
            "end": end.isoformat(),
            "first_advisory": first_timestamp,
            "last_advisory": last_timestamp,
            "database_path": str(resolved),
            "advisories": len(api_rows),
        },
        "api_usage": {
            "calls": len(api_rows),
            "known_cost_calls": len(known_costs),
            "total_known_spend_usd": sum(known_costs),
            "average_cost_per_call_usd": sum(known_costs) / len(api_rows) if api_rows else None,
            "average_latency_ms": _mean(latencies),
            "total_input_tokens": sum(int(row.get("input_tokens") or 0) for row in api_rows),
            "total_output_tokens": sum(int(row.get("output_tokens") or 0) for row in api_rows),
            "total_cache_write_tokens": sum(int(row.get("cache_write_tokens") or 0) for row in api_rows),
            "total_cached_input_tokens": sum(int(row.get("cached_input_tokens") or 0) for row in api_rows),
        },
        "decisions": decision_summary,
        "confidence": {
            "average_overall": _mean(all_confidences),
            **{f"average_{name.lower()}": _mean(confidences[name]) for name in DECISIONS},
            "buckets": buckets,
        },
        "paper_performance": {
            "overall": _trade_stats(directional),
            "BUY": _trade_stats([row for row in directional if row.get("decision") == "BUY"]),
            "SELL": _trade_stats([row for row in directional if row.get("decision") == "SELL"]),
        },
        "no_trade": {
            "count": len(no_trade_decisions),
            "checkpoints": horizons,
            "excursions": excursions,
        },
        "market_context_patterns": patterns,
        "ai_reason_themes": _themes(no_trade_decisions),
        "ai_reason_term_frequencies": _reason_terms(no_trade_decisions),
        "ai_warning_themes": _themes(no_trade_decisions, warning_only=True),
        "candidate_efficiency": {
            "openai_calls": len(api_rows),
            "directional_decisions": len(directional),
            "directional_percentage": _percent(len(directional), len(decision_names)),
            "no_trade_decisions": decision_names.count("NO_TRADE"),
            "no_trade_percentage": _percent(decision_names.count("NO_TRADE"), len(decision_names)),
            "candidate_reservations": reservations,
            "eligible_contexts_persisted_for_sent_calls": eligible_persisted,
            "eligible_deterministic_candidates_total": fully_eligible if "research_candidates" in tables else None,
            "rejected_before_gpt": market_rejected if "research_candidates" in tables else None,
            "duplicate_or_consumed_attempts": disposition_counts["DUPLICATE"] if "research_candidates" in tables else None,
            "limitations": [] if "research_candidates" in tables else [
                "This older database predates deterministic candidate persistence.",
            ],
        },
        "deterministic_candidates": {
            "available": "research_candidates" in tables,
            "total": len(research_rows),
            "market_gate_eligible": market_eligible,
            "market_gate_rejected": market_rejected,
            "news_blocked": news_blocked,
            "spacing_blocked": disposition_counts["RATE_SPACING_BLOCKED"],
            "duplicate": disposition_counts["DUPLICATE"],
            "budget_blocked": disposition_counts["BUDGET_BLOCKED"],
            "sent_to_ai": sent,
            "dispositions": dict(sorted(disposition_counts.items())),
            "latest_observation_dispositions": dict(
                sorted(latest_observation_counts.items())
            ),
        },
        "advisory_input_replay": {
            "replayable": len(advisory_input_rows),
            "legacy_unavailable": quality[
                "legacy_missing_sanitized_advisory_inputs"
            ],
            "missing_new_schema": quality["missing_sanitized_advisory_inputs"],
            "prompt_versioned": sum(
                bool(str(row.get("prompt_version") or "").strip())
                and bool(str(row.get("system_prompt_sha256") or "").strip())
                for row in advisory_input_rows.values()
            ),
        },
        "ai_conversion": {
            "eligible_deterministic_candidates": fully_eligible,
            "sent_to_gpt": sent,
            "BUY": sent_decisions.count("BUY"),
            "SELL": sent_decisions.count("SELL"),
            "NO_TRADE": sent_decisions.count("NO_TRADE"),
            "percentage_eligible_sent": _percent(sent, fully_eligible),
            "percentage_directional": _percent(directional_sent, len(sent_decisions)),
        },
        "time_distribution_server_hour": server_hours,
        "data_quality": quality,
        "baseline_readiness": {
            "sent_candidate_context_available": eligible_persisted == len(api_rows) if api_rows else False,
            "gpt_selection_available": len(decisions_by_usage) == len(api_rows),
            "paper_outcomes_available": len(paper_rows) == len(successful_ids),
            "unbiased_all_eligible_candidate_baseline_available": "research_candidates" in tables,
            "missing_for_future_unbiased_comparison": [] if "research_candidates" in tables else [
                "A durable record of every eligible MarketGate candidate, including candidates not sent to GPT.",
                "Durable pre-GPT rejection and duplicate-attempt counters keyed by candidate.",
            ],
        },
    }


def _number(value: Any, digits: int = 2, suffix: str = "") -> str:
    numeric = _finite(value)
    return "N/A" if numeric is None else f"{numeric:.{digits}f}{suffix}"


def _percentage(value: Any) -> str:
    numeric = _finite(value)
    return "N/A" if numeric is None else f"{numeric * 100:.1f}%"


def _console_text(value: Any) -> str:
    return (
        str(value)
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2212", "-")
        .encode("ascii", errors="replace")
        .decode("ascii")
    )


def paper_report_to_human(report: dict[str, Any]) -> str:
    period = report["report_period"]
    usage = report["api_usage"]
    decisions = report["decisions"]
    confidence = report["confidence"]
    paper = report["paper_performance"]
    no_trade = report["no_trade"]
    lines = [
        "XAUUSD Advisory & Paper Analytics (read-only)",
        "=" * 48,
        "",
        "REPORT PERIOD",
        f"Database             : {period['database_path']}",
        f"Scope / timezone     : {period['scope']} / {period['timezone']}",
        f"Start / end          : {period['start']} / {period['end']}",
        f"First / last record  : {period['first_advisory']} / {period['last_advisory']}",
        f"Advisories           : {period['advisories']}",
        "",
        "API USAGE",
        f"Calls / known costs  : {usage['calls']} / {usage['known_cost_calls']}",
        f"Spend / avg call USD : {_number(usage['total_known_spend_usd'], 6)} / {_number(usage['average_cost_per_call_usd'], 6)}",
        f"Average latency      : {_number(usage['average_latency_ms'], 1, ' ms')}",
        f"Input/output tokens  : {usage['total_input_tokens']} / {usage['total_output_tokens']}",
        f"Cache write/read     : {usage['total_cache_write_tokens']} / {usage['total_cached_input_tokens']}",
        "",
        "DECISIONS",
    ]
    for name in DECISIONS:
        lines.append(
            f"{name:<20}: {decisions[name]['count']:>3} ({_percentage(decisions[name]['percentage'])})"
        )
    lines.extend(
        [
            "",
            "CONFIDENCE",
            f"Overall / BUY        : {_number(confidence['average_overall'], 1)} / {_number(confidence['average_buy'], 1)}",
            f"SELL / NO_TRADE      : {_number(confidence['average_sell'], 1)} / {_number(confidence['average_no_trade'], 1)}",
        ]
    )
    for label, bucket in confidence["buckets"].items():
        lines.append(
            f"  {label:<6} n={bucket['advisories']:<3} BUY={bucket['BUY']:<3} SELL={bucket['SELL']:<3} NO_TRADE={bucket['NO_TRADE']:<3}"
        )
    lines.extend(["", "BUY / SELL PAPER PERFORMANCE"])
    for label in ("overall", "BUY", "SELL"):
        value = paper[label]
        lines.append(
            f"{label:<8} candidates={value['candidates']} completed={value['completed']} "
            f"W/L={value['wins']}/{value['losses']} win={_percentage(value['win_rate'])} "
            f"avgR={_number(value['average_r'])} cumR={_number(value['cumulative_r'])} "
            f"PF={_number(value['profit_factor_r'])} MFE/MAE={_number(value['average_mfe_r'])}/{_number(value['average_mae_r'])} "
            f"hold={_number(value['average_holding_seconds'], 1, 's')}"
        )
        lines.append("  statuses: " + ", ".join(f"{key}={count}" for key, count in value["statuses"].items()))
    lines.extend(["", "NO_TRADE CHECKPOINTS"])
    for minute, value in no_trade["checkpoints"].items():
        directions = value["direction_counts"]
        lines.append(
            f"{minute:>2}m n={value['observations']:<3} abs avg/med={_number(value['average_absolute_price_movement'], 3)}/"
            f"{_number(value['median_absolute_price_movement'], 3)} price | signed price avg/med="
            f"{_number(value['average_signed_price_movement'], 3)}/{_number(value['median_signed_price_movement'], 3)} | "
            f"signed pts avg/med={_number(value['average_signed_movement_points'], 1)}/{_number(value['median_signed_movement_points'], 1)} | "
            f"max price +/-={_number(value['maximum_positive_price_movement'], 3)}/{_number(value['maximum_negative_price_movement'], 3)} | "
            f"abs M1 avg/med={_number(value['average_absolute_move_m1_range'], 2)}x/{_number(value['median_absolute_move_m1_range'], 2)}x | "
            f"U/D/F={directions['UP']}/{directions['DOWN']}/{directions['FLAT']}"
        )
    excursion = no_trade["excursions"]
    lines.extend(
        [
            "",
            "NO_TRADE EXCURSIONS",
            f"Basis                : {excursion['basis']}",
            f"Average max up/down  : {_number(excursion['average_maximum_upward_excursion_points'], 1)} / {_number(excursion['average_maximum_downward_excursion_points'], 1)} pts",
            f"Average observed rng : {_number(excursion['average_observed_high_low_range_points'], 1)} pts",
            "",
            "MARKET CONTEXT PATTERNS (NO_TRADE)",
        ]
    )
    for name, values in report["market_context_patterns"].items():
        lines.append(f"{name:<20}: " + ", ".join(f"{key}={count}" for key, count in values.items()))
    lines.extend(["", "AI REASON THEMES (transparent text frequency)"])
    for theme in report["ai_reason_themes"]:
        example = _console_text(
            theme["representative_examples"][0]
            if theme["representative_examples"] else ""
        )
        lines.append(
            f"- {theme['theme']}: {theme['count']} ({_percentage(theme['percentage'])}) - {example}"
        )
    lines.append(
        "Reason terms          : "
        + ", ".join(
            f"{item['term']}={item['count']}"
            for item in report["ai_reason_term_frequencies"]
        )
    )
    lines.append("AI WARNING THEMES")
    for theme in report["ai_warning_themes"][:5]:
        example = _console_text(
            theme["representative_examples"][0]
            if theme["representative_examples"] else ""
        )
        lines.append(
            f"- {theme['theme']}: {theme['count']} ({_percentage(theme['percentage'])}) - {example}"
        )
    efficiency = report["candidate_efficiency"]
    candidates = report["deterministic_candidates"]
    conversion = report["ai_conversion"]
    replay = report["advisory_input_replay"]
    lines.extend(
        [
            "",
            "DETERMINISTIC CANDIDATES",
            f"Available / total    : {str(candidates['available']).lower()} / {candidates['total']}",
            f"Eligible / rejected  : {candidates['market_gate_eligible']} / {candidates['market_gate_rejected']}",
            f"News / spacing block : {candidates['news_blocked']} / {candidates['spacing_blocked']}",
            f"Duplicate / budget   : {candidates['duplicate']} / {candidates['budget_blocked']}",
            f"Sent to AI           : {candidates['sent_to_ai']}",
            "Terminal dispositions: "
            + ", ".join(
                f"{key}={value}" for key, value in candidates["dispositions"].items()
            ),
            "Latest observations   : "
            + (
                ", ".join(
                    f"{key}={value}"
                    for key, value in candidates[
                        "latest_observation_dispositions"
                    ].items()
                )
                or "none/legacy"
            ),
            "",
            "ADVISORY INPUT REPLAY",
            f"Replayable / legacy unavailable: {replay['replayable']} / {replay['legacy_unavailable']}",
            f"Missing new / prompt-versioned: {replay['missing_new_schema']} / {replay['prompt_versioned']}",
            "",
            "AI CONVERSION",
            f"Eligible / sent      : {conversion['eligible_deterministic_candidates']} / {conversion['sent_to_gpt']}",
            f"BUY / SELL / NO_TRADE: {conversion['BUY']} / {conversion['SELL']} / {conversion['NO_TRADE']}",
            f"Eligible sent / directional: {_percentage(conversion['percentage_eligible_sent'])} / {_percentage(conversion['percentage_directional'])}",
            "",
            "CANDIDATE EFFICIENCY (legacy advisory view)",
            f"Calls / directional / NO_TRADE: {efficiency['openai_calls']} / {efficiency['directional_decisions']} / {efficiency['no_trade_decisions']}",
            f"Directional / NO_TRADE %: {_percentage(efficiency['directional_percentage'])} / {_percentage(efficiency['no_trade_percentage'])}",
            f"Reservations / eligible sent contexts: {efficiency['candidate_reservations']} / {efficiency['eligible_contexts_persisted_for_sent_calls']}",
            "",
            "SERVER-TIME DISTRIBUTION",
        ]
    )
    for hour, value in report["time_distribution_server_hour"].items():
        lines.append(
            f"{hour}: candidates={value.get('candidates', 0)} calls={value['calls']} B/S/N={value['BUY']}/{value['SELL']}/{value['NO_TRADE']} "
            f"confidence={_number(value['average_confidence'], 1)} spread={_number(value['average_spread_points'], 1)} pts"
        )
    quality = report["data_quality"]
    lines.extend(["", "DATA QUALITY", f"Trustworthy           : {str(quality['trustworthy']).lower()}"])
    for key, value in quality.items():
        if key not in {"trustworthy", "warnings"}:
            lines.append(f"{key:<28}: {value}")
    for warning in quality["warnings"]:
        lines.append(f"WARNING: {warning}")
    baseline = report["baseline_readiness"]
    lines.extend(
        [
            "",
            "BASELINE READINESS",
            f"Sent contexts / GPT decisions / paper: {baseline['sent_candidate_context_available']} / {baseline['gpt_selection_available']} / {baseline['paper_outcomes_available']}",
            f"Unbiased all-eligible baseline: {str(baseline['unbiased_all_eligible_candidate_baseline_available']).lower()}",
        ]
    )
    for missing in baseline["missing_for_future_unbiased_comparison"]:
        lines.append(f"- Missing: {missing}")
    return "\n".join(lines)


def paper_report_to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False)
