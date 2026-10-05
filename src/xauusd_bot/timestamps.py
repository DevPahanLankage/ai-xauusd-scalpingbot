from __future__ import annotations

from datetime import datetime, timezone


class TimestampError(ValueError):
    """Raised when a timestamp is invalid or cannot be compared safely."""


def parse_timestamp(value: str) -> datetime:
    """Parse MT5 naive server time or an ISO timestamp with Z/offset.

    Aware timestamps are normalized to UTC. Naive MT5 server timestamps remain
    naive because the broker's server timezone cannot safely be inferred.
    """

    if not value or not value.strip():
        raise TimestampError("Timestamp is empty")
    normalized = value.strip()
    if normalized.endswith(("Z", "z")):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise TimestampError(f"Invalid ISO timestamp: {value}") from exc
    if is_aware(parsed):
        return parsed.astimezone(timezone.utc)
    return parsed


def is_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def ensure_comparable(left: datetime, right: datetime) -> None:
    if is_aware(left) != is_aware(right):
        raise TimestampError("Cannot compare naive and timezone-aware timestamps")


def seconds_between(later: datetime, earlier: datetime) -> float:
    ensure_comparable(later, earlier)
    return (later - earlier).total_seconds()


def canonical_timestamp(value: str) -> str:
    parsed = parse_timestamp(value)
    return parsed.isoformat()


def mcp_timestamp(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()
