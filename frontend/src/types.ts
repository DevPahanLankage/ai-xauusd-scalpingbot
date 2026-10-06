export type Reason = { code: string; message: string };
export type Candle = { time: string; open: number; high: number; low: number; close: number };
export type EventItem = { timestamp: string; level: string; message: string };

export interface ApplicationState {
  schema_version: string;
  revision: number;
  system: Record<string, unknown> & {
    mode: string;
    engine_status: string;
    mt5_connected: boolean;
    candidate_time: string | null;
    candidate_hash: string | null;
    persistent_state_available: boolean | null;
    last_refresh: string;
    trade_server_time: string | null;
    uptime_seconds: number;
  };
  market: null | {
    symbol: string;
    bid: number;
    ask: number;
    spread_price: number;
    spread_points: number;
    point: number;
    digits: number;
    m1_latest_range_points: number;
    m1_average_range_points: number;
    m5_latest_range_points: number;
    m5_average_range_points: number;
    recent_tick_count: number;
  };
  market_gate: null | Record<string, unknown> & {
    eligible_for_ai: boolean;
    market_active: boolean;
    quote_fresh: boolean;
    tick_fresh: boolean;
    spread_acceptable: boolean;
    volatility_acceptable: boolean;
    history_sufficient: boolean;
    tick_history_sufficient: boolean;
    free_margin_sufficient: boolean;
    direction_m1: string;
    direction_m5: string;
    directions_aligned: boolean;
    existing_position: boolean;
    metrics: Record<string, number | null>;
  };
  validity: { eligible: boolean; status: string; reasons: Reason[]; valid_reasons: string[] };
  news_gate: null | Record<string, unknown> & {
    calendar_available: boolean;
    safe_for_ai: boolean;
    blackout_active: boolean;
    nearest_event: null | Record<string, unknown>;
    minutes_to_nearest_event: number | null;
    upcoming_high_impact_events: Array<Record<string, unknown>>;
    recent_high_impact_events: Array<Record<string, unknown>>;
  };
  ai: Record<string, unknown> & {
    state: string;
    model?: string;
    reasoning_effort?: string;
    current_candidate_time?: string | null;
    current_candidate_hash?: string | null;
    candidate_consumed?: boolean | null;
    last_advisory: null | Record<string, unknown> & {
      timestamp: string;
      candidate_time: string;
      decision: string | null;
      confidence: number | null;
      usage: Record<string, number | null>;
    };
  };
  usage: null | {
    summary: null | Record<string, number>;
    budget: null | Record<string, number | boolean | string[]>;
  };
  account: null | {
    currency: string;
    balance: number;
    equity: number;
    free_margin: number;
    xauusd_position_exists: boolean;
    xauusd_position_count: number;
  };
  chart: { m1: Candle[]; m5: Candle[] };
  events: EventItem[];
}
