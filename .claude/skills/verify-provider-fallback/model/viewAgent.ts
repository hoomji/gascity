// Reader's words for the provider fallback chart.
const red = (h: any, p: string) => h[p] === "red";
const quota = (h: any) =>
  !red(h, "P") ? "Primary usable"
  : !red(h, "F1") ? "Primary spent"
  : !red(h, "F2") ? "Primary and first fallback spent"
  : "Every provider spent";
const agent = (v: any) =>
  v.probe === "idle" ? "No session"
  : v.probe === "refused" ? "Work waiting, nothing usable"
  : v.prov === "P" ? "Working on primary"
  : v.prov === "F1" ? "Working on first fallback"
  : "Working on second fallback";
const dispatcher = (d: string) => (d === "up" ? "Running" : d === "down" ? "Stopped" : "Blocked by quota");

export const title = "A pool agent with fallbacks: which provider new work starts on";
export const state = (v: any) => ({
  "Quota health": quota(v.health),
  "Agent with fallbacks": agent(v),

});
export const step = (action: string, from: any, to: any) => {
  switch (action) {
    case "QuotaWatchWrites": return "quota-watch writes a new reading";
    case "WorkArrives": return "work is slung to the agent";
    case "SessionEnds": return "the session ends";
    case "DispatcherStarts": return "gc starts the dispatcher";
    case "DispatcherStops": return "the dispatcher stops";
  }
  return action;
};
