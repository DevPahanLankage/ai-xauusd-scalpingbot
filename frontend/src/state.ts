import type { ApplicationState } from "./types";

export function acceptIncomingState(
  current: ApplicationState | null,
  incoming: ApplicationState,
): ApplicationState {
  if (current && incoming.revision < current.revision) return current;
  return incoming;
}

export function advisoryIsHistorical(state: ApplicationState): boolean {
  const last = state.ai.last_advisory;
  return Boolean(last && last.candidate_time !== state.system.candidate_time);
}
