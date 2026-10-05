# XAUUSD news-aware AI advisory

This Python application collects a typed, read-only `XAUUSDMarketSnapshot` from the
MetaTrader 5 Terminal MCP. It applies deterministic market and USD economic-news
gates before an optional GPT advisory. AI output is analysis only: the project has no
order execution, order modification, EA attachment, chart mutation, or AutoTrading
control.

## Safety boundary

The MCP client has an explicit allowlist containing only nine read-only tools:

- `get_workspace_info`
- `get_marketwatch_symbols`
- `get_trading_account_info`
- `get_trading_open_positions`
- `get_time_information`
- `get_chart_history`
- `get_chart_ticks_history`
- `economic_calendar_list_events_by_currency`
- `economic_calendar_list_values`

Any other tool name, including every `trade_*` tool, is rejected locally.
The collector never changes Market Watch, charts, EAs, services, AutoTrading, orders,
or positions.

Before AI can receive sanitized market data, `MarketGate` checks quote
and tick freshness, active-market state, absolute and volatility-relative spread,
history sufficiency, spike conditions, M1/M5 direction and alignment, existing
XAUUSD exposure, and free margin. Any critical failure sets `eligible_for_ai`
to `false`. `EconomicCalendarGate` separately rejects the candidate within the
configured window around high-importance USD events. Missing, truncated, malformed,
unknown-time, or incompatible calendar data fails closed.

The model receives no tools and no MCP access. Its compact payload excludes account
login, owner/broker identity, balance, equity, free margin, credentials, tokens, and
authorization headers. A local validator converts malformed or geometrically invalid
BUY/SELL recommendations to `NO_TRADE`.

## Setup

Python 3.10 or newer is required.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
Copy-Item .env.example .env
```

Set `MT5_MCP_TOKEN` in `.env`. Set `OPENAI_API_KEY` only when advisory mode is
intentionally enabled. Never commit `.env`, `.codex/config.toml`, or local state.

## Run

Human-readable output:

```powershell
.venv\Scripts\python -m xauusd_bot
```

JSON output:

```powershell
.venv\Scripts\python -m xauusd_bot --json
```

Both commands above are free/read-only and never initialize or call OpenAI. Explicit
advisory mode is the only mode that may make one API request after all gates, budget
checks, and duplicate protection pass:

```powershell
.venv\Scripts\python -m xauusd_bot --ai
.venv\Scripts\python -m xauusd_bot --ai --json
```

Display local usage without contacting MT5 or OpenAI:

```powershell
.venv\Scripts\python -m xauusd_bot --usage
```

Tests:

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
```

The direction metric compares the latest M1 close with the first close in the
configured lookback. A move smaller than 10% of the recent average M1 candle range
is classified as `FLAT`; otherwise it is `UP` or `DOWN`.

All market thresholds are defined in `MarketGateConfig`; news thresholds are in
`NewsGateConfig`. Defaults create a 30-minute blackout before and after high-impact
USD events, with a 2-hour lookback and 24-hour lookahead for advisory context.

## AI configuration and cost controls

The default model is `gpt-6.1-sol` with `reasoning.effort=low`, called through the
Responses API with Pydantic Structured Outputs. The compact payload defaults to the
latest 30 completed M1 candles and 20 completed M5 candles.

Before any request, an atomic SQLite transaction checks and reserves the completed M1
candidate. The default database is `.state/xauusd_bot.sqlite3`, which is ignored by
Git. A unique `(symbol, completed_m1_time)` key prevents duplicate spend across
threads and process restarts. A reserved candidate is never automatically retried,
including after a timeout or crash.

Default limits are 30 calls/day, $0.65 estimated spend/day, and $5.00/week. Pricing is
centralized in `AIConfig`: $2.00/1M uncached input tokens, $0.10/1M cached input
tokens, and $10.00/1M output tokens. Successful responses use actual SDK-reported
usage. Failed attempts with unknown usage retain null token/cost fields and keep the
candidate consumed.

See `.env.example` for every supported `XAUUSD_NEWS_*`, `OPENAI_*`, and state-path
override.
