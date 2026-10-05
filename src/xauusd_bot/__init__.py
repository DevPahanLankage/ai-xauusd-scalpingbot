"""Read-only XAUUSD collector with advisory-only AI analysis."""

from .candidate_tracker import CandidateEvaluationTracker, CandidateReservation
from .models import MarketGateResult, XAUUSDMarketSnapshot

__all__ = [
    "CandidateEvaluationTracker",
    "CandidateReservation",
    "MarketGateResult",
    "XAUUSDMarketSnapshot",
]
__version__ = "0.2.0"
