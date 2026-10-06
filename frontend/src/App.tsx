import { useEffect, useMemo, useRef, useState } from "react";
import { ColorType, createChart, LineStyle, type IChartApi, type IPriceLine, type ISeriesApi, type UTCTimestamp } from "lightweight-charts";
import { acceptIncomingState, advisoryIsHistorical } from "./state";
import type { ApplicationState, Candle } from "./types";

const money = (value: unknown, digits = 2) => typeof value === "number" ? `$${value.toFixed(digits)}` : "—";
const number = (value: unknown, digits = 2) => typeof value === "number" ? value.toFixed(digits) : "—";
const text = (value: unknown) => value === null || value === undefined || value === "" ? "—" : String(value);
const shortTime = (value: unknown) => value ? new Date(String(value)).toLocaleTimeString() : "—";

function useLiveState() {
  const [state, setState] = useState<ApplicationState | null>(null);
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    let socket: WebSocket | null = null;
    let timer = 0;
    let stopped = false;
    const connect = () => {
      const scheme = location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${location.host}/ws`);
      socket.onopen = () => setConnected(true);
      socket.onmessage = (event) => {
        const incoming = JSON.parse(event.data) as ApplicationState;
        setState((current) => acceptIncomingState(current, incoming));
      };
      socket.onclose = () => {
        setConnected(false);
        if (!stopped) timer = window.setTimeout(connect, 1500);
      };
    };
    connect();
    return () => {
      stopped = true;
      clearTimeout(timer);
      socket?.close();
    };
  }, []);
  return { state, connected };
}

function Status({ value, invert = false }: { value: boolean | null | undefined; invert?: boolean }) {
  const pass = value === null || value === undefined ? null : invert ? !value : value;
  return <span className={`status ${pass === null ? "unknown" : pass ? "pass" : "fail"}`}>{pass === null ? "—" : pass ? "✓" : "✕"}</span>;
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return <div className="data-row"><span>{label}</span><strong>{children}</strong></div>;
}

function Panel({ title, className = "", children }: { title: string; className?: string; children: React.ReactNode }) {
  return <section className={`panel ${className}`}><h2>{title}</h2>{children}</section>;
}

function CandleChart({ state }: { state: ApplicationState }) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const priceLines = useRef<IPriceLine[]>([]);
  const signature = useRef("");
  const [timeframe, setTimeframe] = useState<"m1" | "m5">("m1");
  const candles = state.chart[timeframe];
  useEffect(() => {
    if (!container.current) return;
    const instance = createChart(container.current, {
      height: 330,
      layout: { background: { type: ColorType.Solid, color: "#0d1016" }, textColor: "#8f99aa" },
      grid: { vertLines: { color: "#171c25" }, horzLines: { color: "#171c25" } },
      rightPriceScale: { borderColor: "#28303c" },
      timeScale: { borderColor: "#28303c", timeVisible: true },
    });
    const candleSeries = instance.addCandlestickSeries({ upColor: "#32c48d", downColor: "#ef5f64", wickUpColor: "#32c48d", wickDownColor: "#ef5f64", borderVisible: false });
    chart.current = instance;
    series.current = candleSeries;
    const observer = new ResizeObserver(([entry]) => instance.applyOptions({ width: entry.contentRect.width }));
    observer.observe(container.current);
    return () => { observer.disconnect(); instance.remove(); };
  }, []);
  useEffect(() => {
    const nextSignature = `${timeframe}:${candles.length}:${candles.at(-1)?.time ?? ""}`;
    if (!series.current || signature.current === nextSignature) return;
    signature.current = nextSignature;
    const data = candles.flatMap((item: Candle) => {
      const parsed = Date.parse(item.time);
      return Number.isFinite(parsed) ? [{ ...item, time: Math.floor(parsed / 1000) as UTCTimestamp }] : [];
    });
    series.current.setData(data);
    chart.current?.timeScale().fitContent();
  }, [candles, timeframe]);
  useEffect(() => {
    const candleSeries = series.current;
    if (!candleSeries) return;
    priceLines.current.forEach((line) => candleSeries.removePriceLine(line));
    priceLines.current = [];
    const add = (value: unknown, title: string, color: string, style = LineStyle.Solid) => {
      if (typeof value !== "number" || !Number.isFinite(value)) return;
      priceLines.current.push(candleSeries.createPriceLine({
        price: value,
        title,
        color,
        lineWidth: 1,
        lineStyle: style,
        axisLabelVisible: true,
      }));
    };
    add(state.market?.bid, "BID", "#4bb4ff", LineStyle.Dashed);
    add(state.market?.ask, "ASK", "#d5aa4f", LineStyle.Dashed);
    const advisory = state.ai.last_advisory;
    if (advisory && advisory.decision !== "NO_TRADE") {
      add(advisory.entry_price, "ENTRY", "#e7c969");
      add(advisory.entry_zone_low, "ENTRY LOW", "#e7c969", LineStyle.Dotted);
      add(advisory.entry_zone_high, "ENTRY HIGH", "#e7c969", LineStyle.Dotted);
      add(advisory.stop_loss, "SL", "#ef5f64");
      add(advisory.take_profit, "TP", "#32c48d");
    }
  }, [state.market?.bid, state.market?.ask, state.ai.last_advisory]);
  return <Panel title="PRICE STRUCTURE" className="chart-panel"><div className="chart-tools"><button className={timeframe === "m1" ? "active" : ""} onClick={() => setTimeframe("m1")}>M1</button><button className={timeframe === "m5" ? "active" : ""} onClick={() => setTimeframe("m5")}>M5</button></div><div ref={container} className="chart" /></Panel>;
}

function CalendarEvents({ title, events }: { title: string; events: Array<Record<string, unknown>> }) {
  return <div className="calendar-list"><small>{title}</small>{events.slice(0, 3).map((event, index) => <div key={`${text(event.value_id)}-${index}`}><span>{text(event.name)}</span><b>{number(event.minutes_to_event, 0)} min</b></div>)}</div>;
}

function App() {
  const { state, connected } = useLiveState();
  const historical = useMemo(() => state ? advisoryIsHistorical(state) : false, [state]);
  if (!state) return <main className="loading"><div className="gold-mark">XAU</div><p>Connecting to local advisory engine…</p></main>;
  const gate = state.market_gate;
  const market = state.market;
  const news = state.news_gate;
  const last = state.ai.last_advisory;
  const summary = state.usage?.summary;
  const budget = state.usage?.budget;
  const weeklySpend = Number(summary?.budget_accounted_spend_this_week_usd ?? 0);
  const weeklyCap = Number(budget?.weekly_spend_cap_usd ?? 5);
  const budgetPct = Math.min(100, weeklyCap > 0 ? (weeklySpend / weeklyCap) * 100 : 0);
  const exit = async () => {
    if (!window.confirm("Exit XAUUSD AI?")) return;
    await fetch("/api/exit", { method: "POST" });
  };
  return <main>
    <header className="topbar">
      <div><span className="symbol">XAUUSD</span><span className="advisory">ADVISORY ONLY</span></div>
      <div className="top-status"><span className={state.system.mt5_connected ? "green" : "red"}>● MT5 {state.system.mt5_connected ? "CONNECTED" : "DISCONNECTED"}</span><span className={connected ? "green" : "amber"}>● LIVE {connected ? "CONNECTED" : "RECONNECTING"}</span><span className="ai-state">AI {state.ai.state}</span><span>{text(state.system.trade_server_time)}</span><button className="exit" onClick={exit}>Exit Application</button></div>
    </header>

    {!market || !gate ? <Panel title="ENGINE STATUS" className="blocked"><h3>MT5 DISCONNECTED</h3><p>{state.validity.reasons[0]?.message}</p><p>The application will reconnect automatically.</p></Panel> : <>
      <div className="grid top-grid">
        <Panel title="MARKET" className="market-panel">
          <div className="quote"><div><small>BID</small><b>{market.bid.toFixed(market.digits)}</b></div><div><small>ASK</small><b>{market.ask.toFixed(market.digits)}</b></div></div>
          <Row label="Spread"><span>{market.spread_points.toFixed(1)} pts / {market.spread_price.toFixed(market.digits)} <Status value={gate.spread_acceptable} /></span></Row>
          <Row label="M1 / M5 Direction">{gate.direction_m1} / {gate.direction_m5}</Row>
          <Row label="Alignment"><span>{gate.directions_aligned ? "YES" : "NO"} <Status value={gate.directions_aligned} /></span></Row>
          <Row label="M1 Range / Avg">{number(market.m1_latest_range_points, 1)} / {number(market.m1_average_range_points, 1)} pts</Row>
          <Row label="M5 Range / Avg">{number(market.m5_latest_range_points, 1)} / {number(market.m5_average_range_points, 1)} pts</Row>
          <Row label="M1 / M5 Spike">{number(gate.metrics.m1_spike_ratio)}x / {number(gate.metrics.m5_spike_ratio)}x</Row>
          <Row label="Quote / Tick Age">{number(gate.metrics.quote_age_seconds)}s / {number(gate.metrics.tick_age_seconds)}s</Row>
          <Row label="Recent Ticks">{market.recent_tick_count}</Row>
        </Panel>
        <Panel title="MARKET GATE">
          {([ ["Market active", "market_active"], ["Quote fresh", "quote_fresh"], ["Tick fresh", "tick_fresh"], ["Spread", "spread_acceptable"], ["Volatility", "volatility_acceptable"], ["History", "history_sufficient"], ["Tick history", "tick_history_sufficient"], ["Free margin", "free_margin_sufficient"] ] as const).map(([label, key]) => <Row key={key} label={label}><Status value={Boolean(gate[key])} /></Row>)}
          <Row label="Existing position"><span>{gate.existing_position ? "YES" : "NO"} <Status value={gate.existing_position} invert /></span></Row>
          <Row label="M1/M5 alignment"><Status value={gate.directions_aligned} /></Row>
          <Row label="Eligible for AI"><span>{state.validity.eligible ? "YES" : "NO"} <Status value={state.validity.eligible} /></span></Row>
        </Panel>
        <Panel title={state.validity.eligible ? "VALIDITY" : "REJECTION"} className={state.validity.eligible ? "valid" : "blocked"}>
          <div className="validity-title">{state.validity.eligible ? "✓ VALID" : "✕ NOT ELIGIBLE"}</div>
          <div className="reason-list">{(state.validity.eligible ? state.validity.valid_reasons.map((message) => ({ message })) : state.validity.reasons).map((item, index) => <div key={index}>{state.validity.eligible ? "✓" : "✕"} {item.message}</div>)}</div>
        </Panel>
      </div>

      <CandleChart state={state} />

      <div className="grid detail-grid">
        <Panel title="NEWS GATE">
          <Row label="Calendar"><span>{news?.calendar_available ? "AVAILABLE" : "UNAVAILABLE"} <Status value={news?.calendar_available} /></span></Row>
          <Row label="Blackout active">{news?.blackout_active ? "YES" : "NO"}</Row>
          <Row label="Safe for AI"><Status value={news?.safe_for_ai} /></Row>
          <Row label="Nearest high news">{text(news?.nearest_event?.name ?? "None")}</Row>
          <Row label="Minutes to event">{number(news?.minutes_to_nearest_event)}</Row>
          <CalendarEvents title="UPCOMING HIGH-IMPACT USD" events={news?.upcoming_high_impact_events ?? []} />
          <CalendarEvents title="RECENT HIGH-IMPACT USD" events={news?.recent_high_impact_events ?? []} />
        </Panel>
        <Panel title={historical ? "LAST AI ADVISORY — HISTORICAL" : "LAST AI ADVISORY"} className="advisory-panel">
          {!last ? <p className="muted">No persisted advisory is available.</p> : <>
            <div className="decision">{text(last.decision)}</div>
            <Row label="Status / Model">{text(last.status)} / {text(last.model)}</Row><Row label="Advisory time">{text(last.timestamp)}</Row>
            <Row label="Candidate">{text(last.candidate_time)}</Row><Row label="Confidence">{last.confidence == null ? "—" : `${last.confidence}%`}</Row>
            <Row label="Regime">{text(last.market_regime)}</Row><Row label="Analysis">{text(last.setup_summary)}</Row>
            <Row label="Entry">{last.entry_price != null ? text(last.entry_price) : last.entry_zone_low != null ? `${last.entry_zone_low} – ${last.entry_zone_high}` : "—"}</Row>
            <Row label="Stop / Target">{text(last.stop_loss)} / {text(last.take_profit)}</Row><Row label="Risk / Reward">{text(last.risk_reward_ratio)}</Row>
            <Row label="Invalidation">{text(last.invalidation_reason)}</Row>
            {Array.isArray(last.warnings) && last.warnings.length > 0 ? <div className="warning-list"><small>WARNINGS</small>{last.warnings.map((warning, index) => <div key={index}>• {text(warning)}</div>)}</div> : null}
          </>}
        </Panel>
        <Panel title="OPENAI / BUDGET">
          <Row label="Model / Effort">{text(state.ai.model)} / {text(state.ai.reasoning_effort)}</Row>
          <Row label="Input / Output">{text(last?.usage?.input_tokens)} / {text(last?.usage?.output_tokens)}</Row>
          <Row label="Cached / Cache write">{text(last?.usage?.cached_input_tokens)} / {text(last?.usage?.cache_write_tokens)}</Row>
          <Row label="Reasoning tokens">{text(last?.usage?.reasoning_tokens)}</Row>
          <Row label="Latency / Cost">{last?.latency_ms == null ? "—" : `${number(last.latency_ms)} ms`} / {money(last?.estimated_cost_usd, 6)}</Row>
          <Row label="Calls today / week">{text(summary?.calls_today)} / {text(summary?.calls_this_week)}</Row>
          <Row label="Known / budget today">{money(summary?.known_spend_today_usd, 6)} / {money(summary?.budget_accounted_spend_today_usd, 6)}</Row>
          <Row label="Known / budget week">{money(summary?.known_spend_this_week_usd, 6)} / {money(summary?.budget_accounted_spend_this_week_usd, 6)}</Row>
          <Row label="Daily call / spend limits">{text(budget?.max_calls_per_day)} / {money(budget?.daily_spend_cap_usd)}</Row>
          <Row label="Weekly spend limit">{money(budget?.weekly_spend_cap_usd)}</Row>
          <div className="progress"><i style={{ width: `${budgetPct}%` }} /></div>
        </Panel>
        <Panel title="ACCOUNT">
          <Row label="Balance">{money(state.account?.balance)} {state.account?.currency}</Row><Row label="Equity">{money(state.account?.equity)} {state.account?.currency}</Row><Row label="Free margin">{money(state.account?.free_margin)} {state.account?.currency}</Row><Row label="XAUUSD position">{state.account?.xauusd_position_exists ? `${state.account.xauusd_position_count} OPEN` : "NONE"}</Row>
        </Panel>
        <Panel title="SYSTEM">
          <Row label="Application mode">{state.system.mode}</Row><Row label="Engine">{state.system.engine_status}</Row><Row label="Candidate candle">{text(state.system.candidate_time)}</Row><Row label="Candidate hash">{state.system.candidate_hash ? `${state.system.candidate_hash.slice(0, 12)}…` : "—"}</Row><Row label="Persistent state"><Status value={state.system.persistent_state_available} /></Row><Row label="Last update">{shortTime(state.system.last_refresh)}</Row><Row label="Uptime">{Math.floor(state.system.uptime_seconds)} sec</Row>
        </Panel>
        <Panel title="RECENT EVENTS" className="events-panel">
          <div className="events">{state.events.slice(-9).reverse().map((item, index) => <div key={`${item.timestamp}-${index}`}><time>{shortTime(item.timestamp)}</time><b className={`event-${item.level.toLowerCase()}`}>{item.level}</b><span>{item.message}</span></div>)}</div>
        </Panel>
      </div>
    </>}
  </main>;
}

export default App;
