package main

import (
	"io"
	"strings"

	"github.com/gastownhall/gascity/internal/config"
	sessionpkg "github.com/gastownhall/gascity/internal/session"
)

// Resource admission in the reconcile v2 path
// (engdocs/proposals/resource-budgets.md, "Reconcile v2"). admit checks every
// start (pool, named, resumed) and every create against the host ledgers as
// one more cap class, after its other caps: a deferred start or create makes
// no write and spends no wake token. Pool demand is also gated earlier, in
// computePoolDesiredStatesAt, which v2 shares with the legacy path.

// causeResourcePrefix prefixes a resource deferral's cause; the rest is the
// ledger's trace reason (resource_budget_full, resource_queued_behind, ...).
const causeResourcePrefix = "resource/"

// resourceRow is one reservation admission reads: a row's (or an in-flight
// create's) host and logical template.
type resourceRow struct {
	Key      rowKey
	Host     string
	Template string
}

// resourceAdmitInput is what admit reads of the resource budgets. Nil
// Budgets means no [resources] table: admit checks nothing.
type resourceAdmitInput struct {
	Budgets resourceBudgets
	// Rows is every census row's host and template, for the start intents.
	Rows map[rowKey]resourceRow
	// Resident are the reservations already held: rows believed running
	// (active, awake, draining), rows with a start in flight, and in-flight
	// creates no census row carries yet.
	Resident []resourceRow
}

// resourceAdmitInputOf builds admit's resource input from the pass's census
// and in-flight view. It reads the host budget (memory = "auto" reads
// /proc/meminfo), so it runs in the planner, outside the pure admit.
func resourceAdmitInputOf(cfg *config.City, rows []censusRow, inFlight inflightView) resourceAdmitInput {
	budgets, err := newResourceBudgets(cfg)
	if err != nil {
		resourceDeferrals.logBudgetError(err)
	}
	if len(budgets) == 0 {
		return resourceAdmitInput{}
	}
	starting := make(map[rowKey]bool)
	for _, e := range inFlight.Entries {
		if e.Kind == inflightStart {
			starting[e.Key] = true
		}
	}
	in := resourceAdmitInput{Budgets: budgets, Rows: make(map[rowKey]resourceRow, len(rows))}
	tokens := make(map[string]bool)
	for _, row := range rows {
		r := resourceRow{Key: row.Key, Host: resourceHostOf(row.Info), Template: normalizedSessionTemplateInfo(row.Info, cfg)}
		in.Rows[row.Key] = r
		if row.InstanceToken != "" {
			tokens[row.InstanceToken] = true
		}
		if row.Info.Closed {
			continue
		}
		switch sessionpkg.State(strings.TrimSpace(row.Info.MetadataState)) {
		case sessionpkg.StateActive, sessionpkg.StateAwake, sessionpkg.StateDraining:
			in.Resident = append(in.Resident, r)
		default:
			if starting[row.Key] {
				in.Resident = append(in.Resident, r)
			}
		}
	}
	for _, e := range inFlight.Entries {
		if e.Kind == inflightCreate && !e.Ambiguous && !tokens[e.Token] {
			in.Resident = append(in.Resident, resourceRow{Host: resourceLocalHost, Template: e.Template})
		}
	}
	return in
}

// ledgers returns the pass's host ledgers charged with every resident
// reservation except the rows a start in intents brings up (a start's row is
// not running yet); nil when resource admission is disabled.
//
//gc:pure
func (in resourceAdmitInput) ledgers(intents []intent) *resourceLedgers {
	ls := in.Budgets.newLedgers()
	if ls == nil {
		return nil
	}
	starts := make(map[rowKey]bool)
	for _, it := range intents {
		if spec, ok := effectSpecs[it.Kind]; ok && spec.class == capStarts && it.Key.ID != "" {
			starts[it.Key] = true
		}
	}
	for _, r := range in.Resident {
		if r.Key.ID != "" && starts[r.Key] {
			continue
		}
		ls.charge(r.Host, r.Key.ID, r.Template)
	}
	return ls
}

// resourceRequest is the host, row ID and template an admitted start or
// create would reserve.
func (a *admission) resourceRequest(it intent) (host, id, template string) {
	if it.Key.ID == "" {
		// A create is not placed yet: it reserves on the local host.
		return resourceLocalHost, "", it.CreatePlan.Template
	}
	r, ok := a.in.Resources.Rows[it.Key]
	if !ok {
		return resourceLocalHost, it.Key.ID, it.CreatePlan.Template
	}
	return r.Host, it.Key.ID, r.Template
}

// resourceCause is why it does not fit its host's budget, or "". It does
// not charge: commitResource charges an intent its other caps admitted.
// A memory miss still blocks later requests on the host (head-of-line).
func (a *admission) resourceCause(it intent) string {
	if a.resources == nil {
		return ""
	}
	host, id, template := a.resourceRequest(it)
	l := a.resources.ledger(host)
	if l == nil {
		return ""
	}
	v := l.check(id, template)
	if v.fits() {
		return ""
	}
	if v.outcome == resourceBudgetFull {
		l.blocked = true
	}
	return causeResourcePrefix + string(v.outcome.reason())
}

// commitResource charges an admitted start or create to its host.
func (a *admission) commitResource(it intent) {
	if a.resources == nil {
		return
	}
	host, id, template := a.resourceRequest(it)
	a.resources.charge(host, id, template)
}

// logResourceDeferrals writes one controller log line when a v2 start or
// create starts waiting on the resource budget and one when it is admitted.
func logResourceDeferrals(res admitResult, rows map[rowKey]resourceRow, stderr io.Writer) {
	if rows == nil {
		return
	}
	for _, it := range res.Deferred {
		if !strings.HasPrefix(it.Cause, causeResourcePrefix) {
			continue
		}
		key, name := resourceLogKey(it, rows)
		resourceDeferrals.deferredCause(key, name, strings.TrimPrefix(it.Cause, causeResourcePrefix), stderr)
	}
	for _, it := range res.Admitted {
		key, name := resourceLogKey(it, rows)
		resourceDeferrals.admitted(key, name, stderr)
	}
}

func resourceLogKey(it intent, rows map[rowKey]resourceRow) (key, name string) {
	if it.Key.ID == "" {
		return "pool:" + it.CreatePlan.Template, "new " + it.CreatePlan.Template + " session"
	}
	return "session:" + it.Key.ID, rows[it.Key].Template + " session " + it.Key.ID
}
