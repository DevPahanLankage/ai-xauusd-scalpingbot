import { describe, expect, it } from "vitest";
import { acceptIncomingState, advisoryIsHistorical } from "./state";
import type { ApplicationState } from "./types";

const sample = (revision: number, current: string, last: string): ApplicationState => ({
  revision,
  schema_version: "1",
  system: { mode: "combined", engine_status: "MONITORING", mt5_connected: true, candidate_time: current, candidate_hash: null, persistent_state_available: true, last_refresh: "", trade_server_time: "", uptime_seconds: 1 },
  market: null,
  market_gate: null,
  validity: { eligible: false, status: "BLOCKED", reasons: [], valid_reasons: [] },
  news_gate: null,
  ai: { state: "BLOCKED", last_advisory: { timestamp: "", candidate_time: last, decision: "NO_TRADE", confidence: 80, usage: {} } },
  usage: null,
  account: null,
  chart: { m1: [], m5: [] },
  events: [],
});

describe("live state handling", () => {
  it("ignores out-of-order websocket revisions", () => {
    expect(acceptIncomingState(sample(3, "c", "b"), sample(2, "c", "b")).revision).toBe(3);
  });
  it("keeps current candidate separate from historical advisory", () => {
    expect(advisoryIsHistorical(sample(3, "current", "older"))).toBe(true);
    expect(advisoryIsHistorical(sample(3, "current", "current"))).toBe(false);
  });
});
