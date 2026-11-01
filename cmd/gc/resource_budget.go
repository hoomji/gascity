package main

import (
	"bufio"
	"fmt"
	"io"
	"os"
	goruntime "runtime"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/gastownhall/gascity/internal/config"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
)

// Resource admission (engdocs/proposals/resource-budgets.md).
//
// Agents declare a resource class, the host declares a memory budget, and gc
// does not start a session whose class reservation does not fit in what is
// left. One pure ledger does the arithmetic for both gates: the desired-state
// gate on pool demand, and the start-boundary gate on every start candidate.

// resourceOutcome is a ledger verdict.
type resourceOutcome int

const (
	// resourceFits: the reservation fits (or the template reserves nothing).
	resourceFits resourceOutcome = iota
	// resourceBudgetFull: the reservation does not fit in what is left now.
	resourceBudgetFull
	// resourceQueuedBehind: an earlier request in this pass did not fit, so
	// this one waits behind it (head-of-line fairness).
	resourceQueuedBehind
	// resourceClassFull: the class's own max_active_sessions is reached.
	resourceClassFull
	// resourceNeverFits: the class can never start on this host.
	resourceNeverFits
)

func (o resourceOutcome) reason() TraceReasonCode {
	switch o {
	case resourceBudgetFull:
		return TraceReasonResourceBudgetFull
	case resourceQueuedBehind:
		return TraceReasonResourceQueuedBehind
	case resourceClassFull:
		return TraceReasonResourceClassFull
	case resourceNeverFits:
		return TraceReasonResourceNeverFits
	default:
		return ""
	}
}

// resourceClassSpec is a parsed [resources.classes] entry.
type resourceClassSpec struct {
	memory    int64
	maxActive int // -1 = no class cap
}

// resourceBudget is the parsed [resources] table for one decision pass.
type resourceBudget struct {
	cfg *config.City
	// capacity is memory - reserve in bytes; -1 when no memory budget is
	// configured (class caps may still apply).
	capacity int64
	classes  map[string]resourceClassSpec
	// templateClass caches template -> class name ("" = reserves nothing).
	templateClass map[string]string
}

// hostMemoryTotal reads the host's total physical memory. Replaced in tests.
var hostMemoryTotal = readHostMemoryTotal

func readHostMemoryTotal() (int64, error) {
	if goruntime.GOOS != "linux" {
		return 0, fmt.Errorf("memory = %q is supported on linux only", config.ResourceMemoryAuto)
	}
	f, err := os.Open("/proc/meminfo")
	if err != nil {
		return 0, err
	}
	defer f.Close() //nolint:errcheck // read-only
	return parseMemTotal(f)
}

// parseMemTotal extracts MemTotal (kB) from /proc/meminfo content.
func parseMemTotal(r io.Reader) (int64, error) {
	sc := bufio.NewScanner(r)
	for sc.Scan() {
		fields := strings.Fields(sc.Text())
		if len(fields) >= 2 && fields[0] == "MemTotal:" {
			kb, err := strconv.ParseInt(fields[1], 10, 64)
			if err != nil {
				return 0, fmt.Errorf("parsing MemTotal: %w", err)
			}
			return kb * 1024, nil
		}
	}
	if err := sc.Err(); err != nil {
		return 0, err
	}
	return 0, fmt.Errorf("MemTotal not found")
}

// resourceLocalHost is the controller's own machine. Every session runs
// there until multi-host placement lands
// (engdocs/proposals/multi-host-placement.md, "Changes to resource-budgets"),
// which adds remote hosts with their own [hosts.X.resources] budgets.
const resourceLocalHost = "local"

// resourceHostOf is the host a session reserves on. A session bead has no
// host field yet, so this is always the local host; placement records one.
func resourceHostOf(sessionpkg.Info) string { return resourceLocalHost }

// newResourceBudget returns host's budget for cfg, or nil when the city has
// no [resources] table (resource admission disabled: today's behaviour) or
// host has no budget. Only the local host has one today: it reads the
// top-level [resources]. An unreadable "auto" budget fails open to "no memory
// budget" with an error the caller may log; class caps still apply.
func newResourceBudget(cfg *config.City, host string) (*resourceBudget, error) {
	if cfg == nil || !cfg.Resources.Enabled() || host != resourceLocalHost {
		return nil, nil
	}
	r := cfg.Resources
	b := &resourceBudget{
		cfg:           cfg,
		capacity:      -1,
		classes:       make(map[string]resourceClassSpec, len(r.Classes)),
		templateClass: make(map[string]string),
	}
	for name, class := range r.Classes {
		spec := resourceClassSpec{memory: class.ResourceClassBytes(), maxActive: -1}
		if class.MaxActiveSessions != nil {
			spec.maxActive = *class.MaxActiveSessions
		}
		b.classes[name] = spec
	}
	var err error
	if mem := strings.TrimSpace(r.Memory); mem != "" {
		var total int64
		if r.MemoryIsAuto() {
			total, err = hostMemoryTotal()
		} else {
			total, err = config.ParseMemorySize(mem)
		}
		if err == nil {
			b.capacity = max(total-r.ReserveBytes(), 0)
		}
	}
	return b, err
}

// classFor returns the class name a template reserves against ("" = none).
func (b *resourceBudget) classFor(template string) string {
	template = strings.TrimSpace(template)
	if c, ok := b.templateClass[template]; ok {
		return c
	}
	class := ""
	if agent := findAgentByTemplate(b.cfg, template); agent != nil {
		class = b.cfg.Resources.EffectiveResourceClass(agent)
		if _, ok := b.classes[class]; !ok {
			class = ""
		}
	}
	b.templateClass[template] = class
	return class
}

// neverFits reports whether a class can never start on this host.
func (b *resourceBudget) neverFits(spec resourceClassSpec) bool {
	if spec.maxActive == 0 {
		return true
	}
	return b.capacity >= 0 && spec.memory > b.capacity
}

// resourceBudgets is one budget per host, keyed by host name. A host without
// an entry admits everything.
type resourceBudgets map[string]*resourceBudget

// newResourceBudgets returns the budgets of every host cfg declares (only
// the local host today), or nil when resource admission is disabled.
func newResourceBudgets(cfg *config.City) (resourceBudgets, error) {
	b, err := newResourceBudget(cfg, resourceLocalHost)
	if b == nil {
		return nil, err
	}
	return resourceBudgets{resourceLocalHost: b}, err
}

// newLedgers returns one empty ledger per host; nil when bs is empty.
func (bs resourceBudgets) newLedgers() *resourceLedgers {
	if len(bs) == 0 {
		return nil
	}
	ls := &resourceLedgers{byHost: make(map[string]*resourceLedger, len(bs))}
	for host, b := range bs {
		ls.byHost[host] = b.newLedger()
	}
	return ls
}

// resourceLedgers holds one ledger per host for a decision pass. Head-of-line
// blocking is per host: a request that misses on one host does not hold up
// another. A nil *resourceLedgers, or a host without a ledger, admits all.
type resourceLedgers struct {
	byHost map[string]*resourceLedger
}

func (ls *resourceLedgers) ledger(host string) *resourceLedger {
	if ls == nil {
		return nil
	}
	return ls.byHost[host]
}

// charge records a resident session on host unconditionally.
func (ls *resourceLedgers) charge(host, sessionID, template string) {
	ls.ledger(host).charge(sessionID, template)
}

// try checks whether a session of template fits on host, charging it when
// it does.
func (ls *resourceLedgers) try(host, sessionID, template string) resourceVerdict {
	v := ls.ledger(host).try(sessionID, template)
	v.host = host
	return v
}

// resourceLedger accumulates reservations on one host for one decision pass.
type resourceLedger struct {
	b          *resourceBudget
	used       int64
	classCount map[string]int
	charged    map[string]bool // session IDs already charged
	blocked    bool            // head-of-line: a request did not fit on memory
}

// resourceVerdict is the result of one admission check.
type resourceVerdict struct {
	outcome    resourceOutcome
	host       string
	class      string
	need       int64
	used       int64
	capacity   int64
	classCount int
	classMax   int
}

func (v resourceVerdict) fits() bool { return v.outcome == resourceFits }

func (v resourceVerdict) payload() traceRecordPayload {
	return traceRecordPayload{
		"host":              v.host,
		"resource_class":    v.class,
		"reservation_bytes": v.need,
		"used_bytes":        v.used,
		"capacity_bytes":    v.capacity,
		"class_count":       v.classCount,
		"class_max":         v.classMax,
	}
}

func (v resourceVerdict) String() string {
	switch v.outcome {
	case resourceBudgetFull:
		return fmt.Sprintf("class %s needs %s, %s of %s reserved", v.class, formatResourceBytes(v.need), formatResourceBytes(v.used), formatResourceBytes(v.capacity))
	case resourceQueuedBehind:
		return fmt.Sprintf("class %s queued behind an earlier session that does not fit", v.class)
	case resourceClassFull:
		return fmt.Sprintf("class %s at max_active_sessions %d", v.class, v.classMax)
	case resourceNeverFits:
		if v.classMax == 0 {
			return fmt.Sprintf("class %s has max_active_sessions = 0 on this host", v.class)
		}
		return fmt.Sprintf("class %s needs %s, more than the %s budget", v.class, formatResourceBytes(v.need), formatResourceBytes(v.capacity))
	default:
		return "fits"
	}
}

func (b *resourceBudget) newLedger() *resourceLedger {
	if b == nil {
		return nil
	}
	return &resourceLedger{b: b, classCount: make(map[string]int), charged: make(map[string]bool)}
}

// charge records a resident session unconditionally. A non-empty sessionID
// is charged at most once per pass.
func (l *resourceLedger) charge(sessionID, template string) {
	if l == nil {
		return
	}
	if sessionID != "" {
		if l.charged[sessionID] {
			return
		}
		l.charged[sessionID] = true
	}
	class := l.b.classFor(template)
	if class == "" {
		return
	}
	l.used += l.b.classes[class].memory
	l.classCount[class]++
}

// try checks whether a session of template fits and charges it when it does.
// A memory miss blocks every later classed request in this pass.
func (l *resourceLedger) try(sessionID, template string) resourceVerdict {
	v := l.check(sessionID, template)
	switch {
	case l == nil:
	case v.fits():
		l.charge(sessionID, template)
	case v.outcome == resourceBudgetFull:
		l.blocked = true
	}
	return v
}

// check is try without effect: the verdict for a session of template,
// charging nothing and blocking nothing.
func (l *resourceLedger) check(sessionID, template string) resourceVerdict {
	if l == nil {
		return resourceVerdict{outcome: resourceFits}
	}
	if sessionID != "" && l.charged[sessionID] {
		return resourceVerdict{outcome: resourceFits}
	}
	class := l.b.classFor(template)
	if class == "" {
		return resourceVerdict{outcome: resourceFits}
	}
	spec := l.b.classes[class]
	v := resourceVerdict{
		class:      class,
		need:       spec.memory,
		used:       l.used,
		capacity:   l.b.capacity,
		classCount: l.classCount[class],
		classMax:   spec.maxActive,
	}
	switch {
	case l.b.neverFits(spec):
		v.outcome = resourceNeverFits
	case spec.maxActive >= 0 && l.classCount[class] >= spec.maxActive:
		v.outcome = resourceClassFull
	case l.blocked:
		v.outcome = resourceQueuedBehind
	case l.b.capacity >= 0 && l.used+spec.memory > l.b.capacity:
		v.outcome = resourceBudgetFull
	default:
		v.outcome = resourceFits
	}
	return v
}

// sessionHoldsResourcesInfo reports whether a session's state means it is,
// or is about to be, resident in memory.
func sessionHoldsResourcesInfo(info sessionpkg.Info) bool {
	if info.Closed {
		return false
	}
	switch sessionpkg.State(strings.TrimSpace(info.MetadataState)) {
	case sessionpkg.StateActive, sessionpkg.StateAwake, sessionpkg.StateCreating,
		sessionpkg.StateStartPending, sessionpkg.StateDraining:
		return true
	}
	return false
}

// newResourceLedgerFromSessions builds a desired-state ledger charged with
// every resident session. Returns nil when resource admission is disabled.
func newResourceLedgerFromSessions(cfg *config.City, sessionInfos []sessionpkg.Info) *resourceLedgers {
	budgets, err := newResourceBudgets(cfg)
	if err != nil {
		resourceDeferrals.logBudgetError(err)
	}
	ledgers := budgets.newLedgers()
	if ledgers == nil {
		return nil
	}
	for _, info := range sessionInfos {
		if sessionHoldsResourcesInfo(info) {
			ledgers.charge(resourceHostOf(info), info.ID, normalizedSessionTemplateInfo(info, cfg))
		}
	}
	return ledgers
}

// applyResourceBudgetToStarts is the start-boundary gate. resident lists the
// sessions already holding memory (see resourceResidentInfos); candidates in
// it are ignored, since a candidate is by definition not running yet. It
// returns the candidates that fit, in their original order; the rest are
// deferred without writes and without spending the wake budget.
func applyResourceBudgetToStarts(
	cfg *config.City,
	candidates []startCandidate,
	resident []sessionpkg.Info,
	trace *sessionReconcilerTraceCycle,
	stderr io.Writer,
) []startCandidate {
	if len(candidates) == 0 || cfg == nil || !cfg.Resources.Enabled() {
		return candidates
	}
	budgets, err := newResourceBudgets(cfg)
	if err != nil {
		resourceDeferrals.logBudgetError(err)
	}
	ledgers := budgets.newLedgers()
	if ledgers == nil {
		return candidates
	}
	candidateIDs := make(map[string]bool, len(candidates))
	for _, c := range candidates {
		candidateIDs[c.info.ID] = true
	}
	for _, info := range resident {
		if !candidateIDs[info.ID] {
			ledgers.charge(resourceHostOf(info), info.ID, normalizedSessionTemplateInfo(info, cfg))
		}
	}
	ordered := append([]startCandidate(nil), candidates...)
	sortCandidatesByWakeFairness(ordered)
	admitted := make(map[int]bool, len(ordered))
	for _, c := range ordered {
		v := ledgers.try(resourceHostOf(c.info), c.info.ID, c.logicalTemplate(cfg))
		key := "session:" + c.info.ID
		if v.fits() {
			admitted[c.order] = true
			resourceDeferrals.admitted(key, c.name(), stderr)
			continue
		}
		if trace != nil {
			trace.RecordDecision(TraceSiteReconcilerResourceBudget, v.outcome.reason(), TraceOutcomeDeferredByResourceBudget, c.tp.TemplateName, c.name(), v.payload())
		}
		resourceDeferrals.deferred(key, c.name(), v, stderr)
	}
	out := candidates[:0:0]
	for _, c := range candidates {
		if admitted[c.order] {
			out = append(out, c)
		}
	}
	return out
}

// resourceDeferralLog logs a deferral once when it starts and once when it
// ends, instead of once per tick.
type resourceDeferralLog struct {
	mu        sync.Mutex
	since     map[string]time.Time
	lastError string
	now       func() time.Time
}

var resourceDeferrals = &resourceDeferralLog{since: make(map[string]time.Time), now: time.Now}

func (d *resourceDeferralLog) deferred(key, name string, v resourceVerdict, w io.Writer) {
	d.deferredCause(key, name, v.String(), w)
}

func (d *resourceDeferralLog) deferredCause(key, name, why string, w io.Writer) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if _, ok := d.since[key]; ok {
		return
	}
	d.since[key] = d.now()
	if w != nil {
		fmt.Fprintf(w, "resource budget: deferring %s: %s\n", name, why) //nolint:errcheck // best-effort log
	}
}

func (d *resourceDeferralLog) admitted(key, name string, w io.Writer) {
	d.mu.Lock()
	defer d.mu.Unlock()
	since, ok := d.since[key]
	if !ok {
		return
	}
	delete(d.since, key)
	if w != nil {
		fmt.Fprintf(w, "resource budget: admitting %s after %s\n", name, d.now().Sub(since).Round(time.Second)) //nolint:errcheck // best-effort log
	}
}

func (d *resourceDeferralLog) logBudgetError(err error) {
	if err == nil {
		return
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.lastError == err.Error() {
		return
	}
	d.lastError = err.Error()
	fmt.Fprintf(os.Stderr, "resource budget: memory budget unavailable, class caps only: %v\n", err) //nolint:errcheck // best-effort log
}

func formatResourceBytes(n int64) string {
	if n < 0 {
		return "unlimited"
	}
	const gib = 1 << 30
	const mib = 1 << 20
	trim := func(f float64) string {
		return strings.TrimRight(strings.TrimRight(strconv.FormatFloat(f, 'f', 2, 64), "0"), ".")
	}
	if n >= gib {
		return trim(float64(n)/gib) + "GiB"
	}
	return trim(float64(n)/mib) + "MiB"
}

// admitPoolRequestToResourceBudget is the desired-state gate: a pool request
// the nested caps admitted is kept only when its class fits. A request that
// does not fit is not desired, so no session bead is created for it.
func admitPoolRequestToResourceBudget(l *resourceLedgers, req SessionRequest, trace *sessionReconcilerTraceCycle) bool {
	if l == nil {
		return true
	}
	// A pool request is not placed yet: it reserves on the local host.
	v := l.try(resourceLocalHost, req.SessionBeadID, req.Template)
	key := "pool:" + req.Template
	if v.fits() {
		resourceDeferrals.admitted(key, "new "+req.Template+" session", os.Stderr)
		return true
	}
	if trace != nil {
		payload := v.payload()
		payload["tier"] = req.Tier
		trace.RecordDecision(TraceSitePoolResourceBudget, v.outcome.reason(), TraceOutcomeRejected, req.Template, "", payload)
	}
	resourceDeferrals.deferred(key, "new "+req.Template+" session", v, os.Stderr)
	return false
}

// resourceResidentInfos returns the sessions that hold memory at the start
// boundary: believed running (active, awake, draining), or with a start in
// flight. A creating session whose start is not in flight is waiting, not
// resident.
func resourceResidentInfos(infos map[string]sessionpkg.Info, inFlight func(sessionpkg.Info) bool) []sessionpkg.Info {
	var out []sessionpkg.Info
	for _, info := range infos {
		if info.Closed {
			continue
		}
		switch sessionpkg.State(strings.TrimSpace(info.MetadataState)) {
		case sessionpkg.StateActive, sessionpkg.StateAwake, sessionpkg.StateDraining:
			out = append(out, info)
		case sessionpkg.StateCreating, sessionpkg.StateStartPending:
			if inFlight != nil && inFlight(info) {
				out = append(out, info)
			}
		}
	}
	return out
}
