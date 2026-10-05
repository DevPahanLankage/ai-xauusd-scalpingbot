"""Read-only XAUUSD market snapshot collector."""

from .candidate_tracker import CandidateEvaluationTracker, CandidateReservation
from .models import MarketGateResult, XAUUSDMarketSnapshot

__all__ = [
    "CandidateEvaluationTracker",
    "CandidateReservation",
    "MarketGateResult",
    "XAUUSDMarketSnapshot",
]
__version__ = "0.1.0"
