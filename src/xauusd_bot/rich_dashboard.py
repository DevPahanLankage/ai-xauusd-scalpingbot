from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Iterable

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .monitor import StateHub


FULL_MIN_WIDTH = 104
FULL_MIN_HEIGHT = 28


def _value(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.3f}{suffix}"
    return f"{value}{suffix}"


def _duration(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    seconds = max(0, int(round(value)))
    minutes, remainder = divmod(seconds, 60)
    return f"{minutes}m {remainder}s" if minutes else f"{remainder}s"


def _line(value: Any, style: str | None = None) -> Text:
    return Text(str(value), style=style, overflow="ellipsis", no_wrap=True)


def _mark(value: bool | None) -> Text:
    if value is True:
        return Text("✓", style="bold green")
    if value is False:
        return Text("✗", style="bold red")
    return Text("—", style="dim")


def _flag(value: bool | None) -> Text:
    mark = _mark(value)
    label = " PASS" if value is True else " FAIL" if value is False else ""
    mark.append(label)
    return mark


def _marks(items: Iterable[tuple[str, bool | None]]) -> Text:
    result = Text(no_wrap=True, overflow="ellipsis")
    for index, (label, value) in enumerate(items):
        if index:
            result.append("  ")
        result.append(f"{label} ", style="dim")
        result.append_text(_mark(value))
    return result


def _table(*, label_width: int = 13) -> Table:
    table = Table.grid(expand=True, padding=(0, 1))
    table.add_column(
        style="dim",
        width=label_width,
        no_wrap=True,
        overflow="ellipsis",
    )
    table.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
    return table


def _panel(
    renderable: RenderableType,
    title: str,
    *,
    border_style: str = "blue",
) -> Panel:
    return Panel(
        renderable,
        title=title,
        border_style=border_style,
        padding=(0, 1),
    )


def _columns(renderables: list[RenderableType]) -> Table:
    columns = Table.grid(expand=True, padding=(0, 1))
    for _ in renderables:
        columns.add_column(ratio=1)
    columns.add_row(*renderables)
    return columns


def _header(state: dict[str, Any]) -> Panel:
    system = state["system"]
    connected = system["mt5_connected"]
    ai = state.get("ai") or {}
    auto = state.get("auto_advisory") or {}
    header = Text.assemble(
        ("XAUUSD AI ENGINE", "bold gold1"),
        "   ADVISORY ONLY   ",
        (
            "MT5 CONNECTED" if connected else "MT5 DISCONNECTED",
            "bold green" if connected else "bold red",
        ),
        "   ",
        (str(ai.get("state", "BLOCKED")), "bold cyan"),
        "   ",
        (f"AUTO {auto.get('state', 'OFF')}", "bold yellow" if auto.get("enabled") else "dim"),
    )
    return Panel(header, border_style="gold1", padding=(0, 1))


def _reason_lines(state: dict[str, Any], limit: int) -> Group:
    validity = state["validity"]
    values = (
        validity.get("valid_reasons", [])
        if validity["eligible"]
        else [item["message"] for item in validity.get("reasons", [])]
    )
    if not values:
        values = ["No reasons available"]
    visible = values[:limit]
    lines: list[Text] = []
    for value in visible:
        prefix = "✓ " if validity["eligible"] else "✗ "
        lines.append(
            _line(
                prefix + value,
                "green" if validity["eligible"] else "red",
            )
        )
    if len(values) > limit:
        lines[-1] = _line(f"… {len(values) - limit + 1} more reasons", "yellow")
    return Group(*lines)


def _events_panel(state: dict[str, Any], limit: int) -> Panel:
    events = state.get("events", [])[-limit:]
    lines: list[Text] = []
    for item in events:
        try:
            stamp = datetime.fromisoformat(item["timestamp"]).strftime("%H:%M:%S")
        except (KeyError, TypeError, ValueError):
            stamp = "--:--:--"
        lines.append(_line(f"{stamp} {item['level']:<9} {item['message']}"))
    return _panel(
        Group(*lines) if lines else _line("Waiting for events…", "dim"),
        "RECENT EVENTS",
    )


def _disconnected_dashboard(state: dict[str, Any], height: int) -> Group:
    reason = state["validity"]["reasons"][0]["message"]
    event_limit = max(1, min(5, height - 8))
    return Group(
        _header(state),
        _panel(_line(reason, "bold red"), "SYSTEM", border_style="red"),
        _events_panel(state, event_limit),
        Text("[WAITING] Reconnecting to MT5…  Ctrl+C to exit", style="dim"),
    )


def _full_dashboard(state: dict[str, Any], height: int) -> Group:
    market = state["market"]
    gate = state["market_gate"]
    metrics = gate["metrics"]
    validity = state["validity"]
    news = state["news_gate"]
    ai = state.get("ai") or {}
    auto = state.get("auto_advisory") or {}
    last = ai.get("last_advisory")
    usage = state.get("usage") or {}
    paper_stats = state.get("paper_stats") or {}
    summary = usage.get("summary") or {}
    budget = usage.get("budget") or {}
    account = state["account"]
    paper = state.get("paper") or {}
    system = state["system"]

    market_table = _table()
    market_table.add_row("Bid / Ask", _line(f"{market['bid']:.2f} / {market['ask']:.2f}"))
    market_table.add_row(
        "Spread",
        _line(f"{market['spread_points']:.1f} pts / {market['spread_price']:.2f}"),
    )
    market_table.add_row(
        "M1 / M5",
        _line(
            f"{gate['direction_m1']} / {gate['direction_m5']}  | "
            f"aligned {_value(gate['directions_aligned'])}"
        ),
    )
    market_table.add_row(
        "M1 rng/avg/spk",
        _line(
            f"{_value(market['m1_latest_range_points'])} / "
            f"{_value(market['m1_average_range_points'])} / "
            f"{_value(metrics['m1_spike_ratio'])}x"
        ),
    )
    market_table.add_row(
        "M5 rng/avg/spk",
        _line(
            f"{_value(market['m5_latest_range_points'])} / "
            f"{_value(market['m5_average_range_points'])} / "
            f"{_value(metrics['m5_spike_ratio'])}x"
        ),
    )
    market_table.add_row(
        "Quote/tick/ticks",
        _line(
            f"{_value(metrics['quote_age_seconds'])}s / "
            f"{_value(metrics['tick_age_seconds'])}s / {market['recent_tick_count']}"
        ),
    )

    gate_table = _table(label_width=14)
    gate_table.add_row(
        "Market/Q/T",
        _marks(
            (
                ("M", gate["market_active"]),
                ("Q", gate["quote_fresh"]),
                ("T", gate["tick_fresh"]),
            )
        ),
    )
    gate_table.add_row(
        "Spread / vol",
        _marks(
            (
                ("S", gate["spread_acceptable"]),
                ("V", gate["volatility_acceptable"]),
            )
        ),
    )
    gate_table.add_row(
        "History / ticks",
        _marks(
            (
                ("H", gate["history_sufficient"]),
                ("T", gate["tick_history_sufficient"]),
            )
        ),
    )
    gate_table.add_row(
        "Margin / no pos",
        _marks(
            (
                ("$", gate["free_margin_sufficient"]),
                ("P", not gate["existing_position"]),
            )
        ),
    )
    gate_table.add_row("Alignment", _flag(gate["directions_aligned"]))
    gate_table.add_row("Eligible", _flag(validity["eligible"]))

    top = _columns(
        [
            _panel(market_table, "MARKET"),
            _panel(gate_table, "MARKET GATE"),
            _panel(
                _reason_lines(state, 6),
                f"VALIDITY — {validity['status']}",
                border_style="green" if validity["eligible"] else "red",
            ),
        ]
    )

    news_table = _table(label_width=12)
    news_table.add_row(
        "Calendar/blk",
        _marks(
            (
                ("C", news["calendar_available"]),
                ("B", not news["blackout_active"]),
            )
        ),
    )
    news_table.add_row("Safe for AI", _flag(news["safe_for_ai"]))
    news_table.add_row(
        "Nearest high",
        _line((news.get("nearest_event") or {}).get("name", "None")),
    )
    news_table.add_row(
        "Min / up / rec",
        _line(
            f"{_value(news.get('minutes_to_nearest_event'))} / "
            f"{len(news.get('upcoming_high_impact_events', []))} / "
            f"{len(news.get('recent_high_impact_events', []))}"
        ),
    )

    account_table = _table(label_width=12)
    account_table.add_row(
        "Balance/equity",
        _line(
            f"{account['balance']:.2f} / {account['equity']:.2f} "
            f"{account['currency']}"
        ),
    )
    account_table.add_row(
        "Free margin",
        _line(f"{account['free_margin']:.2f} {account['currency']}"),
    )
    account_table.add_row(
        "Pos / paper",
        _line(
            (f"{account['xauusd_position_count']} OPEN"
            if account["xauusd_position_exists"]
            else "NONE")
            + f" / {paper.get('status', 'NONE')}"
        ),
    )

    advisory_table = _table(label_width=12)
    historical = bool(
        last and last.get("candidate_time") != ai.get("current_candidate_time")
    )
    if last:
        advisory_table.add_row(
            "Decision/status",
            _line(f"{_value(last.get('decision'))} / {_value(last.get('status'))}"),
        )
        advisory_table.add_row(
            "Conf / regime",
            _line(
                f"{_value(last.get('confidence'), '%')} / "
                f"{_value(last.get('market_regime'))}"
            ),
        )
        advisory_table.add_row(
            "Model/candidate",
            _line(f"{_value(last.get('model'))} / {_value(last.get('candidate_time'))}"),
        )
        advisory_table.add_row("Setup", _line(_value(last.get("setup_summary"))))
        entry = last.get("entry_price")
        if entry is None and last.get("entry_zone_low") is not None:
            entry = f"{last['entry_zone_low']} – {last.get('entry_zone_high')}"
        advisory_table.add_row("Entry", _line(_value(entry)))
        advisory_table.add_row(
            "SL / TP",
            _line(
                f"{_value(last.get('stop_loss'))} / "
                f"{_value(last.get('take_profit'))}"
            ),
        )
        advisory_table.add_row(
            "Risk / reward", _line(_value(last.get("risk_reward_ratio")))
        )
        advisory_table.add_row(
            "Invalidation", _line(_value(last.get("invalidation_reason")))
        )
        advisory_table.add_row(
            "Warnings",
            _line(" | ".join(last.get("warnings") or []) or "None"),
        )
    else:
        advisory_table.add_row("Status", _line("No persisted advisory"))

    usage_table = _table(label_width=13)
    usage_table.add_row(
        "Calls day/week",
        _line(f"{summary.get('calls_today', '—')} / {summary.get('calls_this_week', '—')}"),
    )
    usage_table.add_row(
        "Spend today",
        _line(
            f"${summary.get('known_spend_today_usd', 0):.6f} / "
            f"${summary.get('budget_accounted_spend_today_usd', 0):.6f}"
            if summary
            else "—"
        ),
    )
    usage_table.add_row(
        "Week / cap",
        _line(
            f"${summary.get('budget_accounted_spend_this_week_usd', 0):.6f} / "
            f"${budget.get('weekly_spend_cap_usd', 0):.2f}"
            if summary and budget
            else "—"
        ),
    )
    last_usage = (last or {}).get("usage") or {}
    last_cost = (last or {}).get("estimated_cost_usd")
    latency = (last or {}).get("latency_ms")
    usage_table.add_row(
        "Last in/out/cache",
        _line(
            f"{_value(last_usage.get('input_tokens'))}/"
            f"{_value(last_usage.get('output_tokens'))}/"
            f"{_value(last_usage.get('cached_input_tokens'))}"
        ),
    )
    usage_table.add_row(
        "Last cost / sec",
        _line(
            f"{'—' if last_cost is None else f'${float(last_cost):.6f}'} / "
            f"{'—' if latency is None else f'{float(latency) / 1000:.2f}'} | "
            f"paper W/L {paper_stats.get('wins', 0)}/{paper_stats.get('losses', 0)} "
            f"R {_value(paper_stats.get('cumulative_r'))}"
        ),
    )

    persistent_state = system.get("persistent_state_available")
    persistent_label = "—"
    if persistent_state is True:
        persistent_label = "OK"
    elif persistent_state is False:
        persistent_label = "FAIL"
    system_table = _table(label_width=13)
    system_table.add_row(
        "Mode / uptime",
        _line(
            f"{_value(system.get('mode'))} / "
            f"{int(system.get('uptime_seconds', 0))}s | "
            f"AI {_value(auto.get('min_interval_minutes'), 'm')} "
            f"next {_duration(auto.get('seconds_until_eligible'))}"
        ),
    )
    system_table.add_row("Candidate", _line(_value(system.get("candidate_time"))))
    system_table.add_row(
        "Hash / state",
        _line(
            f"{system['candidate_hash'][:10] + '…' if system.get('candidate_hash') else '—'} / "
            f"{persistent_label}"
        ),
    )
    system_table.add_row("Last update", _line(_value(system.get("last_refresh"))))

    bottom = _columns(
        [
            Group(
                _panel(news_table, "NEWS GATE"),
                _panel(account_table, "ACCOUNT"),
            ),
            _panel(
                advisory_table,
                "LAST AI ADVISORY — HISTORICAL" if historical else "LAST AI ADVISORY",
            ),
            Group(
                _panel(usage_table, "OPENAI / BUDGET"),
                _panel(system_table, "SYSTEM"),
            ),
        ]
    )

    event_limit = max(2, min(5, height - 27))
    return Group(
        _header(state),
        top,
        bottom,
        _events_panel(state, event_limit),
        Text("[WAITING] Monitoring XAUUSD…  Ctrl+C to exit", style="dim"),
    )


def _compact_dashboard(state: dict[str, Any], height: int) -> Group:
    market = state.get("market") or {}
    gate = state.get("market_gate") or {}
    validity = state["validity"]
    news = state.get("news_gate") or {}
    ai = state.get("ai") or {}
    auto = state.get("auto_advisory") or {}
    paper = state.get("paper") or {}
    last = ai.get("last_advisory") or {}

    summary = _table(label_width=15)
    summary.add_row(
        "MT5 / AI",
        _line(
            f"{'CONNECTED' if state['system']['mt5_connected'] else 'DISCONNECTED'} / "
            f"{ai.get('state', 'BLOCKED')} / AUTO {auto.get('state', 'OFF')}"
        ),
    )
    summary.add_row(
        "Eligible / news",
        _marks(
            (
                ("AI", validity["eligible"]),
                ("N", news.get("safe_for_ai")),
            )
        ),
    )
    summary.add_row(
        "Bid / ask", _line(f"{market.get('bid', '—')} / {market.get('ask', '—')}")
    )
    summary.add_row(
        "Spread", _line(f"{_value(market.get('spread_points'))} pts")
    )
    summary.add_row(
        "M1 / M5 / align",
        _line(
            f"{gate.get('direction_m1', '—')} / {gate.get('direction_m5', '—')} / "
            f"{_value(gate.get('directions_aligned'))}"
        ),
    )
    reasons = [item["message"] for item in validity.get("reasons", [])]
    summary.add_row(
        "Decision reason",
        _line(" | ".join(reasons) or "All deterministic checks passed"),
    )
    summary.add_row(
        "Last decision",
        _line(
            f"{last.get('decision', 'None')} / "
            f"{_value(last.get('confidence'), '%')}"
        ),
    )
    summary.add_row(
        "Paper",
        _line(f"{paper.get('decision', 'None')} / {paper.get('status', 'NONE')} / {_value(paper.get('final_r'))}R"),
    )
    summary.add_row(
        "Entry / SL / TP / RR",
        _line(
            f"{_value(last.get('entry_price'))} / {_value(last.get('stop_loss'))} / "
            f"{_value(last.get('take_profit'))} / "
            f"{_value(last.get('risk_reward_ratio'))}"
        ),
    )
    event_limit = max(1, min(3, height - 17))
    return Group(
        _header(state),
        _panel(summary, "SAFETY SUMMARY", border_style="yellow"),
        _events_panel(state, event_limit),
        Text(
            "Terminal too small — enlarge for full diagnostics  |  Ctrl+C to exit",
            style="bold yellow",
        ),
    )


def _minimal_dashboard(state: dict[str, Any], height: int) -> Group:
    market = state.get("market") or {}
    gate = state.get("market_gate") or {}
    validity = state["validity"]
    news = state.get("news_gate") or {}
    ai = state.get("ai") or {}
    auto = state.get("auto_advisory") or {}
    paper = state.get("paper") or {}
    last = ai.get("last_advisory") or {}
    reasons = [item["message"] for item in validity.get("reasons", [])]
    lines = [
        _line(
            f"XAUUSD AI | MT5 {'UP' if state['system']['mt5_connected'] else 'DOWN'} | "
            f"AI {ai.get('state', 'BLOCKED')} | AUTO {auto.get('state', 'OFF')} | eligible {validity['eligible']}",
            "bold gold1",
        ),
        _line(
            f"Bid/Ask {market.get('bid', '—')}/{market.get('ask', '—')} | "
            f"spread {_value(market.get('spread_points'))} pts"
        ),
        _line(
            f"M1/M5 {gate.get('direction_m1', '—')}/{gate.get('direction_m5', '—')} | "
            f"aligned {_value(gate.get('directions_aligned'))}"
        ),
        _line(f"Reason: {' | '.join(reasons) or 'All deterministic checks passed'}"),
        _line(f"News safe: {_value(news.get('safe_for_ai'))}"),
        _line(
            f"Last: {last.get('decision', 'None')} | "
            f"confidence {_value(last.get('confidence'), '%')} | "
            f"RR {_value(last.get('risk_reward_ratio'))}"
        ),
        _line(f"Paper: {paper.get('decision', 'None')} / {paper.get('status', 'NONE')} / {_value(paper.get('final_r'))}R"),
        _line("Terminal too small — enlarge for full diagnostics", "bold yellow"),
        _line("[WAITING] Monitoring XAUUSD…  Ctrl+C to exit", "dim"),
    ]
    return Group(*lines[: max(1, height)])


def render_dashboard(
    state: dict[str, Any],
    *,
    width: int = 120,
    height: int = 30,
) -> Group:
    if not state["system"]["mt5_connected"] or not state.get("market"):
        return _disconnected_dashboard(state, height)
    if height < 17:
        return _minimal_dashboard(state, height)
    if width < FULL_MIN_WIDTH or height < FULL_MIN_HEIGHT:
        return _compact_dashboard(state, height)
    return _full_dashboard(state, height)


def _sync_console_dimensions(console: Console):
    """Read the rebound CONOUT$ descriptor used by the windowed executable."""

    try:
        size = os.get_terminal_size(console.file.fileno())
    except (AttributeError, OSError, TypeError, ValueError):
        return console.size
    if size.columns > 0:
        console.width = size.columns
    if size.lines > 0:
        console.height = size.lines
    return console.size


async def run_rich_dashboard(
    hub: StateHub,
    stop_event: Any,
    *,
    force_terminal: bool | None = None,
) -> None:
    revision = -1
    rich_environment = None
    if force_terminal:
        rich_environment = dict(os.environ)
        # A verified Windows VT console is interactive even when the parent
        # process supplied TERM=dumb (common in launchers and test harnesses).
        rich_environment.pop("TERM", None)
    console = Console(
        force_terminal=force_terminal,
        legacy_windows=False if force_terminal else None,
        _environ=rich_environment,
    )
    _sync_console_dimensions(console)
    with Live(
        console=console,
        screen=console.is_terminal,
        auto_refresh=False,
        vertical_overflow="crop",
    ) as live:
        while not stop_event.is_set():
            try:
                revision, state = await asyncio_wait_state(hub, revision, stop_event)
            except TimeoutError:
                continue
            size = _sync_console_dimensions(console)
            live.update(
                render_dashboard(state, width=size.width, height=size.height),
                refresh=True,
            )


async def asyncio_wait_state(hub: StateHub, revision: int, stop_event: Any):
    import asyncio

    state_task = asyncio.create_task(
        hub.snapshot() if revision < 0 else hub.wait_after(revision)
    )
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        {state_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    if stop_task in done:
        raise TimeoutError
    return state_task.result()
