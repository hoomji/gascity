package main

import (
	"encoding/json"
	"testing"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/events"
	"github.com/gastownhall/gascity/internal/session"
)

func beadEventForTest(t *testing.T, eventType string, bead beads.Bead) events.Event {
	t.Helper()
	payload, err := json.Marshal(bead)
	if err != nil {
		t.Fatal(err)
	}
	return events.Event{Type: eventType, Subject: bead.ID, Payload: payload}
}

// A session bead and an order-tracking bead are never what a workflow waits on,
// so their events do not wake the serve loop into a ready scan; a workflow
// bead's do, and so does an event with nothing to say what it touched.
func TestWorkflowEventRelevantIgnoresSessionAndOrderTrackingBeads(t *testing.T) {
	cases := []struct {
		name  string
		evt   events.Event
		relev bool
	}{
		{"session updated", beadEventForTest(t, events.BeadUpdated, beads.Bead{ID: "s-1", Labels: []string{session.LabelSession}}), false},
		{"tracking created", beadEventForTest(t, events.BeadCreated, beads.Bead{ID: "t-1", Labels: []string{"order-run:x", labelOrderTracking}}), false},
		{"tracking closed", beadEventForTest(t, events.BeadClosed, beads.Bead{ID: "t-1", Labels: []string{labelOrderTracking}}), false},
		{"workflow step updated", beadEventForTest(t, events.BeadUpdated, beads.Bead{ID: "w-1", Type: "task"}), true},
		{"workflow step closed", beadEventForTest(t, events.BeadClosed, beads.Bead{ID: "w-2", Type: "task"}), true},
		{"payloadless update", events.Event{Type: events.BeadUpdated, Subject: "s-1"}, true},
		{"unrelated type", events.Event{Type: events.SessionStopped}, false},
	}
	for _, tc := range cases {
		if got := workflowEventRelevant(tc.evt); got != tc.relev {
			t.Errorf("%s: workflowEventRelevant = %t, want %t", tc.name, got, tc.relev)
		}
	}
}
