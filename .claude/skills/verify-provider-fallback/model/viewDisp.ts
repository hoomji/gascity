const dispatcher = (d: string) => (d === "up" ? "Running" : d === "down" ? "Stopped" : "Blocked by quota");
export const title = "core.control-dispatcher (runs its own command, nominal provider = primary)";
export const state = (v: any) => ({
  "Primary provider": v.health["P"] === "red" ? "Spent (held)" : "Usable",
  "Control dispatcher": dispatcher(v.disp),
});
export const step = (action: string) => {
  switch (action) {
    case "QuotaWatchWrites": return "quota-watch writes a new reading";
    case "DispatcherStarts": return "gc starts the dispatcher";
    case "DispatcherStops": return "the dispatcher stops";
  }
  return action;
};
