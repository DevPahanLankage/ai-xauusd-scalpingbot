from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from dotenv import load_dotenv

from .collector import XAUUSDCollector
from .config import ConfigurationError, Settings
from .logging_utils import configure_logging
from .market_gate import MarketGate
from .mcp_client import MT5ReadOnlyClient
from .output import to_human, to_json


LOGGER = logging.getLogger(__name__)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a read-only XAUUSD market snapshot from the MT5 Terminal MCP."
    )
    parser.add_argument("--json", action="store_true", help="Print the full snapshot as JSON")
    return parser


async def _run(settings: Settings, json_output: bool) -> None:
    async with MT5ReadOnlyClient(settings) as client:
        snapshot = await XAUUSDCollector(client, settings).collect()
    gate = MarketGate(settings.market_gate).evaluate(snapshot)
    print(to_json(snapshot, gate) if json_output else to_human(snapshot, gate))


def main() -> None:
    args = _parser().parse_args()
    load_dotenv()
    try:
        settings = Settings.from_environment()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    configure_logging(settings.log_level, secrets=(settings.mcp_token or "",))
    try:
        asyncio.run(_run(settings, args.json))
    except Exception as exc:
        # The formatter redacts the configured token if an upstream exception ever
        # includes it. Authentication headers are never logged intentionally.
        LOGGER.error("Snapshot collection failed (%s): %s", type(exc).__name__, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
