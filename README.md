# XAUUSD advisory engine and desktop monitor

This Python application collects a typed, read-only `XAUUSDMarketSnapshot` from the
MetaTrader 5 Terminal MCP. It applies deterministic market and USD economic-news
gates before an optional GPT advisory. AI output is analysis only: the project has no
order execution, order modification, EA attachment, chart mutation, or AutoTrading
control.

## Windows desktop application

The repository now includes a dual-interface desktop monitor built around the
existing Python engine. Market data is collected once, gates are calculated once,
and a normalized, secret-free `ApplicationState` is presented by both interfaces:

```text
MT5 Terminal MCP -> existing collector / gates / preview
                 -> shared monitoring service and application state
                    |-> Rich live CLI
                    `-> FastAPI + WebSocket -> React dashboard
```

The dashboard does not contain alternate gate or advisory logic. React displays
backend truth and never talks to MT5 or OpenAI. Opening the executable, opening or
refreshing the dashboard, and reconnecting its WebSocket make zero OpenAI requests.
The monitor can display `AI ELIGIBLE`, but an advisory remains available only through
the existing explicit `--ai` developer command documented below.

One windowed executable supports all main launch modes:

```powershell
# Rich live console and automatic browser dashboard
.\XAUUSD-AI.exe

# Browser dashboard only; no console window
.\XAUUSD-AI.exe --browser-only

# Rich live console only; no browser or web server
.\XAUUSD-AI.exe --cli-only
```

The local web service binds only to `127.0.0.1:8765`. The browser opens only after
the server reports ready. Browser-only mode includes a confirmed **Exit Application**
action; console modes support Ctrl+C. There are no BUY, SELL, order, position, or
execution controls.

### Desktop data and update behavior

The monitoring service keeps one MT5 MCP connection and one collector. Bid/ask,
server time, and spread refresh every five seconds; account/exposure refresh every
15 seconds; news refreshes every minute. Completed M1/M5 histories are reused and
refreshed when a new completed M1 candle is due. Both views receive the same revisioned
state. The bounded event feed records meaningful transitions—connection, candidate,
gate, news, and advisory changes—not raw ticks.

The browser provides a dark trading-terminal layout, M1/M5 candlestick selector,
bid/ask lines, and persisted advisory entry/zone/SL/TP levels when they exist.
`NO_TRADE` never creates chart levels. Current candidate identity and the persisted
last advisory are labeled separately to prevent stale decisions from appearing current.

### Run the desktop app from source

Build the frontend once, then start the Python desktop entry point:

```powershell
Set-Location frontend
npm ci
npm run lint
npm run typecheck
npm test
npm run build
Set-Location ..

.venv\Scripts\python -m xauusd_bot.desktop
.venv\Scripts\python -m xauusd_bot.desktop --browser-only
.venv\Scripts\python -m xauusd_bot.desktop --cli-only
```

Vite is development/build tooling only. Production assets are served directly by
FastAPI; there is no Node production server.

### Build `XAUUSD-AI.exe`

From a Windows PowerShell prompt:

```powershell
.\scripts\build_desktop.ps1
```

The script installs the declared frontend lockfile, runs frontend validation, builds
the React production assets, installs the Python desktop build extra, and runs the
PyInstaller specification. The reproducible local output is:

```text
dist\XAUUSD-AI.exe
```

Generated frontend assets, PyInstaller work files, and binaries are intentionally
ignored by Git.

### Packaged configuration

The executable never contains `.env`. For packaged use, place a private `.env` beside
`XAUUSD-AI.exe`:

```text
deployment-folder\
  XAUUSD-AI.exe
  .env
```

When run from source, the application reads `.env` from the current project directory.
Do not place runtime secrets in `frontend/`; nothing from the process environment is
serialized into browser state. The browser may receive local balance, equity, free
margin, and position count, but never account login, broker/server identity, MCP token,
OpenAI key, credentials, or authorization headers.

Persistent state paths are deterministic:

- The normal developer CLI preserves its existing behavior: a relative
  `XAUUSD_AI_STATE_DB` is relative to the directory from which the CLI is run.
- The source desktop resolves a relative path against the directory containing the
  project `.env`.
- The packaged executable resolves a relative path under
  `%LOCALAPPDATA%\XAUUSD-AI`. With the default value, the packaged database is
  `%LOCALAPPDATA%\XAUUSD-AI\.state\xauusd_bot.sqlite3`.
- An absolute `XAUUSD_AI_STATE_DB` is always used exactly as configured. Use this when
  the source CLI and packaged desktop should intentionally share one database.

To retain an independent packaged copy of existing development history, close every
source and packaged instance first. Confirm that the destination does not exist, then
copy the database explicitly:

```powershell
$source = Resolve-Path '.state\xauusd_bot.sqlite3'
$targetDirectory = Join-Path $env:LOCALAPPDATA 'XAUUSD-AI\.state'
$target = Join-Path $targetDirectory 'xauusd_bot.sqlite3'
if (Test-Path -LiteralPath $target) { throw "Destination already exists: $target" }
New-Item -ItemType Directory -Path $targetDirectory -Force | Out-Null
Copy-Item -LiteralPath $source -Destination $target
```

This migration is never automatic and never overwrites an existing packaged database.
Alternatively, set the packaged `.env` to the absolute source database path so usage,
candidate reservations, and advisory history all continue from that single file.

### Dashboard screenshot

_Placeholder: add a current dashboard screenshot after a release build is selected._

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
BUY/SELL recommendations to `NO_TRADE`. Before reservation, the exact outbound payload
is also checked for the selected gold symbol, valid quotes, sufficient chronological
M1/M5 history, candidate identity, finite JSON values, both gate decisions, and
forbidden account/secret fields. Recommendations detached from current price by more
than the configured recent-volatility multiple are converted to `NO_TRADE`.

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

Inspect first-call readiness without initializing OpenAI, reserving a candle, charging
budget, or writing SQLite state:

```powershell
.venv\Scripts\python -m xauusd_bot --ai-preview
.venv\Scripts\python -m xauusd_bot --ai-preview --json
```

When both gates pass, JSON preview includes the exact sanitized payload and its hash.
When either gate rejects, preview lists the reasons and builds no payload. Preview
opens existing SQLite state read-only; missing state is a valid zero-usage baseline,
while corrupt, unreadable, locked, or structurally invalid state reports
`persistent_state_unavailable` and blocks readiness without repairing the file.

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
Responses API with Pydantic Structured Outputs. Each request explicitly uses
`store=False` and no conversation or previous-response identifier. The compact payload
defaults to the latest 30 completed M1 candles and 20 completed M5 candles.

Before any request, one atomic `BEGIN IMMEDIATE` SQLite transaction checks the call
limit and ensures that adding the configured per-call budget reserve would not cross
the daily or weekly cap. Only then does it reserve both the completed M1 candidate and
budget. The default reserve is $0.05. Known response cost replaces the reserve; a
timeout, failure, or crash with unknown usage retains it conservatively. The default
database is `.state/xauusd_bot.sqlite3`, which is ignored by Git. Desktop path
resolution is documented under Packaged configuration above. A unique
`(symbol, completed_m1_time)` key prevents duplicate spend across threads and process
restarts. Existing databases are migrated in place. `--usage` reports reported known
spend separately from conservative budget-accounted spend.

Default limits are 30 calls/day, $0.65 budget-accounted spend/day, and $5.00/week.
Pricing is centralized in `AIConfig`: $2.00/1M ordinary input tokens, $0.10/1M cached
input tokens, $2.50/1M cache-write tokens, and $10.00/1M output tokens. Reasoning
tokens are included in output-token billing and are not charged twice. Successful
responses use actual SDK-reported usage. Failed attempts with unknown usage retain
null token/cost fields, retain their budget reserve, and keep the candidate consumed.

See `.env.example` for every supported `XAUUSD_NEWS_*`, `OPENAI_*`, and state-path
override.

## Troubleshooting

- **Port 8765 is already in use:** stop the other local process and relaunch. The app
  fails clearly and never falls back to a LAN-facing bind address.
- **MT5 disconnected:** confirm the Terminal MCP endpoint/token and that the MT5
  terminal is running. The desktop stays alive, fails closed, and retries without
  starting OpenAI.
- **AI NOT_CONFIGURED:** set `OPENAI_API_KEY` only if explicit advisory commands are
  intended. Market monitoring works without it.
- **News or persistent state unavailable:** the corresponding gate remains blocked by
  design. Repair the source service/database rather than weakening the gate.
- **Frontend unavailable from source:** run `npm run build` in `frontend/`. CLI-only
  mode remains independent of frontend assets.
- **Packaged app cannot find configuration:** verify `.env` is beside the executable,
  not inside the repository build directory by assumption.
- **Packaged usage/advisory history is empty:** either copy the development database
  to the documented `%LOCALAPPDATA%` location while all app instances are stopped, or
  configure one absolute `XAUUSD_AI_STATE_DB` path in both environments. The app never
  searches for or overwrites another database implicitly.

## Development validation

```powershell
.venv\Scripts\python -m unittest discover -s tests -v
.venv\Scripts\python -m pip check

Set-Location frontend
npm run lint
npm run typecheck
npm test
npm run build
npm audit
```

## Planned stages

This release remains advisory-only. Future stages, each requiring deliberate review,
are paper outcome tracking, strategy evaluation, demo execution, and only eventually
live execution. None of those execution stages is implemented here.
