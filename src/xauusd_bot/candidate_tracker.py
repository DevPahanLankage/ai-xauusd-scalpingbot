from __future__ import annotations

from dataclasses import dataclass
from threading import Lock

from .models import MarketGateResult
from .timestamps import TimestampError, canonical_timestamp


@dataclass(frozen=True, slots=True)
class CandidateReservation:
    reserved_for_ai: bool
    already_consumed: bool
    symbol: str
    completed_m1_time: str | None
    reason: str | None


class CandidateEvaluationTracker:
    """Tracks only candidates explicitly reserved for a future AI call.

    Merely evaluating a snapshot with MarketGate never mutates this tracker.
    Reservation is an explicit caller action and is atomic within this process.
    """

    def __init__(self) -> None:
        self._consumed: set[tuple[str, str]] = set()
        self._lock = Lock()

    def reserve_for_ai(
        self,
        symbol: str,
        gate_result: MarketGateResult,
    ) -> CandidateReservation:
        normalized_symbol = symbol.upper()
        candle_time = gate_result.completed_m1_time
        if not gate_result.eligible_for_ai:
            return CandidateReservation(
                reserved_for_ai=False,
                already_consumed=False,
                symbol=normalized_symbol,
                completed_m1_time=candle_time,
                reason="candidate_not_eligible",
            )
        if candle_time is None:
            return CandidateReservation(
                reserved_for_ai=False,
                already_consumed=False,
                symbol=normalized_symbol,
                completed_m1_time=None,
                reason="missing_completed_m1_candle",
            )
        try:
            canonical_time = canonical_timestamp(candle_time)
        except TimestampError:
            return CandidateReservation(
                reserved_for_ai=False,
                already_consumed=False,
                symbol=normalized_symbol,
                completed_m1_time=candle_time,
                reason="invalid_completed_m1_timestamp",
            )

        key = (normalized_symbol, canonical_time)
        with self._lock:
            if key in self._consumed:
                return CandidateReservation(
                    reserved_for_ai=False,
                    already_consumed=True,
                    symbol=normalized_symbol,
                    completed_m1_time=candle_time,
                    reason="completed_m1_already_consumed",
                )
            self._consumed.add(key)

        return CandidateReservation(
            reserved_for_ai=True,
            already_consumed=False,
            symbol=normalized_symbol,
            completed_m1_time=candle_time,
            reason=None,
        )

    def is_consumed(self, symbol: str, completed_m1_time: str) -> bool:
        try:
            key = (symbol.upper(), canonical_timestamp(completed_m1_time))
        except TimestampError:
            return False
        with self._lock:
            return key in self._consumed
