# XAUUSD read-only market collector

This phase provides a Python foundation that builds a typed `XAUUSDMarketSnapshot`
from the connected MetaTrader 5 Terminal MCP and evaluates it with a deterministic,
fail-closed `MarketGate`. It contains no AI calls and no order execution code.

## Safety boundary

The MCP client has an explicit allowlist containing only:

- `get_workspace_info`
- `get_marketwatch_symbols`
- `get_trading_account_info`
- `get_trading_open_positions`
- `get_time_information`
- `get_chart_history`
- `get_chart_ticks_history`

Any other tool name, including every `trade_*` tool, is rejected locally.
The collector never changes Market Watch, charts, EAs, services, AutoTrading, orders,
or positions.

Before any future AI integration can receive a snapshot, `MarketGate` checks quote
and tick freshness, active-market state, absolute and volatility-relative spread,
history sufficiency, spike conditions, M1/M5 direction and alignment, existing
XAUUSD exposure, and free margin. Any critical failure sets `eligible_for_ai`
to `false`.

## Setup

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

Set `MT5_MCP_TOKEN` in `.env`. Do not commit `.env` or `.codex/config.toml`.

## Run

Human-readable output:

```powershell
.venv\Scripts\python -m xauusd_bot
```

JSON output:

```powershell
.venv\Scripts\python -m xauusd_bot --json
```

Tests:

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

The direction metric compares the latest M1 close with the first close in the
configured lookback. A move smaller than 10% of the recent average M1 candle range
is classified as `FLAT`; otherwise it is `UP` or `DOWN`.

All gate thresholds are defined together in `MarketGateConfig` and can be overridden
with the `XAUUSD_GATE_*` variables documented in `.env.example`. `MarketGate` is pure:
inspecting an ineligible snapshot never consumes its completed M1 candle. A future AI
caller must explicitly call `CandidateEvaluationTracker.reserve_for_ai()` after an
eligible result. Reservations are process-local, so a long-running process must reuse
the same tracker across polling cycles.
