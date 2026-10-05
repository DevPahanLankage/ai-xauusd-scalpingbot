from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from .advisory_service import AIAdvisoryService
from .collector import XAUUSDCollector
from .config import AIConfig, ConfigurationError, Settings
from .economic_calendar import EconomicCalendarCollector, EconomicCalendarGate
from .logging_utils import configure_logging
from .market_gate import MarketGate
from .mcp_client import MT5ReadOnlyClient
from .output import advisory_to_human, advisory_to_json, to_human, to_json
from .state_store import SQLiteStateStore


LOGGER = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a read-only XAUUSD market snapshot from the MT5 Terminal MCP."
    )
    parser.add_argument("--json", action="store_true", help="Print the full snapshot as JSON")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--ai",
        action="store_true",
        help="Run one explicitly opted-in, advisory-only AI evaluation",
    )
    mode.add_argument(
        "--usage",
        action="store_true",
        help="Display persisted AI usage without contacting MT5 or OpenAI",
    )
    return parser


async def _run(settings: Settings, json_output: bool) -> None:
    async with MT5ReadOnlyClient(settings) as client:
        snapshot = await XAUUSDCollector(client, settings).collect()
    gate = MarketGate(settings.market_gate).evaluate(snapshot)
    print(to_json(snapshot, gate) if json_output else to_human(snapshot, gate))


async def _run_ai(settings: Settings, json_output: bool) -> None:
    if not settings.ai.api_key:
        LOGGER.warning(
            "OPENAI_API_KEY is not configured; an otherwise eligible candidate will be skipped"
        )
    news_gate_engine = EconomicCalendarGate(settings.news_gate)
    async with MT5ReadOnlyClient(settings) as client:
        snapshot = await XAUUSDCollector(client, settings).collect()
        market_gate = MarketGate(settings.market_gate).evaluate(snapshot)
        try:
            events = await EconomicCalendarCollector(client, settings.news_gate).collect(
                snapshot.trade_server_time
            )
            news_gate = news_gate_engine.evaluate(snapshot.trade_server_time, events)
        except Exception as exc:
            LOGGER.warning("Economic calendar failed closed (%s)", type(exc).__name__)
            news_gate = news_gate_engine.unavailable(
                f"calendar_unavailable:{type(exc).__name__}"
            )
    outcome = await AIAdvisoryService(settings.ai).evaluate(
        snapshot, market_gate, news_gate
    )
    print(
        advisory_to_json(snapshot, market_gate, news_gate, outcome)
        if json_output
        else advisory_to_human(snapshot, market_gate, news_gate, outcome)
    )


def _print_usage(config: AIConfig, json_output: bool) -> None:
    summary = SQLiteStateStore(config.state_db_path).usage_summary()
    if json_output:
        import json

        print(json.dumps(summary.to_dict(), indent=2))
        return
    print("OpenAI advisory usage (local persisted estimate)")
    print("=" * 47)
    print(f"Calls today         : {summary.calls_today}")
    print(f"Spend today USD     : {summary.estimated_spend_today_usd:.6f}")
    print(f"Calls this week     : {summary.calls_this_week}")
    print(f"Spend this week USD : {summary.estimated_spend_this_week_usd:.6f}")


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    load_dotenv()
    if args.usage:
        try:
            ai_config = AIConfig.from_environment()
            configure_logging(
                "INFO", secrets=(ai_config.api_key or "",)
            )
            _print_usage(ai_config, args.json)
        except ConfigurationError as exc:
            print(f"Configuration error: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        return
    try:
        settings = Settings.from_environment()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(
        settings.log_level,
        secrets=(settings.mcp_token or "", settings.ai.api_key or ""),
    )
    try:
        asyncio.run(_run_ai(settings, args.json) if args.ai else _run(settings, args.json))
    except Exception as exc:
        # The formatter redacts the configured token if an upstream exception ever
        # includes it. Authentication headers are never logged intentionally.
        LOGGER.error("Snapshot collection failed (%s): %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
