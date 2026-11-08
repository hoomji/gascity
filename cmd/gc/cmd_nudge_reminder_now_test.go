package main

import (
	"strings"
	"testing"
)

// SESSION-REMINDER-001: injected deferred reminders, one or many, ask for
// action in the current turn, not after it (ci-d1t8).
func TestNudgeInjectRequestsImmediateAction(t *testing.T) {
	for _, items := range [][]queuedNudge{
		{{Source: "mail", Message: "one"}},
		{{Source: "mail", Message: "one"}, {Source: "wait", Message: "two"}},
	} {
		out := formatNudgeInjectOutput(items)
		if !strings.Contains(out, "Handle these reminders now, before responding to the user.") {
			t.Fatalf("%d reminder(s) do not ask for action now:\n%s", len(items), out)
		}
		if strings.Contains(out, "after this turn") {
			t.Fatalf("%d reminder(s) still defer to after this turn:\n%s", len(items), out)
		}
	}
}
