package main

import (
	"testing"

	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/session"
)

// v2 admission (resource_admit.go): admit defers a start or create whose
// class does not fit its host's budget, whatever arm proposed it.

// resourceAdmitCfg is a 10 GiB local budget with an 8 GiB heavy class and a
// 1 GiB light class; "builder" and "boss" are heavy, "chat" light.
func resourceAdmitCfg() *config.City {
	return resourcesCfg("10GiB", "", map[string]config.ResourceClass{
		"heavy": {Memory: "8GiB"},
		"light": {Memory: "1GiB"},
	}, classedAgent("builder", "heavy", nil), classedAgent("boss", "heavy", nil), classedAgent("chat", "light", nil))
}

// resourceAdmitIn is admitIn with cfg's budgets and rows with templates.
func resourceAdmitIn(t *testing.T, cfg *config.City, rows []censusRow, inFlight inflightView) admitInput {
	t.Helper()
	in := admitIn(10, 4)
	cfg.Daemon = in.Cfg.Daemon
	in.Cfg = cfg
	in.InFlight = inFlight
	in.Resources = resourceAdmitInputOf(cfg, rows, inFlight)
	if in.Resources.Budgets == nil {
		t.Fatal("resourceAdmitInputOf: no budgets for a [resources] table")
	}
	return in
}

func resourceCensusRow(i int, template, state string) censusRow {
	return censusRow{Key: admitRow(i), Info: session.Info{ID: admitRow(i).ID, Template: template, MetadataState: state}}
}

// A resumed (asleep) or named row's start is a start intent like any other:
// with a running heavy session holding 8 of 10 GiB, a second heavy start is
// deferred and spends no token, while an older light start fits.
func TestAdmitResourceBudgetDefersStartsThatDoNotFit(t *testing.T) {
	cfg := resourceAdmitCfg()
	rows := []censusRow{
		resourceCensusRow(1, "builder", string(session.StateActive)), // resident: 8 GiB
		resourceCensusRow(2, "boss", "asleep"),                       // a named session waking
		resourceCensusRow(3, "chat", "asleep"),                       // resumed light session
	}
	in := resourceAdmitIn(t, cfg, rows, inflightView{})
	res := admit(in, []intent{startOn(2, "", 1), startOn(3, "", 0)})
	causes := causesOf(res.Deferred)
	if got := causes[intentStart+":"+admitRow(2).ID]; got != causeResourcePrefix+string(TraceReasonResourceBudgetFull) {
		t.Fatalf("heavy start cause = %q, want resource budget full; deferred %v", got, causes)
	}
	if len(res.Admitted) != 1 || res.Admitted[0].Key != admitRow(3) {
		t.Fatalf("admitted = %v, want only the light start", keysOf(res.Admitted))
	}
	if res.Bucket.Tokens != 9 {
		t.Fatalf("tokens = %d, want 9: the deferred start spends none", res.Bucket.Tokens)
	}
}

// Head-of-line: once the older heavy start misses on memory, a newer light
// start on the same host waits behind it.
func TestAdmitResourceBudgetHeadOfLine(t *testing.T) {
	cfg := resourceAdmitCfg()
	rows := []censusRow{
		resourceCensusRow(1, "builder", string(session.StateActive)),
		resourceCensusRow(2, "chat", string(session.StateActive)),
		resourceCensusRow(3, "boss", "asleep"),
		resourceCensusRow(4, "chat", "asleep"),
	}
	in := resourceAdmitIn(t, cfg, rows, inflightView{})
	res := admit(in, []intent{startOn(4, "", 1), startOn(3, "", 0)})
	causes := causesOf(res.Deferred)
	if causes[intentStart+":"+admitRow(3).ID] != causeResourcePrefix+string(TraceReasonResourceBudgetFull) ||
		causes[intentStart+":"+admitRow(4).ID] != causeResourcePrefix+string(TraceReasonResourceQueuedBehind) {
		t.Fatalf("causes = %v, want heavy budget-full and light queued-behind", causes)
	}
}

// A named create reserves too; two heavy creates in one pass do not both fit.
func TestAdmitResourceBudgetDefersCreates(t *testing.T) {
	cfg := resourceAdmitCfg()
	in := resourceAdmitIn(t, cfg, nil, inflightView{})
	named := intent{Kind: intentCreate, CreatePlan: createPlan{Template: "boss", Named: &namedCreatePlan{Identity: "boss"}}}
	res := admit(in, []intent{named, poolCreate("builder", "")})
	if len(res.Admitted) != 1 || !res.Admitted[0].named() {
		t.Fatalf("admitted = %d intents, want the named create alone", len(res.Admitted))
	}
	if len(res.Deferred) != 1 || res.Deferred[0].Cause != causeResourcePrefix+string(TraceReasonResourceBudgetFull) {
		t.Fatalf("deferred = %v, want the pool create on the resource budget", causesOf(res.Deferred))
	}
}

// Resident: running rows and rows or creates with a start in flight; an
// asleep row holds nothing.
func TestResourceAdmitInputResidents(t *testing.T) {
	cfg := resourceAdmitCfg()
	rows := []censusRow{
		resourceCensusRow(1, "chat", string(session.StateAwake)),
		resourceCensusRow(2, "chat", "asleep"),
		resourceCensusRow(3, "chat", string(session.StateStartPending)),
		resourceCensusRow(4, "chat", string(session.StateDraining)),
	}
	inFlight := inflightView{Entries: []inflightEntry{
		{Kind: inflightStart, Key: admitRow(3)},
		{Kind: inflightCreate, Token: "tok", Template: "builder"},
	}}
	in := resourceAdmitInputOf(cfg, rows, inFlight)
	got := map[string]bool{}
	for _, r := range in.Resident {
		got[r.Key.ID+"/"+r.Template] = true
	}
	want := map[string]bool{"gc-1/chat": true, "gc-3/chat": true, "gc-4/chat": true, "/builder": true}
	if len(got) != len(want) {
		t.Fatalf("resident = %v, want %v", got, want)
	}
	for k := range want {
		if !got[k] {
			t.Fatalf("resident = %v, want %v", got, want)
		}
	}
	if in.Rows[admitRow(2)].Host != resourceLocalHost {
		t.Fatalf("row host = %q, want %q", in.Rows[admitRow(2)].Host, resourceLocalHost)
	}
}

// No [resources] table: admit sees no budgets and changes nothing.
func TestAdmitResourceBudgetDisabled(t *testing.T) {
	cfg := &config.City{Agents: []config.Agent{classedAgent("builder", "heavy", nil)}}
	if in := resourceAdmitInputOf(cfg, []censusRow{resourceCensusRow(1, "builder", "active")}, inflightView{}); in.Budgets != nil || in.Rows != nil {
		t.Fatalf("resourceAdmitInputOf without [resources] = %+v, want zero", in)
	}
	res := admit(admitIn(10, 4), []intent{startOn(1, "", 0), startOn(2, "", 1)})
	if len(res.Deferred) != 0 {
		t.Fatalf("deferred = %v, want none", causesOf(res.Deferred))
	}
}

// Per-host ledgers: a full local host does not block a host without a
// budget, and verdicts carry their host.
func TestResourceLedgersArePerHost(t *testing.T) {
	cfg := resourceAdmitCfg()
	budgets, err := newResourceBudgets(cfg)
	if err != nil || len(budgets) != 1 || budgets[resourceLocalHost] == nil {
		t.Fatalf("newResourceBudgets = %v, %v; want the local host only", budgets, err)
	}
	ls := budgets.newLedgers()
	if v := ls.try(resourceLocalHost, "a", "builder"); !v.fits() || v.host != resourceLocalHost {
		t.Fatalf("first heavy = %+v, want fits on local", v)
	}
	if v := ls.try(resourceLocalHost, "b", "builder"); v.outcome != resourceBudgetFull {
		t.Fatalf("second heavy = %v, want budget full", v)
	}
	if v := ls.try("other", "c", "builder"); !v.fits() {
		t.Fatalf("host without a budget = %v, want fits", v)
	}
	if b, _ := newResourceBudget(cfg, "other"); b != nil {
		t.Fatal("newResourceBudget for a remote host: want nil until [hosts] lands")
	}
}

// Budgets are per host (multi-host-placement, "Changes to resource-budgets"):
// a full, head-of-line-blocked host does not defer a start on another host.
func TestAdmitResourceBudgetIsPerHost(t *testing.T) {
	cfg := resourceAdmitCfg()
	rows := []censusRow{
		resourceCensusRow(1, "builder", string(session.StateActive)),
		resourceCensusRow(2, "boss", "asleep"),
		resourceCensusRow(3, "builder", "asleep"),
	}
	in := resourceAdmitIn(t, cfg, rows, inflightView{})
	in.Resources.Budgets["host-a"] = in.Resources.Budgets[resourceLocalHost]
	r := in.Resources.Rows[admitRow(3)]
	r.Host = "host-a"
	in.Resources.Rows[admitRow(3)] = r
	res := admit(in, []intent{startOn(2, "", 1), startOn(3, "", 0)})
	causes := causesOf(res.Deferred)
	if causes[intentStart+":"+admitRow(2).ID] != causeResourcePrefix+string(TraceReasonResourceBudgetFull) {
		t.Fatalf("local heavy start causes = %v, want resource budget full", causes)
	}
	if len(res.Admitted) != 1 || res.Admitted[0].Key != admitRow(3) {
		t.Fatalf("admitted = %v, want the host-a heavy start", keysOf(res.Admitted))
	}
}
