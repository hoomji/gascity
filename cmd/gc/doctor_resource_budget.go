package main

import (
	"fmt"
	"strings"

	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/doctor"
)

// resourceBudgetDoctorCheck reports the host memory budget and resource
// classes (engdocs/proposals/resource-budgets.md), and warns when an agent's
// class can never start on this host.
type resourceBudgetDoctorCheck struct {
	cfg *config.City
}

func newResourceBudgetDoctorCheck(cfg *config.City) *resourceBudgetDoctorCheck {
	return &resourceBudgetDoctorCheck{cfg: cfg}
}

// Name implements doctor.Check.
func (*resourceBudgetDoctorCheck) Name() string { return "resource-budget" }

// CanFix implements doctor.Check. Budgets and classes are operator policy.
func (*resourceBudgetDoctorCheck) CanFix() bool { return false }

// WarmupEligible implements doctor.Check. It reads only parsed config and
// the host's total memory.
func (*resourceBudgetDoctorCheck) WarmupEligible() bool { return true }

// Fix implements doctor.Check.
func (*resourceBudgetDoctorCheck) Fix(_ *doctor.CheckContext) error { return nil }

// Run implements doctor.Check.
func (c *resourceBudgetDoctorCheck) Run(_ *doctor.CheckContext) *doctor.CheckResult {
	const name = "resource-budget"
	if c.cfg == nil || !c.cfg.Resources.Enabled() {
		return okCheck(name, "no [resources] table; resource admission off")
	}
	budget, err := newResourceBudget(c.cfg, resourceLocalHost)
	if err != nil {
		return warnCheck(name, fmt.Sprintf("memory budget unavailable (%v); only class caps apply", err),
			"set [resources] memory to an explicit size", nil)
	}
	agentsByClass := make(map[string][]string)
	for i := range c.cfg.Agents {
		a := &c.cfg.Agents[i]
		if class := c.cfg.Resources.EffectiveResourceClass(a); class != "" {
			agentsByClass[class] = append(agentsByClass[class], a.QualifiedName())
		}
	}
	var details, never []string
	for _, class := range c.cfg.Resources.ClassNames() {
		spec := budget.classes[class]
		limit := "no class cap"
		if spec.maxActive >= 0 {
			limit = fmt.Sprintf("max %d", spec.maxActive)
		}
		agents := agentsByClass[class]
		details = append(details, fmt.Sprintf("class %s: %s, %s; agents: %s",
			class, formatResourceBytes(spec.memory), limit, orNone(agents)))
		if len(agents) > 0 && budget.neverFits(spec) {
			never = append(never, fmt.Sprintf("%s (%s)", class, strings.Join(agents, ", ")))
		}
	}
	noun := "classes"
	if len(budget.classes) == 1 {
		noun = "class"
	}
	summary := fmt.Sprintf("budget %s after reserve, %d %s", formatResourceBytes(budget.capacity), len(budget.classes), noun)
	if len(never) > 0 {
		return warnCheck(name, summary+"; classes that never start on this host: "+strings.Join(never, "; "),
			"raise [resources] memory, lower the class reservation, or move the agents to another class", details)
	}
	res := okCheck(name, summary)
	res.Details = details
	return res
}

func orNone(items []string) string {
	if len(items) == 0 {
		return "none"
	}
	return strings.Join(items, ", ")
}
