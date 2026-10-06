from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .ai_models import AdvisoryResult, TradeDecision
from .config import PaperConfig
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, Tick, XAUUSDMarketSnapshot
from .state_store import AttemptReservation, SQLiteStateStore
from .timestamps import TimestampError, parse_timestamp, seconds_between


ACTIVE_STATUSES = frozenset({"PENDING_ENTRY", "OPEN", "OBSERVING"})
PAPER_STATUSES = frozenset(
    {
        "PENDING_ENTRY", "OPEN", "TP_HIT", "SL_HIT", "EXPIRED_UNFILLED",
        "EXPIRED_OPEN", "INVALID", "CANCELLED_BY_SAFETY", "AMBIGUOUS",
        "OBSERVING", "OBSERVATION_COMPLETE", "OBSERVATION_INCOMPLETE",
    }
)


@dataclass(frozen=True, slots=True)
class PaperRegistration:
    paper_id: int | None
    status: str | None


def _context(
    snapshot: XAUUSDMarketSnapshot,
    gate: MarketGateResult,
    news: EconomicNewsGateResult,
) -> str:
    # Deliberately exclude account identity, credentials, headers, and tokens.
    value = {
        "symbol": snapshot.symbol.symbol,
        "candidate_time": gate.completed_m1_time,
        "quote": {"bid": snapshot.symbol.bid, "ask": snapshot.symbol.ask},
        "point": snapshot.symbol.point,
        "market_gate": gate.to_dict(),
        "news_gate": news.to_dict(),
        "market_metrics": {
            "spread_points": snapshot.metrics.spread_points,
            "m1_average_range_points": snapshot.metrics.m1_average_range_points,
            "m5_average_range_points": snapshot.metrics.m5_average_range_points,
        },
    }
    return json.dumps(value, allow_nan=False, separators=(",", ":"))


def _valid_trade_geometry(result: AdvisoryResult) -> bool:
    decision = result.decision
    if decision.decision is TradeDecision.NO_TRADE:
        return True
    entries = [
        value
        for value in (decision.entry_price, decision.entry_zone_low, decision.entry_zone_high)
        if value is not None
    ]
    if not entries or decision.stop_loss is None or decision.take_profit is None:
        return False
    if not all(math.isfinite(value) for value in (*entries, decision.stop_loss, decision.take_profit)):
        return False
    low, high = min(entries), max(entries)
    if decision.decision is TradeDecision.BUY:
        return decision.stop_loss < low <= high < decision.take_profit
    return decision.take_profit < low <= high < decision.stop_loss


class PaperPerformanceTracker:
    """Persist and advance spread-aware paper observations; never sends orders."""

    def __init__(self, store: SQLiteStateStore, config: PaperConfig) -> None:
        self.store = store
        self.config = config

    def register(
        self,
        *,
        reservation: AttemptReservation,
        result: AdvisoryResult,
        snapshot: XAUUSDMarketSnapshot,
        market_gate: MarketGateResult,
        news_gate: EconomicNewsGateResult,
        completed_at: datetime | None = None,
        tracking_start_market_time: str | None = None,
    ) -> PaperRegistration:
        if not reservation.reserved or reservation.usage_id is None or result.status != "success":
            return PaperRegistration(None, None)
        decision = result.decision
        status = "OBSERVING" if decision.decision is TradeDecision.NO_TRADE else "PENDING_ENTRY"
        notes = None
        if not _valid_trade_geometry(result):
            status, notes = "INVALID", "invalid_advisory_geometry"
        moment = completed_at or datetime.now(timezone.utc)
        if moment.tzinfo is None or moment.utcoffset() is None:
            raise ValueError("Advisory completion time must be timezone-aware")
        paper_id = self.store.create_paper_evaluation(
            {
                "advisory_usage_id": reservation.usage_id,
                "symbol": snapshot.symbol.symbol,
                "candidate_time": reservation.completed_m1_time,
                "advisory_completed_at": moment.astimezone(timezone.utc).isoformat(),
                "tracking_start_market_time": (
                    tracking_start_market_time or snapshot.trade_server_time
                ),
                "decision": decision.decision.value,
                "confidence": decision.confidence,
                "status": status,
                "entry_price": decision.entry_price,
                "entry_zone_low": decision.entry_zone_low,
                "entry_zone_high": decision.entry_zone_high,
                "stop_loss": decision.stop_loss,
                "take_profit": decision.take_profit,
                "context_json": _context(snapshot, market_gate, news_gate),
                "notes": notes,
            }
        )
        return PaperRegistration(paper_id, status)

    def observe(self, snapshot: XAUUSDMarketSnapshot) -> None:
        for row in self.store.active_paper_evaluations():
            if str(row["symbol"]).upper() != snapshot.symbol.symbol.upper():
                continue
            try:
                self._observe_one(row, snapshot)
            except (TimestampError, TypeError, ValueError):
                # Timestamp ambiguity must not manufacture fills or outcomes.
                self.store.update_paper_evaluation(
                    int(row["id"]),
                    {"notes": "paper_observation_timestamp_incomparable"},
                )

    def _observe_one(self, row: dict[str, Any], snapshot: XAUUSDMarketSnapshot) -> None:
        paper_id = int(row["id"])
        start = parse_timestamp(str(row["tracking_start_market_time"]))
        now = parse_timestamp(snapshot.trade_server_time)
        seconds_between(now, start)
        last_value = row.get("last_observed_market_time") or row["tracking_start_market_time"]
        last = parse_timestamp(str(last_value))
        ticks = []
        for tick in snapshot.recent_ticks:
            tick_time = parse_timestamp(tick.time)
            if seconds_between(tick_time, start) >= 0 and seconds_between(tick_time, last) > 0:
                ticks.append((tick_time, tick))
        ticks.sort(key=lambda item: item[0])
        if self._has_observation_gap(last, now, ticks):
            status = str(row["status"])
            replacement = (
                "AMBIGUOUS" if status == "OPEN"
                else "CANCELLED_BY_SAFETY" if status == "PENDING_ENTRY"
                else "OBSERVATION_INCOMPLETE"
            )
            self.store.update_paper_evaluation(
                paper_id,
                {
                    "status": replacement,
                    "close_at": snapshot.trade_server_time,
                    "last_observed_market_time": snapshot.trade_server_time,
                    "notes": "observation_gap_exceeded",
                },
            )
            return
        self._record_checkpoints(row, snapshot, start, ticks)
        status = str(row["status"])
        updates: dict[str, Any] = {"last_observed_market_time": snapshot.trade_server_time}
        if status == "OBSERVING":
            if seconds_between(now, start) >= max(self.config.checkpoint_minutes) * 60:
                updates["status"] = "OBSERVATION_COMPLETE"
            self.store.update_paper_evaluation(paper_id, updates)
            return
        if status == "PENDING_ENTRY":
            fill = next(((stamp, tick) for stamp, tick in ticks if self._entry_reached(row, tick)), None)
            if fill is None:
                if seconds_between(now, start) >= self.config.entry_expiry_minutes * 60:
                    updates["status"] = "EXPIRED_UNFILLED"
                self.store.update_paper_evaluation(paper_id, updates)
                return
            fill_time, fill_tick = fill
            fill_price = fill_tick.ask if row["decision"] == "BUY" else fill_tick.bid
            row.update(status="OPEN", fill_at=fill_time.isoformat(), fill_price=fill_price)
            updates.update(status="OPEN", fill_at=fill_time.isoformat(), fill_price=fill_price)
            ticks = [(stamp, tick) for stamp, tick in ticks if stamp >= fill_time]
        if row["status"] == "OPEN":
            self._advance_open(row, ticks, now, snapshot, updates)
        self.store.update_paper_evaluation(paper_id, updates)

    def _has_observation_gap(
        self,
        last: datetime,
        now: datetime,
        ticks: list[tuple[datetime, Tick]],
    ) -> bool:
        timeline = [last, *(stamp for stamp, _ in ticks), now]
        return any(
            seconds_between(current, previous)
            > self.config.max_observation_gap_seconds
            for previous, current in zip(timeline, timeline[1:])
        )

    @staticmethod
    def _entry_reached(row: dict[str, Any], tick: Tick) -> bool:
        price = tick.ask if row["decision"] == "BUY" else tick.bid
        low, high = row.get("entry_zone_low"), row.get("entry_zone_high")
        if low is not None and high is not None:
            return price <= float(high) if row["decision"] == "BUY" else price >= float(low)
        entry = row.get("entry_price")
        if entry is None:
            return False
        return price <= float(entry) if row["decision"] == "BUY" else price >= float(entry)

    def _advance_open(
        self,
        row: dict[str, Any],
        ticks: list[tuple[datetime, Tick]],
        now: datetime,
        snapshot: XAUUSDMarketSnapshot,
        updates: dict[str, Any],
    ) -> None:
        fill = float(row["fill_price"])
        stop = float(row["stop_loss"])
        target = float(row["take_profit"])
        risk = abs(fill - stop)
        if risk <= 0:
            updates.update(status="INVALID", notes="non_positive_fill_risk")
            return
        mfe = float(row["mfe_price"] or 0.0)
        mae = float(row["mae_price"] or 0.0)
        for stamp, tick in ticks:
            exit_price = tick.bid if row["decision"] == "BUY" else tick.ask
            movement = exit_price - fill if row["decision"] == "BUY" else fill - exit_price
            mfe = max(mfe, movement)
            mae = min(mae, movement)
            stop_hit = exit_price <= stop if row["decision"] == "BUY" else exit_price >= stop
            target_hit = exit_price >= target if row["decision"] == "BUY" else exit_price <= target
            if stop_hit or target_hit:
                close = stop if stop_hit else target
                final_r = (close - fill) / risk if row["decision"] == "BUY" else (fill - close) / risk
                updates.update(
                    status="SL_HIT" if stop_hit else "TP_HIT",
                    close_at=stamp.isoformat(), close_price=close, final_r=final_r,
                    mfe_price=mfe, mae_price=mae, mfe_r=mfe / risk, mae_r=mae / risk,
                )
                return
        fill_time = parse_timestamp(str(row["fill_at"]))
        if seconds_between(now, fill_time) >= self.config.max_trade_minutes * 60:
            exit_price = snapshot.symbol.bid if row["decision"] == "BUY" else snapshot.symbol.ask
            movement = exit_price - fill if row["decision"] == "BUY" else fill - exit_price
            updates.update(
                status="EXPIRED_OPEN", close_at=snapshot.trade_server_time,
                close_price=exit_price, final_r=movement / risk,
                mfe_price=max(mfe, movement), mae_price=min(mae, movement),
                mfe_r=max(mfe, movement) / risk, mae_r=min(mae, movement) / risk,
            )
        else:
            updates.update(mfe_price=mfe, mae_price=mae, mfe_r=mfe / risk, mae_r=mae / risk)

    def _record_checkpoints(
        self,
        row: dict[str, Any],
        snapshot: XAUUSDMarketSnapshot,
        start: datetime,
        ticks: list[tuple[datetime, Tick]],
    ) -> None:
        try:
            context = json.loads(row["context_json"])
            initial_mid = (float(context["quote"]["bid"]) + float(context["quote"]["ask"])) / 2
            point = float(context["point"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return
        for minutes in self.config.checkpoint_minutes:
            target = start + timedelta(minutes=minutes)
            observed = next(((stamp, tick) for stamp, tick in ticks if stamp >= target), None)
            if observed is None:
                continue
            stamp, tick = observed
            midpoint = (tick.bid + tick.ask) / 2
            movement = (midpoint - initial_mid) / point if point > 0 else None
            favorable_r = adverse_r = None
            if row.get("fill_price") is not None and row.get("stop_loss") is not None:
                fill = float(row["fill_price"])
                risk = abs(fill - float(row["stop_loss"]))
                if risk > 0:
                    exit_price = tick.bid if row["decision"] == "BUY" else tick.ask
                    value = (
                        (exit_price - fill) / risk
                        if row["decision"] == "BUY"
                        else (fill - exit_price) / risk
                    )
                    favorable_r, adverse_r = max(value, 0.0), min(value, 0.0)
            self.store.add_paper_checkpoint(
                {
                    "paper_id": row["id"], "checkpoint_minutes": minutes,
                    "observed_at": stamp.isoformat(), "bid": tick.bid, "ask": tick.ask,
                    "midpoint": midpoint, "movement_points": movement,
                    "favorable_r": favorable_r, "adverse_r": adverse_r,
                }
            )

    @staticmethod
    def candle_exit_status(*, side: str, high: float, low: float, stop: float, target: float) -> str | None:
        """Conservative fallback classifier for unordered candle-only evidence."""
        stop_hit = low <= stop if side == "BUY" else high >= stop
        target_hit = high >= target if side == "BUY" else low <= target
        if stop_hit and target_hit:
            return "AMBIGUOUS"
        if stop_hit:
            return "SL_HIT"
        if target_hit:
            return "TP_HIT"
        return None

    def apply_candle_exit_evidence(
        self,
        *,
        paper_id: int,
        candle_time: str,
        high: float,
        low: float,
    ) -> str | None:
        """Apply candle-only recovery evidence, preserving dual-hit ambiguity."""
        row = self.store.paper_evaluation(paper_id)
        if row is None or row["status"] != "OPEN":
            return None
        status = self.candle_exit_status(
            side=str(row["decision"]), high=high, low=low,
            stop=float(row["stop_loss"]), target=float(row["take_profit"]),
        )
        if status is None:
            return None
        if status == "AMBIGUOUS":
            self.store.update_paper_evaluation(
                paper_id,
                {"status": status, "close_at": candle_time, "notes": "unordered_candle_hit_stop_and_target"},
            )
            return status
        close = float(row["stop_loss"] if status == "SL_HIT" else row["take_profit"])
        fill = float(row["fill_price"])
        risk = abs(fill - float(row["stop_loss"]))
        final_r = ((close - fill) if row["decision"] == "BUY" else (fill - close)) / risk
        self.store.update_paper_evaluation(
            paper_id,
            {"status": status, "close_at": candle_time, "close_price": close, "final_r": final_r, "notes": "candle_only_recovery_evidence"},
        )
        return status

    def cancel_by_safety(self, paper_id: int, *, market_time: str, reason: str) -> bool:
        """Explicitly stop an observation when a future safety policy requires it."""
        row = self.store.paper_evaluation(paper_id)
        if row is None or row["status"] not in {"PENDING_ENTRY", "OPEN"}:
            return False
        self.store.update_paper_evaluation(
            paper_id,
            {
                "status": "CANCELLED_BY_SAFETY",
                "close_at": market_time,
                "notes": f"safety:{reason}",
            },
        )
        return True
