package main

import (
	"strings"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/doctor"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
)

const testGiB = int64(1) << 30

func resourcesCfg(memory, reserve string, classes map[string]config.ResourceClass, agents ...config.Agent) *config.City {
	return &config.City{
		Resources: config.ResourcesConfig{Memory: memory, Reserve: reserve, Classes: classes},
		Agents:    agents,
	}
}

func classedAgent(name, class string, maxSess *int) config.Agent {
	return config.Agent{Name: name, ResourceClass: class, MaxActiveSessions: maxSess}
}

func TestResourceBudget_DisabledWithoutTable(t *testing.T) {
	b, err := newResourceBudget(&config.City{Agents: []config.Agent{{Name: "a", ResourceClass: "heavy"}}}, resourceLocalHost)
	if err != nil || b != nil {
		t.Fatalf("newResourceBudget without [resources] = %v, %v; want nil, nil", b, err)
	}
	if l := b.newLedger(); l != nil {
		t.Fatalf("nil budget ledger = %v, want nil", l)
	}
	var l *resourceLedger
	if v := l.try("x", "a"); !v.fits() {
		t.Fatalf("nil ledger try = %v, want fits", v)
	}
}

func TestResourceLedger_Outcomes(t *testing.T) {
	zero := 0
	two := 2
	cfg := resourcesCfg("10GiB", "2GiB", map[string]config.ResourceClass{
		"heavy":   {Memory: "6GiB"},
		"light":   {Memory: "1GiB", MaxActiveSessions: &two},
		"huge":    {Memory: "9GiB"},
		"refused": {Memory: "1GiB", MaxActiveSessions: &zero},
	},
		classedAgent("builder", "heavy", nil),
		classedAgent("chat", "light", nil),
		classedAgent("giant", "huge", nil),
		classedAgent("nope", "refused", nil),
		config.Agent{Name: "plain"},
	)
	b, err := newResourceBudget(cfg, resourceLocalHost)
	if err != nil {
		t.Fatal(err)
	}
	if b.capacity != 8*testGiB {
		t.Fatalf("capacity = %d, want 8GiB (10 - 2 reserve)", b.capacity)
	}
	l := b.newLedger()

	if v := l.try("g1", "giant"); v.outcome != resourceNeverFits {
		t.Fatalf("giant: %v, want never fits (9GiB > 8GiB capacity)", v)
	}
	if v := l.try("n1", "nope"); v.outcome != resourceNeverFits {
		t.Fatalf("refused class: %v, want never fits (max 0)", v)
	}
	if v := l.try("b1", "builder"); !v.fits() {
		t.Fatalf("first builder: %v, want fits", v)
	}
	if v := l.try("b1", "builder"); !v.fits() || l.used != 6*testGiB {
		t.Fatalf("re-trying a charged session must not double count: %v used=%d", v, l.used)
	}
	if v := l.try("c1", "chat"); !v.fits() {
		t.Fatalf("chat 1: %v", v)
	}
	if v := l.try("c2", "chat"); !v.fits() {
		t.Fatalf("chat 2: %v", v)
	}
	if v := l.try("c3", "chat"); v.outcome != resourceClassFull {
		t.Fatalf("chat 3: %v, want class full (max 2)", v)
	}
	if v := l.try("b2", "builder"); v.outcome != resourceBudgetFull {
		t.Fatalf("second builder: %v, want budget full (8 used + 6 > 8)", v)
	}
	// Head-of-line: once a request misses on memory, later classed requests
	// queue behind it even when they would fit.
	l.used = 0
	if v := l.try("b3", "builder"); v.outcome != resourceQueuedBehind {
		t.Fatalf("after a memory miss: %v, want queued behind", v)
	}
	// Unclassed agents reserve nothing and are never deferred.
	if v := l.try("p1", "plain"); !v.fits() {
		t.Fatalf("unclassed: %v, want fits", v)
	}
}

func TestResourceLedger_DefaultClassAndUnknownTemplate(t *testing.T) {
	cfg := resourcesCfg("4GiB", "", map[string]config.ResourceClass{"std": {Memory: "3GiB"}},
		config.Agent{Name: "a"}, config.Agent{Name: "b"})
	cfg.Resources.DefaultClass = "std"
	b, err := newResourceBudget(cfg, resourceLocalHost)
	if err != nil {
		t.Fatal(err)
	}
	l := b.newLedger()
	if v := l.try("1", "a"); !v.fits() {
		t.Fatalf("a: %v", v)
	}
	if v := l.try("2", "b"); v.outcome != resourceBudgetFull {
		t.Fatalf("b via default_class: %v, want budget full", v)
	}
	if v := l.try("3", "gone-template"); !v.fits() {
		t.Fatalf("unknown template reserves nothing: %v", v)
	}
}

func TestResourceBudget_AutoUsesHostMemory(t *testing.T) {
	old := hostMemoryTotal
	hostMemoryTotal = func() (int64, error) { return 16 * testGiB, nil }
	t.Cleanup(func() { hostMemoryTotal = old })
	b, err := newResourceBudget(resourcesCfg("auto", "4GiB", map[string]config.ResourceClass{"x": {Memory: "1GiB"}}), resourceLocalHost)
	if err != nil {
		t.Fatal(err)
	}
	if b.capacity != 12*testGiB {
		t.Fatalf("auto capacity = %d, want 12GiB", b.capacity)
	}
}

func TestParseMemTotal(t *testing.T) {
	got, err := parseMemTotal(strings.NewReader("MemFree:  100 kB\nMemTotal:       32768000 kB\n"))
	if err != nil || got != 32768000*1024 {
		t.Fatalf("parseMemTotal = %d, %v", got, err)
	}
	if _, err := parseMemTotal(strings.NewReader("MemFree: 1 kB\n")); err == nil {
		t.Fatal("missing MemTotal must error")
	}
}

// --- desired-state gate ----------------------------------------------------

func TestComputePoolDesiredStates_ResourceBudgetCapsNewDemand(t *testing.T) {
	cfg := resourcesCfg("13GiB", "", map[string]config.ResourceClass{"heavy": {Memory: "6GiB"}},
		classedAgent("builder", "heavy", intPtr(5)))
	result := ComputePoolDesiredStates(cfg, nil, nil, map[string]int{"builder": 3})
	if got := PoolDesiredCounts(result)["builder"]; got != 2 {
		t.Fatalf("desired builders = %d, want 2 (two 6GiB sessions fit in 13GiB)", got)
	}
}

func TestComputePoolDesiredStates_ResourceBudgetCountsResidentSessions(t *testing.T) {
	cfg := resourcesCfg("13GiB", "", map[string]config.ResourceClass{"heavy": {Memory: "6GiB"}},
		classedAgent("builder", "heavy", intPtr(5)))
	resident := []sessionpkg.Info{
		{ID: "s-1", Template: "builder", MetadataState: string(sessionpkg.StateActive)},
		// Asleep sessions hold nothing.
		{ID: "s-2", Template: "builder", MetadataState: string(sessionpkg.StateAsleep)},
	}
	result := ComputePoolDesiredStates(cfg, nil, resident, map[string]int{"builder": 3})
	if got := PoolDesiredCounts(result)["builder"]; got != 1 {
		t.Fatalf("desired builders = %d, want 1 (one 6GiB already resident)", got)
	}
}

func TestComputePoolDesiredStates_ResourceBudgetHeadOfLine(t *testing.T) {
	cfg := resourcesCfg("10GiB", "", map[string]config.ResourceClass{
		"big":   {Memory: "8GiB"},
		"small": {Memory: "1GiB"},
	},
		classedAgent("big", "big", intPtr(5)),
		classedAgent("small", "small", intPtr(5)),
	)
	resident := []sessionpkg.Info{{ID: "s-1", Template: "small", MetadataState: string(sessionpkg.StateActive)},
		{ID: "s-2", Template: "small", MetadataState: string(sessionpkg.StateActive)},
		{ID: "s-3", Template: "small", MetadataState: string(sessionpkg.StateActive)}}
	trace := newPoolDesiredStateTestTrace("big", "small")
	states := computePoolDesiredStates(cfg, nil, resident, map[string]int{"big": 1, "small": 4}, nil, trace)
	counts := PoolDesiredCounts(states)
	if counts["big"] != 0 || counts["small"] != 0 {
		t.Fatalf("desired = %v, want none: big (8GiB) does not fit beside 3GiB, and small queues behind it", counts)
	}
	if trace.reasonCounts[string(TraceReasonResourceBudgetFull)] == 0 || trace.reasonCounts[string(TraceReasonResourceQueuedBehind)] == 0 {
		t.Fatalf("trace reasons = %v, want resource_budget_full and resource_queued_behind", trace.reasonCounts)
	}
}

func TestComputePoolDesiredStates_NoResourcesTableUnchanged(t *testing.T) {
	cfg := &config.City{Agents: []config.Agent{classedAgent("builder", "heavy", intPtr(5))}}
	result := ComputePoolDesiredStates(cfg, nil, nil, map[string]int{"builder": 3})
	if got := PoolDesiredCounts(result)["builder"]; got != 3 {
		t.Fatalf("desired builders = %d, want 3 (no [resources] table)", got)
	}
}

// --- start-boundary gate ---------------------------------------------------

func resourceReconcilerEnv(t *testing.T, resources config.ResourcesConfig, n int) (*reconcilerTestEnv, []beads.Bead) {
	t.Helper()
	env := newReconcilerTestEnv(t)
	var agents []config.Agent
	var sessions []beads.Bead
	for i := 0; i < n; i++ {
		name := []string{"worker-0", "worker-1", "worker-2"}[i]
		agents = append(agents, config.Agent{Name: name, ResourceClass: "heavy"})
		env.addDesired(name, name, false)
		s := env.createSessionBead(name, name)
		env.markSessionCreating(&s)
		sessions = append(sessions, s)
	}
	env.cfg = &config.City{Agents: agents, Resources: resources}
	return env, sessions
}

func heavyResources(memory string) config.ResourcesConfig {
	return config.ResourcesConfig{Memory: memory, Classes: map[string]config.ResourceClass{"heavy": {Memory: "6GiB"}}}
}

func TestReconcileSessionBeads_ResourceBudgetDefersStartThatDoesNotFit(t *testing.T) {
	env, sessions := resourceReconcilerEnv(t, heavyResources("8GiB"), 2)
	if woken := env.reconcile(sessions); woken != 1 {
		t.Fatalf("woken = %d, want 1 (two 6GiB sessions, 8GiB budget); stderr=%s", woken, env.stderr.String())
	}
	if !strings.Contains(env.stderr.String(), "resource budget: deferring") {
		t.Fatalf("stderr missing deferral line: %s", env.stderr.String())
	}
}

func TestReconcileSessionBeads_ResourceBudgetOffStartsAll(t *testing.T) {
	env, sessions := resourceReconcilerEnv(t, config.ResourcesConfig{}, 2)
	if woken := env.reconcile(sessions); woken != 2 {
		t.Fatalf("woken = %d, want 2 without [resources]", woken)
	}
}

func TestReconcileSessionBeads_ResourceBudgetWaitsForResidentToEnd(t *testing.T) {
	env, sessions := resourceReconcilerEnv(t, heavyResources("8GiB"), 2)
	// worker-0 is already running and active: it holds 6GiB.
	running := sessions[0]
	env.addDesired("worker-0", "worker-0", true)
	env.markSessionActive(&running)
	if woken := env.reconcile([]beads.Bead{running, sessions[1]}); woken != 0 {
		t.Fatalf("woken = %d, want 0 while worker-0 holds the budget; stderr=%s", woken, env.stderr.String())
	}
	// worker-0 ends: its bead closes and it leaves the desired set.
	delete(env.desiredState, "worker-0")
	_ = env.sp.Stop("worker-0")
	if err := env.store.Close(running.ID); err != nil {
		t.Fatal(err)
	}
	env.clk.Time = env.clk.Time.Add(time.Minute)
	if woken := env.reconcile([]beads.Bead{sessions[1]}); woken != 1 {
		t.Fatalf("woken = %d, want 1 once worker-0 has ended; stderr=%s", woken, env.stderr.String())
	}
}

func TestApplyResourceBudgetToStarts_OldestFirst(t *testing.T) {
	cfg := resourcesCfg("8GiB", "", map[string]config.ResourceClass{"heavy": {Memory: "6GiB"}},
		classedAgent("a", "heavy", nil), classedAgent("b", "heavy", nil))
	now := time.Date(2026, 11, 1, 12, 0, 0, 0, time.UTC)
	newer := startCandidate{order: 0, info: sessionpkg.Info{ID: "new", Template: "a", CreatedAt: now}, tp: TemplateParams{TemplateName: "a"}}
	older := startCandidate{order: 1, info: sessionpkg.Info{ID: "old", Template: "b", CreatedAt: now.Add(-time.Hour)}, tp: TemplateParams{TemplateName: "b"}}
	got := applyResourceBudgetToStarts(cfg, []startCandidate{newer, older}, nil, nil, nil)
	if len(got) != 1 || got[0].info.ID != "old" {
		t.Fatalf("admitted %v, want only the older candidate", got)
	}
	// A resident session is charged; a candidate listed as resident is not.
	resident := []sessionpkg.Info{{ID: "run", Template: "a", MetadataState: string(sessionpkg.StateActive)}, older.info}
	if got := applyResourceBudgetToStarts(cfg, []startCandidate{older}, resident, nil, nil); len(got) != 0 {
		t.Fatalf("admitted %v beside a resident 6GiB session, want none", got)
	}
}

func TestResourceResidentInfos(t *testing.T) {
	infos := map[string]sessionpkg.Info{
		"active":   {ID: "active", MetadataState: "active"},
		"awake":    {ID: "awake", MetadataState: "awake"},
		"draining": {ID: "draining", MetadataState: "draining"},
		"asleep":   {ID: "asleep", MetadataState: "asleep"},
		"closed":   {ID: "closed", MetadataState: "active", Closed: true},
		"waiting":  {ID: "waiting", MetadataState: "creating"},
		"inflight": {ID: "inflight", MetadataState: "creating"},
	}
	got := map[string]bool{}
	for _, info := range resourceResidentInfos(infos, func(i sessionpkg.Info) bool { return i.ID == "inflight" }) {
		got[info.ID] = true
	}
	for _, id := range []string{"active", "awake", "draining", "inflight"} {
		if !got[id] {
			t.Errorf("%s should be resident", id)
		}
	}
	for _, id := range []string{"asleep", "closed", "waiting"} {
		if got[id] {
			t.Errorf("%s should not be resident", id)
		}
	}
}

func TestResourceBudgetDoctorCheck(t *testing.T) {
	if res := newResourceBudgetDoctorCheck(&config.City{}).Run(nil); res.Status != doctor.StatusOK {
		t.Fatalf("no table: %+v", res)
	}
	cfg := resourcesCfg("8GiB", "2GiB", map[string]config.ResourceClass{"heavy": {Memory: "8GiB"}, "light": {Memory: "1GiB"}},
		classedAgent("builder", "heavy", nil), classedAgent("chat", "light", nil))
	res := newResourceBudgetDoctorCheck(cfg).Run(nil)
	if res.Status != doctor.StatusWarning || !strings.Contains(res.Message, "heavy (builder)") {
		t.Fatalf("never-fit class: %+v", res)
	}
	cfg.Resources.Classes["heavy"] = config.ResourceClass{Memory: "4GiB"}
	res = newResourceBudgetDoctorCheck(cfg).Run(nil)
	if res.Status != doctor.StatusOK || !strings.Contains(res.Message, "budget 6GiB after reserve, 2 classes") {
		t.Fatalf("fitting classes: %+v", res)
	}
}
