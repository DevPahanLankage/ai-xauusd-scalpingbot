from __future__ import annotations

import copy
import unittest
from dataclasses import replace

from xauusd_bot.config import MarketGateConfig
from xauusd_bot.market_gate import MarketGate
from xauusd_bot.payload import (
    PayloadValidationError,
    build_ai_payload,
    validate_ai_payload,
)
from tests.test_advisory_flow import safe_news
from tests.test_market_gate import _snapshot


class PayloadValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _snapshot()
        self.market = MarketGate(MarketGateConfig()).evaluate(self.snapshot)
        self.news = safe_news()
        self.payload = build_ai_payload(
            self.snapshot, self.market, self.news, m1_limit=30, m5_limit=20
        )

    def validate(self, payload: dict | None = None) -> None:
        validate_ai_payload(
            payload or self.payload,
            self.snapshot,
            self.market,
            self.news,
            m1_required=30,
            m5_required=20,
        )

    def test_valid_sanitized_payload_passes(self) -> None:
        self.validate()

    def test_wrong_or_unselected_gold_symbol_fails_closed(self) -> None:
        for change in (
            {"symbol": "EURUSD"},
            {"symbol": "XAUUSD.other"},
        ):
            with self.subTest(change=change):
                payload = copy.deepcopy(self.payload)
                payload["instrument"].update(change)
                with self.assertRaises(PayloadValidationError):
                    self.validate(payload)
        with self.assertRaises(PayloadValidationError):
            validate_ai_payload(
                self.payload,
                replace(self.snapshot, symbol=replace(self.snapshot.symbol, selected=False)),
                self.market,
                self.news,
                m1_required=30,
                m5_required=20,
            )

    def test_invalid_bid_ask_or_point_fails_closed(self) -> None:
        changes = ({"bid": 0}, {"ask": 1900}, {"point": 0})
        for change in changes:
            with self.subTest(change=change):
                payload = copy.deepcopy(self.payload)
                payload["instrument"].update(change)
                with self.assertRaises(PayloadValidationError):
                    self.validate(payload)

    def test_insufficient_payload_history_fails_closed(self) -> None:
        for key in ("completed_m1_candles", "completed_m5_candles"):
            with self.subTest(key=key):
                payload = copy.deepcopy(self.payload)
                payload[key] = payload[key][1:]
                with self.assertRaises(PayloadValidationError):
                    self.validate(payload)

    def test_unordered_candles_fail_closed(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["completed_m1_candles"][-2:] = reversed(
            payload["completed_m1_candles"][-2:]
        )
        with self.assertRaises(PayloadValidationError):
            self.validate(payload)

    def test_latest_m1_must_match_candidate(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["completed_m1_candles"][-1]["time"] = "2026-01-05T12:10:00"
        with self.assertRaises(PayloadValidationError):
            self.validate(payload)

    def test_rejected_market_or_news_gate_fails_closed(self) -> None:
        with self.assertRaises(PayloadValidationError):
            validate_ai_payload(
                self.payload,
                self.snapshot,
                replace(self.market, eligible_for_ai=False),
                self.news,
                m1_required=30,
                m5_required=20,
            )
        with self.assertRaises(PayloadValidationError):
            validate_ai_payload(
                self.payload,
                self.snapshot,
                self.market,
                replace(self.news, safe_for_ai=False),
                m1_required=30,
                m5_required=20,
            )

    def test_non_finite_number_fails_closed(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["instrument"]["bid"] = float("nan")
        with self.assertRaises(PayloadValidationError):
            self.validate(payload)

    def test_forbidden_account_or_secret_field_fails_closed(self) -> None:
        for field in ("account", "balance", "openai_api_key", "authorization"):
            with self.subTest(field=field):
                payload = copy.deepcopy(self.payload)
                payload[field] = "must-not-pass"
                with self.assertRaises(PayloadValidationError):
                    self.validate(payload)


if __name__ == "__main__":
    unittest.main()
