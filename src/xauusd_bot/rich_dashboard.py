from __future__ import annotations

from datetime import datetime
from typing import Any

from rich.columns import Columns
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .monitor import StateHub


def _flag(value: bool | None) -> Text:
    if value is True:
        return Text("✓ PASS", style="bold green")
    if value is False:
        return Text("✗ FAIL", style="bold red")
    return Text("—", style="dim")


def _value(value: Any, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.3f}{suffix}"
    return f"{value}{suffix}"


def _table(title: str) -> Table:
    table = Table(title=title, expand=True, show_header=False, box=None, padding=(0, 1))
    table.add_column(style="dim", width=22)
    table.add_column()
    return table


def render_dashboard(state: dict[str, Any]) -> Group:
    system = state["system"]
    connected = system["mt5_connected"]
    ai = state.get("ai") or {}
    header = Text.assemble(
        ("XAUUSD AI ENGINE", "bold gold1"),
        "   ADVISORY ONLY   ",
        ("MT5 CONNECTED" if connected else "MT5 DISCONNECTED", "bold green" if connected else "bold red"),
        "   ",
        (str(ai.get("state", "BLOCKED")), "bold cyan"),
    )
    panels: list[Any] = [Panel(header, border_style="gold1")]
    if not connected or not state.get("market"):
        reason = state["validity"]["reasons"][0]["message"]
        panels.append(Panel(reason, title="SYSTEM", border_style="red"))
    else:
        market = state["market"]
        gate = state["market_gate"]
        metrics = gate["metrics"]
        market_table = _table("MARKET")
        market_table.add_row("Bid / Ask", f"{market['bid']:.2f} / {market['ask']:.2f}")
        market_table.add_row("Spread", f"{market['spread_points']:.1f} pts / {market['spread_price']:.2f}")
        market_table.add_row("M1 / M5 Direction", f"{gate['direction_m1']} / {gate['direction_m5']}")
        market_table.add_row("Alignment", _flag(gate["directions_aligned"]))
        market_table.add_row("M1 Latest / Avg", f"{_value(market['m1_latest_range_points'])} / {_value(market['m1_average_range_points'])} pts")
        market_table.add_row("M5 Latest / Avg", f"{_value(market['m5_latest_range_points'])} / {_value(market['m5_average_range_points'])} pts")
        market_table.add_row("M1 Avg / Spike", f"{_value(metrics['m1_baseline_range_points'])} pts / {_value(metrics['m1_spike_ratio'])}x")
        market_table.add_row("M5 Avg / Spike", f"{_value(metrics['m5_baseline_range_points'])} pts / {_value(metrics['m5_spike_ratio'])}x")
        market_table.add_row("Quote / Tick Age", f"{_value(metrics['quote_age_seconds'])}s / {_value(metrics['tick_age_seconds'])}s")
        market_table.add_row("Recent ticks", str(market["recent_tick_count"]))

        gate_table = _table("MARKET GATE")
        for label, key in (
            ("Market active", "market_active"),
            ("Quote fresh", "quote_fresh"),
            ("Tick fresh", "tick_fresh"),
            ("Spread", "spread_acceptable"),
            ("Volatility", "volatility_acceptable"),
            ("History", "history_sufficient"),
            ("Tick history", "tick_history_sufficient"),
            ("Free margin", "free_margin_sufficient"),
        ):
            gate_table.add_row(label, _flag(gate[key]))
        gate_table.add_row("Existing position", _flag(not gate["existing_position"]))
        gate_table.add_row("M1/M5 alignment", _flag(gate["directions_aligned"]))
        gate_table.add_row("Eligible", _flag(state["validity"]["eligible"]))
        panels.append(Columns([Panel(market_table), Panel(gate_table)], expand=True))

        validity = state["validity"]
        lines = validity["valid_reasons"] if validity["eligible"] else [
            item["message"] for item in validity["reasons"]
        ]
        panels.append(
            Panel(
                "\n".join(f"{'✓' if validity['eligible'] else '✗'} {item}" for item in lines)
                or "No reasons available",
                title=f"VALIDITY — {validity['status']}",
                border_style="green" if validity["eligible"] else "red",
            )
        )

        news = state["news_gate"]
        news_table = _table("NEWS GATE")
        news_table.add_row("Calendar", _flag(news["calendar_available"]))
        news_table.add_row("Blackout active", _flag(not news["blackout_active"]))
        news_table.add_row("Safe for AI", _flag(news["safe_for_ai"]))
        news_table.add_row(
            "Nearest high news",
            (news.get("nearest_event") or {}).get("name", "None"),
        )
        news_table.add_row("Minutes to event", _value(news.get("minutes_to_nearest_event")))
        news_table.add_row(
            "Upcoming / recent",
            f"{len(news.get('upcoming_high_impact_events', []))} / {len(news.get('recent_high_impact_events', []))}",
        )

        last = ai.get("last_advisory")
        historical = bool(last and last.get("candidate_time") != ai.get("current_candidate_time"))
        advisory_table = _table("LAST AI ADVISORY — HISTORICAL" if historical else "LAST AI ADVISORY")
        if last:
            advisory_table.add_row("Model", _value(last.get("model")))
            advisory_table.add_row("Status", _value(last.get("status")))
            advisory_table.add_row("Candidate", _value(last.get("candidate_time")))
            advisory_table.add_row("Decision", _value(last.get("decision")))
            advisory_table.add_row("Confidence", _value(last.get("confidence"), "%"))
            advisory_table.add_row("Regime", _value(last.get("market_regime")))
            advisory_table.add_row("Setup", _value(last.get("setup_summary")))
            entry = last.get("entry_price")
            if entry is None and last.get("entry_zone_low") is not None:
                entry = f"{last['entry_zone_low']} – {last.get('entry_zone_high')}"
            advisory_table.add_row("Entry", _value(entry))
            advisory_table.add_row("Stop / target", f"{_value(last.get('stop_loss'))} / {_value(last.get('take_profit'))}")
            advisory_table.add_row("Risk / reward", _value(last.get("risk_reward_ratio")))
            advisory_table.add_row("Invalidation", _value(last.get("invalidation_reason")))
            if last.get("warnings"):
                advisory_table.add_row("Warnings", " | ".join(last["warnings"]))
        else:
            advisory_table.add_row("Status", "No persisted advisory")
        panels.append(Columns([Panel(news_table), Panel(advisory_table)], expand=True))

        usage = state.get("usage") or {}
        summary = usage.get("summary") or {}
        budget = usage.get("budget") or {}
        account = state["account"]
        footer = _table("OPENAI / ACCOUNT / SYSTEM")
        footer.add_row("Calls today / week", f"{summary.get('calls_today', '—')} / {summary.get('calls_this_week', '—')}")
        footer.add_row("Known / budget today", f"${summary.get('known_spend_today_usd', 0):.6f} / ${summary.get('budget_accounted_spend_today_usd', 0):.6f}" if summary else "—")
        footer.add_row("Budget week / cap", f"${summary.get('budget_accounted_spend_this_week_usd', 0):.6f} / ${budget.get('weekly_spend_cap_usd', 0):.2f}" if summary and budget else "—")
        if last:
            last_usage = last.get("usage") or {}
            footer.add_row("Last tokens in / out / cached", f"{_value(last_usage.get('input_tokens'))} / {_value(last_usage.get('output_tokens'))} / {_value(last_usage.get('cached_input_tokens'))}")
            if last.get("estimated_cost_usd") is not None and last.get("latency_ms") is not None:
                footer.add_row("Last cost / latency", f"${float(last['estimated_cost_usd']):.6f} / {float(last['latency_ms']) / 1000:.2f}s")
        footer.add_row("Balance / Equity", f"{account['balance']:.2f} / {account['equity']:.2f} {account['currency']}")
        footer.add_row("Free margin", f"{account['free_margin']:.2f} {account['currency']}")
        footer.add_row("XAUUSD position", f"{account['xauusd_position_count']} OPEN" if account["xauusd_position_exists"] else "NONE")
        footer.add_row("Application mode", _value(system.get("mode")))
        footer.add_row("Candidate", _value(system.get("candidate_time")))
        footer.add_row("Candidate hash", f"{system['candidate_hash'][:12]}…" if system.get("candidate_hash") else "—")
        footer.add_row("Persistent state", _flag(system.get("persistent_state_available")))
        footer.add_row("Uptime", f"{int(system.get('uptime_seconds', 0))} sec")
        footer.add_row("Last update", _value(system.get("last_refresh")))
        panels.append(Panel(footer))

    events = state.get("events", [])[-8:]
    event_lines = []
    for item in events:
        try:
            stamp = datetime.fromisoformat(item["timestamp"]).strftime("%H:%M:%S")
        except (KeyError, ValueError):
            stamp = "--:--:--"
        event_lines.append(f"{stamp} {item['level']:<9} {item['message']}")
    panels.append(Panel("\n".join(event_lines) or "Waiting for events…", title="RECENT EVENTS"))
    panels.append(Text("[WAITING] Monitoring XAUUSD…  Ctrl+C to exit", style="dim"))
    return Group(*panels)


async def run_rich_dashboard(hub: StateHub, stop_event: Any) -> None:
    revision = -1
    with Live(refresh_per_second=4, screen=False) as live:
        while not stop_event.is_set():
            try:
                revision, state = await asyncio_wait_state(hub, revision, stop_event)
            except TimeoutError:
                continue
            live.update(render_dashboard(state), refresh=True)


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
