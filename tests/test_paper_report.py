from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from xauusd_bot import cli
from xauusd_bot.config import AIConfig
from xauusd_bot.paper_report import (
    build_paper_report,
    packaged_state_path,
    paper_report_to_json,
)
from xauusd_bot.state_store import SQLiteStateStore


NOW = datetime(2026, 10, 6, 20, 0, tzinfo=timezone.utc)


def _decision(name: str, confidence: int) -> str:
    return json.dumps(
        {
            "decision": {
                "decision": name,
                "confidence": confidence,
                "market_regime": "range",
                "setup_summary": "Insufficient confirmation near resistance",
                "entry_price": None,
                "entry_zone_low": None,
                "entry_zone_high": None,
                "stop_loss": None,
                "take_profit": None,
                "risk_reward_ratio": None,
                "invalidation_reason": "Wait for confirmed breakout",
                "warnings": ["Whipsaw risk"],
            },
            "status": "success",
            "usage": {},
            "estimated_cost_usd": 0.01,
            "latency_ms": 100.0,
        }
    )


def _context(candidate: str) -> str:
    return json.dumps(
        {
            "symbol": "XAUUSD",
            "candidate_time": candidate,
            "quote": {"bid": 2000.0, "ask": 2000.2},
            "point": 0.01,
            "market_gate": {
                "eligible_for_ai": True,
                "direction_m1": "DOWN",
                "direction_m5": "DOWN",
                "metrics": {
                    "spread_points": 20.0,
                    "spread_to_m1_range_ratio": 0.1,
                    "m1_spike_ratio": 0.8,
                    "m5_spike_ratio": 1.2,
                },
            },
            "news_gate": {"safe_for_ai": True},
            "market_metrics": {
                "spread_points": 20.0,
                "m1_average_range_points": 200.0,
                "m5_average_range_points": 400.0,
            },
        }
    )


class ReportFixture:
    def __init__(self, path: Path) -> None:
        self.path = path
        SQLiteStateStore(path)
        self.connection = sqlite3.connect(path)

    def close(self) -> None:
        self.connection.commit()
        self.connection.close()

    def populate(self) -> None:
        records = [
            ("BUY", 85, "TP_HIT", 2.0, 2.5, -0.2),
            ("BUY", 55, "AMBIGUOUS", None, None, None),
            ("SELL", 75, "SL_HIT", -1.0, 0.3, -1.0),
            ("SELL", 65, "EXPIRED_UNFILLED", None, None, None),
            ("NO_TRADE", 90, "OBSERVATION_COMPLETE", None, None, None),
            ("NO_TRADE", 70, "OBSERVATION_COMPLETE", None, None, None),
        ]
        for index, (name, confidence, status, final_r, mfe, mae) in enumerate(records):
            candidate = f"2026-10-06T12:{index:02d}:00"
            cursor = self.connection.execute(
                """
                INSERT INTO api_usage
                    (timestamp, symbol, completed_m1_time, model, input_tokens,
                     cached_input_tokens, cache_write_tokens, output_tokens,
                     reasoning_tokens, estimated_cost_usd, advisory_json,
                     budget_reserved_usd, budget_accounted_usd, status, latency_ms)
                VALUES (?, 'XAUUSD', ?, 'test-model', 100, 10, 20, 30, 5,
                        0.01, ?, 0.01, 0.01, 'success', 100.0)
                """,
                (f"2026-10-06T10:{index:02d}:00+00:00", candidate, _decision(name, confidence)),
            )
            usage_id = int(cursor.lastrowid)
            self.connection.execute(
                """
                INSERT INTO candidate_reservations
                    (symbol, completed_m1_time, reserved_at, model, input_hash, result)
                VALUES ('XAUUSD', ?, ?, 'test-model', ?, ?)
                """,
                (candidate, f"2026-10-06T10:{index:02d}:00+00:00", f"hash-{index}", f"success:{name}"),
            )
            paper = self.connection.execute(
                """
                INSERT INTO paper_evaluations
                    (advisory_usage_id, symbol, candidate_time,
                     advisory_completed_at, tracking_start_market_time,
                     decision, confidence, status, entry_price, stop_loss,
                     take_profit, fill_at, fill_price, close_at, close_price,
                     final_r, mfe_r, mae_r, last_observed_market_time,
                     context_json, notes)
                VALUES (?, 'XAUUSD', ?, ?, ?, ?, ?, ?, 2000.0, 1999.0,
                        2002.0, ?, 2000.0, ?, 2002.0, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    usage_id,
                    candidate,
                    f"2026-10-06T10:{index:02d}:10+00:00",
                    candidate,
                    name,
                    confidence,
                    status,
                    candidate if final_r is not None else None,
                    f"2026-10-06T12:{index + 1:02d}:00" if final_r is not None else None,
                    final_r,
                    mfe,
                    mae,
                    f"2026-10-06T12:{index + 30:02d}:00",
                    _context(candidate),
                ),
            )
            paper_id = int(paper.lastrowid)
            if name == "NO_TRADE":
                points = (
                    (10, 20, 100, 200, 300, 400)
                    if index == 4
                    else (-10, -20, -50, -100, -150, -200)
                )
                for minute, movement in zip((1, 3, 5, 10, 15, 30), points):
                    midpoint = 2000.1 + movement * 0.01
                    self.connection.execute(
                        """
                        INSERT INTO paper_checkpoints
                            (paper_id, checkpoint_minutes, observed_at, bid, ask,
                             midpoint, movement_points, favorable_r, adverse_r)
                        VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                        """,
                        (
                            paper_id,
                            minute,
                            f"2026-10-06T12:{index + minute:02d}:00",
                            midpoint - 0.1,
                            midpoint + 0.1,
                            midpoint,
                            movement,
                        ),
                    )


class PaperReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        fixture = ReportFixture(self.path)
        fixture.populate()
        fixture.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def report(self):
        return build_paper_report(self.path, report_date=date(2026, 10, 6), now=NOW)

    def test_report_is_physically_and_logically_read_only(self) -> None:
        before = hashlib.sha256(self.path.read_bytes()).digest()
        connection = sqlite3.connect(self.path)
        counts_before = tuple(
            connection.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0]
            for table in ("api_usage", "paper_evaluations", "paper_checkpoints")
        )
        connection.close()
        self.report()
        after = hashlib.sha256(self.path.read_bytes()).digest()
        connection = sqlite3.connect(self.path)
        counts_after = tuple(
            connection.execute(f"SELECT COUNT(1) FROM {table}").fetchone()[0]
            for table in ("api_usage", "paper_evaluations", "paper_checkpoints")
        )
        connection.close()
        self.assertEqual(before, after)
        self.assertEqual(counts_before, counts_after)

    def test_decision_counts_and_confidence_buckets(self) -> None:
        report = self.report()
        self.assertEqual([report["decisions"][name]["count"] for name in ("BUY", "SELL", "NO_TRADE")], [2, 2, 2])
        buckets = report["confidence"]["buckets"]
        self.assertEqual(buckets["<60"]["BUY"], 1)
        self.assertEqual(buckets["60-69"]["SELL"], 1)
        self.assertEqual(buckets["70-79"]["advisories"], 2)
        self.assertEqual(buckets["80-89"]["BUY"], 1)
        self.assertEqual(buckets["90+"]["NO_TRADE"], 1)

    def test_no_trade_checkpoint_calculations(self) -> None:
        checkpoint = self.report()["no_trade"]["checkpoints"]["5"]
        self.assertEqual(checkpoint["observations"], 2)
        self.assertAlmostEqual(checkpoint["average_absolute_price_movement"], 0.75)
        self.assertAlmostEqual(checkpoint["median_signed_movement_points"], 25.0)
        self.assertAlmostEqual(checkpoint["average_absolute_move_m1_range"], 0.375)
        self.assertEqual(checkpoint["direction_counts"], {"UP": 1, "DOWN": 1, "FLAT": 0})

    def test_completed_statistics_exclude_ambiguous_and_unfilled(self) -> None:
        stats = self.report()["paper_performance"]["overall"]
        self.assertEqual(stats["candidates"], 4)
        self.assertEqual(stats["completed"], 2)
        self.assertEqual((stats["wins"], stats["losses"]), (1, 1))
        self.assertAlmostEqual(stats["average_r"], 0.5)
        self.assertAlmostEqual(stats["cumulative_r"], 1.0)
        self.assertAlmostEqual(stats["profit_factor_r"], 2.0)

    def test_themes_are_derived_from_persisted_text(self) -> None:
        themes = self.report()["ai_reason_themes"]
        self.assertTrue(any("insufficient confirmation" in item["theme"] for item in themes))
        self.assertEqual(themes[0]["count"], 2)

    def test_json_output_is_valid_and_contains_path(self) -> None:
        parsed = json.loads(paper_report_to_json(self.report()))
        self.assertEqual(parsed["report_period"]["database_path"], str(self.path.resolve()))
        self.assertEqual(parsed["api_usage"]["calls"], 6)

    def test_cli_report_bypasses_mt5_and_advisory_paths(self) -> None:
        output = StringIO()
        config = AIConfig(state_db_path=self.path)
        with (
            patch.object(cli, "load_dotenv"),
            patch.object(cli.AIConfig, "from_environment", return_value=config),
            patch.object(cli.Settings, "from_environment") as settings,
            patch.object(cli, "_run_ai") as ai,
            patch.object(cli, "_run") as mt5,
            redirect_stdout(output),
        ):
            cli.main(["--paper-report", "--json"])
        json.loads(output.getvalue())
        settings.assert_not_called()
        ai.assert_not_called()
        mt5.assert_not_called()


class PartialAndEmptyDatabaseTests(unittest.TestCase):
    def test_empty_database_returns_zero_report_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "empty.sqlite3"
            sqlite3.connect(path).close()
            before = path.read_bytes()
            report = build_paper_report(path)
            self.assertEqual(report["api_usage"]["calls"], 0)
            self.assertEqual(path.read_bytes(), before)

    def test_older_usage_only_database_reports_partial_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE api_usage (
                    id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT,
                    completed_m1_time TEXT, estimated_cost_usd REAL,
                    advisory_json TEXT, status TEXT, latency_ms REAL
                )
                """
            )
            connection.execute(
                "INSERT INTO api_usage VALUES (1, ?, 'XAUUSD', ?, 0.01, ?, 'success', 10.0)",
                ("2026-10-06T10:00:00+00:00", "2026-10-06T12:00:00", _decision("NO_TRADE", 80)),
            )
            connection.commit()
            connection.close()
            report = build_paper_report(path)
            self.assertEqual(report["decisions"]["NO_TRADE"]["count"], 1)
            self.assertIn("paper_evaluations table is missing (older database)", report["data_quality"]["warnings"])

    def test_packaged_state_path_uses_local_app_data(self) -> None:
        config = AIConfig(state_db_path=Path(".state/custom.sqlite3"))
        result = packaged_state_path(config, local_app_data="C:/LocalData")
        self.assertEqual(result, Path("C:/LocalData/XAUUSD-AI/.state/custom.sqlite3").resolve())


if __name__ == "__main__":
    unittest.main()
