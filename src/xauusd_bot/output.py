from __future__ import annotations

import json

from .advisory_service import AIAdvisoryOutcome
from .economic_calendar import EconomicNewsGateResult
from .models import MarketGateResult, XAUUSDMarketSnapshot
from .preview import AIPreviewResult


def to_json(snapshot: XAUUSDMarketSnapshot, gate: MarketGateResult) -> str:
    return json.dumps(
        {"snapshot": snapshot.to_dict(), "market_gate": gate.to_dict()},
        indent=2,
        ensure_ascii=False,
    )


def to_human(snapshot: XAUUSDMarketSnapshot, gate: MarketGateResult) -> str:
    symbol = snapshot.symbol
    account = snapshot.account
    metrics = snapshot.metrics
    price_format = f".{{digits}}f".format(digits=symbol.digits)

    def price(value: float) -> str:
        return format(value, price_format)

    lines = [
        "XAUUSD Market Snapshot (read-only)",
        "=" * 36,
        f"Broker symbol       : {symbol.symbol} ({symbol.description})",
        f"Trade-server time   : {snapshot.trade_server_time}",
        f"Quote time          : {symbol.quote_time or 'unavailable'}",
        f"Bid / Ask           : {price(symbol.bid)} / {price(symbol.ask)}",
        (
            f"Spread              : {price(metrics.spread_price)} "
            f"({metrics.spread_points:.1f} points)"
        ),
        f"Point / Digits      : {symbol.point:g} / {symbol.digits}",
        (
            f"Lot min/max/step    : {symbol.volume_min:g} / "
            f"{symbol.volume_max:g} / {symbol.volume_step:g}"
        ),
        (
            f"Stops / Freeze      : {symbol.trade_stops_level} / "
            f"{symbol.trade_freeze_level} points"
        ),
        (
            f"Trade specification : {symbol.trade_mode}, {symbol.execution_mode}, "
            f"{symbol.calculation_mode}"
        ),
        "",
        f"Account             : {account.server} {account.account_type} ({account.currency})",
        f"Balance / Equity    : {account.balance:.2f} / {account.equity:.2f}",
        f"Free / Used margin  : {account.free_margin:.2f} / {account.used_margin:.2f}",
        f"Floating P/L        : {account.floating_profit:.2f}",
        f"Open {symbol.symbol} positions: {len(snapshot.positions)}",
    ]
    for position in snapshot.positions:
        lines.append(
            "  - "
            f"#{position.position_id} {position.side.upper()} {position.volume:g} lots, "
            f"open {price(position.price_open)}, last {price(position.price_last)}, "
            f"P/L {position.profit:.2f}"
        )

    lines.extend(
        [
            "",
            f"M1 candles          : {len(snapshot.m1_candles)} (latest {snapshot.m1_candles[-1].time})",
            (
                f"M1 latest range     : {price(metrics.m1_latest_range)} "
                f"({metrics.m1_latest_range_points:.1f} points)"
            ),
            (
                f"M1 average range    : {price(metrics.m1_average_range)} "
                f"({metrics.m1_average_range_points:.1f} points, "
                f"last {metrics.average_range_bars} bars)"
            ),
            f"M5 candles          : {len(snapshot.m5_candles)} (latest {snapshot.m5_candles[-1].time})",
            (
                f"M5 latest range     : {price(metrics.m5_latest_range)} "
                f"({metrics.m5_latest_range_points:.1f} points)"
            ),
            (
                f"M5 average range    : {price(metrics.m5_average_range)} "
                f"({metrics.m5_average_range_points:.1f} points, "
                f"last {metrics.average_range_bars} bars)"
            ),
            (
                f"Short-term direction: {metrics.short_term_direction} "
                f"({metrics.direction_price_change:+.{symbol.digits}f} over "
                f"{metrics.direction_lookback_bars} M1 bars)"
            ),
            f"Recent ticks        : {len(snapshot.recent_ticks)}",
        ]
    )
    if snapshot.recent_ticks:
        latest_tick = snapshot.recent_ticks[-1]
        lines.append(
            f"Latest tick         : {latest_tick.time}, "
            f"{price(latest_tick.bid)} / {price(latest_tick.ask)}"
        )
    else:
        lines.append("Latest tick         : unavailable in configured lookback")
    lines.extend(
        [
            "",
            "Market Gate (deterministic, fail-closed)",
            "=" * 40,
            f"Eligible for AI     : {str(gate.eligible_for_ai).lower()}",
            f"Market active       : {str(gate.market_active).lower()}",
            f"Quote / tick fresh  : {str(gate.quote_fresh).lower()} / {str(gate.tick_fresh).lower()}",
            f"Spread acceptable   : {str(gate.spread_acceptable).lower()}",
            f"Volatility acceptable: {str(gate.volatility_acceptable).lower()}",
            f"History sufficient  : {str(gate.history_sufficient).lower()}",
            f"Tick history enough : {str(gate.tick_history_sufficient).lower()}",
            f"Free margin enough  : {str(gate.free_margin_sufficient).lower()}",
            f"Direction M1 / M5   : {gate.direction_m1} / {gate.direction_m5}",
            f"Directions aligned  : {str(gate.directions_aligned).lower()}",
            f"Existing position   : {str(gate.existing_position).lower()}",
            f"Completed M1        : {gate.completed_m1_time or 'unavailable'}",
        ]
    )
    if gate.rejection_reasons:
        lines.append("Rejection reasons   :")
        lines.extend(f"  - {reason}" for reason in gate.rejection_reasons)
    else:
        lines.append("Rejection reasons   : none")
    return "\n".join(lines)


def advisory_to_json(
    snapshot: XAUUSDMarketSnapshot,
    market_gate: MarketGateResult,
    news_gate: EconomicNewsGateResult,
    outcome: AIAdvisoryOutcome,
) -> str:
    return json.dumps(
        {
            "snapshot": snapshot.to_dict(),
            "market_gate": market_gate.to_dict(),
            "news_gate": news_gate.to_dict(),
            "ai_advisory": {
                "attempted": outcome.attempted,
                "skip_reason": outcome.skip_reason,
                "input_hash": outcome.input_hash,
                "result": outcome.result.to_dict() if outcome.result else None,
            },
        },
        indent=2,
        ensure_ascii=False,
    )


def advisory_to_human(
    snapshot: XAUUSDMarketSnapshot,
    market_gate: MarketGateResult,
    news_gate: EconomicNewsGateResult,
    outcome: AIAdvisoryOutcome,
) -> str:
    lines = [to_human(snapshot, market_gate), "", "Economic News Gate (USD, fail-closed)", "=" * 40]
    lines.extend(
        [
            f"Calendar available  : {str(news_gate.calendar_available).lower()}",
            f"Safe for AI         : {str(news_gate.safe_for_ai).lower()}",
            f"Blackout active     : {str(news_gate.blackout_active).lower()}",
            (
                "Nearest high impact : none in configured window"
                if news_gate.nearest_event is None
                else f"{news_gate.nearest_event.name} "
                f"({news_gate.minutes_to_nearest_event:+.1f} minutes)"
            ),
        ]
    )
    if news_gate.rejection_reasons:
        lines.append("News rejection      : " + ", ".join(news_gate.rejection_reasons))
    lines.extend(["", "OpenAI Advisory (no execution)", "=" * 31])
    if not outcome.attempted or outcome.result is None:
        lines.append(f"AI request          : skipped ({outcome.skip_reason})")
    else:
        decision = outcome.result.decision
        lines.extend(
            [
                "AI request          : attempted",
                f"Status              : {outcome.result.status}",
                f"Decision            : {decision.decision.value}",
                f"Confidence          : {decision.confidence}",
                f"Setup               : {decision.setup_summary}",
                f"Entry / SL / TP     : {decision.entry_price} / {decision.stop_loss} / {decision.take_profit}",
                f"Risk/reward         : {decision.risk_reward_ratio}",
                f"Estimated cost USD  : {outcome.result.estimated_cost_usd}",
            ]
        )
    return "\n".join(lines)


def preview_to_json(preview: AIPreviewResult) -> str:
    """Render only sanitised preview state; never include the account snapshot."""

    return json.dumps(
        {"ai_preview": preview.to_dict(include_payload=True)},
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )


def preview_to_human(preview: AIPreviewResult) -> str:
    usage = preview.usage
    budget = preview.budget
    lines = [
        "OpenAI Advisory Preview (zero API calls / zero state writes)",
        "=" * 57,
        f"Gold symbol         : {preview.symbol}",
        f"Completed M1        : {preview.completed_m1_time or 'unavailable'}",
        f"MarketGate eligible : {str(preview.market_gate_eligible).lower()}",
        f"NewsGate safe       : {str(preview.news_gate_safe).lower()}",
        f"Candidate consumed  : {str(preview.candidate_consumed).lower()}",
        f"Model / effort      : {preview.model} / {preview.reasoning_effort}",
        f"Credentials set     : {str(preview.credentials_configured).lower()}",
        f"Payload hash        : {preview.input_hash or 'not built'}",
        f"Would request       : {str(preview.would_request).lower()}",
        "",
        "Configured budget (read-only)",
        "=" * 29,
        f"Calls today         : {usage.calls_today} / {budget.max_calls_per_day}",
        f"Known spend today   : ${usage.known_spend_today_usd:.6f}",
        (
            "Budget spend today  : "
            f"${usage.budget_accounted_spend_today_usd:.6f} / "
            f"${budget.daily_spend_cap_usd:.6f}"
        ),
        f"Calls this week     : {usage.calls_this_week}",
        f"Known spend week    : ${usage.known_spend_this_week_usd:.6f}",
        (
            "Budget spend week   : "
            f"${usage.budget_accounted_spend_this_week_usd:.6f} / "
            f"${budget.weekly_spend_cap_usd:.6f}"
        ),
        f"Per-call reserve    : ${budget.reserve_per_call_usd:.6f}",
    ]
    if preview.market_gate_reasons:
        lines.append("Market gate reasons : " + ", ".join(preview.market_gate_reasons))
    if preview.news_gate_reasons:
        lines.append("News gate reasons   : " + ", ".join(preview.news_gate_reasons))
    if preview.skip_reasons:
        lines.append("Paid call skipped   : " + ", ".join(preview.skip_reasons))
    else:
        lines.append("Paid call skipped   : no (preview still makes no request)")
    return "\n".join(lines)
