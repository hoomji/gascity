---------------------------- MODULE ProviderFallback ----------------------------
\* Provider fallback in gc (city-next f2d8d5dbb) with quota-watch writing provider health.
\* Providers: P (the agent's primary), F1, F2 (its fallback_providers, in order).
\* Health "none" = no fresh record: gc fails open and treats it like "ok".
EXTENDS Naturals, Sequences

CONSTANTS FixStartCommand, \* TRUE models branch fix/health-gate-start-command
          WithAgent,       \* model the pool agent with fallbacks
          WithDispatcher,  \* model core.control-dispatcher
          Health           \* values a health record can take

Provs     == {"P", "F1", "F2"}
Fallbacks == <<"F1", "F2">>

VARIABLES health,      \* provider -> health record state written by quota-watch
          probe,       \* the pool agent: "idle" | "running" | "refused"
          prov,        \* provider its session was created with ("-" when idle)
          override,    \* gc.provider_override recorded on the session bead
          pickedWith,  \* health snapshot gc saw when it created (or refused) the session
          disp         \* core.control-dispatcher (start_command, nominal provider P): "down" | "up" | "refused"

vars == <<health, probe, prov, override, pickedWith, disp>>

Red(h, p) == h[p] = "red"
FirstNonRed(h) == LET ok == {i \in 1..Len(Fallbacks) : ~Red(h, Fallbacks[i])}
                  IN IF ok = {} THEN "-" ELSE Fallbacks[CHOOSE i \in ok : \A j \in ok : i <= j]

Init == /\ health = [p \in Provs |-> "ok"]
        /\ probe = "idle" /\ prov = "-" /\ override = FALSE
        /\ pickedWith = [p \in Provs |-> "ok"]
        /\ disp = "down"

\* quota-watch writes a new reading for one provider
QuotaWatchWrites == \E p \in (IF WithAgent THEN Provs ELSE {"P"}), s \in Health : s # health[p] /\ health' = [health EXCEPT ![p] = s]
                    /\ UNCHANGED <<probe, prov, override, pickedWith, disp>>

\* work is slung to the agent and gc plans a fresh session
WorkArrives == /\ WithAgent /\ probe \in {"idle", "refused"}
               /\ pickedWith' = health
               /\ IF ~Red(health, "P")
                    THEN probe' = "running" /\ prov' = "P" /\ override' = FALSE
                  ELSE IF FirstNonRed(health) # "-"
                    THEN probe' = "running" /\ prov' = FirstNonRed(health) /\ override' = TRUE
                  ELSE probe' = "refused" /\ prov' = "-" /\ override' = FALSE
               /\ UNCHANGED <<health, disp>>

\* the session finishes or is closed; later work needs a new session
SessionEnds == probe = "running" /\ probe' = "idle" /\ prov' = "-" /\ override' = FALSE
               /\ UNCHANGED <<health, pickedWith, disp>>

\* the dispatcher (runs its own start_command, never launches P) is started
DispatcherStarts == /\ WithDispatcher /\ disp \in {"down", "refused"}
                    /\ disp' = IF Red(health, "P") /\ ~FixStartCommand THEN "refused" ELSE "up"
                    /\ UNCHANGED <<health, probe, prov, override, pickedWith>>
DispatcherStops == disp = "up" /\ disp' = "down" /\ UNCHANGED <<health, probe, prov, override, pickedWith>>

Next == QuotaWatchWrites \/ WorkArrives \/ SessionEnds \/ DispatcherStarts \/ DispatcherStops

Spec == Init /\ [][Next]_vars

\* ---- Rules the reader agrees to ----
\* A session only ever starts on a provider that was not red when it started.
StartsOnHealthy == probe = "running" => ~Red(pickedWith, prov)
\* While the primary is not red, work runs on the primary.
PrimaryWhenHealthy == (probe = "running" /\ ~Red(pickedWith, "P")) => prov = "P"
\* The fallback taken is the first one in the list that is not red.
FirstFallbackWins == (probe = "running" /\ prov # "P") => prov = FirstNonRed(pickedWith)
\* The session bead records an override exactly when it is not on the primary.
OverrideMeansFallback == probe = "running" => (override <=> prov # "P")
\* Work is refused only when every candidate was red.
RefusedOnlyWhenAllRed == probe = "refused" => \A p \in Provs : Red(pickedWith, p)
\* The dispatcher never launches a provider, so quota must never stop it.
DispatcherNeverBlockedByQuota == disp # "refused"
\* A running session keeps the provider it started with.
NoMigration == [][(probe = "running" /\ probe' = "running") => prov' = prov]_vars
=============================================================================
