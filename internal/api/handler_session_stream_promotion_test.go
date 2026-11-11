package api

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/events"
	"github.com/gastownhall/gascity/internal/session"
	"github.com/gastownhall/gascity/internal/worker"
)

func TestSessionStreamResumeTokenPrefersLastEventID(t *testing.T) {
	for _, tc := range []struct {
		name        string
		lastEventID string
		afterCursor string
		want        string
	}{
		{name: "query only", afterCursor: "query-cursor", want: "query-cursor"},
		{name: "header only", lastEventID: "header-cursor", want: "header-cursor"},
		{name: "header wins", lastEventID: " header-cursor ", afterCursor: "query-cursor", want: "header-cursor"},
		{name: "blank header falls back", lastEventID: "  ", afterCursor: " query-cursor ", want: "query-cursor"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := sessionStreamResumeToken(tc.lastEventID, tc.afterCursor); got != tc.want {
				t.Fatalf("sessionStreamResumeToken(%q, %q) = %q, want %q", tc.lastEventID, tc.afterCursor, got, tc.want)
			}
		})
	}
}

func TestStructuredPeekPromotionClearsResolvedPendingInteraction(t *testing.T) {
	for _, transport := range []string{"legacy", "huma"} {
		t.Run(transport, func(t *testing.T) {
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()

			handle := &structuredPromotionHandle{}
			info := session.Info{
				ID:          "session-1",
				SessionKey:  "provider-session-1",
				SessionName: "worker-1",
				Template:    "worker",
				Provider:    "test",
			}
			srv := &Server{}
			done := make(chan struct{})
			cleared := make(chan struct{}, 1)

			switch transport {
			case "legacy":
				rec := newSyncResponseRecorder()
				go func() {
					srv.streamSessionPeekStructured(ctx, rec, info, handle, false, "")
					close(done)
				}()
				go func() {
					deadline := time.NewTicker(5 * time.Millisecond)
					defer deadline.Stop()
					for {
						select {
						case <-ctx.Done():
							return
						case <-deadline.C:
							if strings.Contains(rec.BodyString(), "event: pending_cleared") {
								cleared <- struct{}{}
								return
							}
						}
					}
				}()
			case "huma":
				go func() {
					srv.streamSessionPeekStructuredHuma(ctx, func(msg StringIDMessage) error {
						if _, ok := msg.Data.(SessionPendingClearedEvent); ok {
							cleared <- struct{}{}
						}
						return nil
					}, info, handle, false, "")
					close(done)
				}()
			}

			select {
			case <-cleared:
			case <-time.After(250 * time.Millisecond):
				t.Fatal("fallback-to-history promotion did not clear the resolved pending interaction")
			}
			cancel()
			select {
			case <-done:
			case <-time.After(time.Second):
				t.Fatal("structured stream did not stop after cancellation")
			}
		})
	}
}

// TestSessionPeekRawPendingCarriesReadTimeLineage proves the raw stream's
// pending event carries the same read-time bead lineage as every other
// stream variant: when the session has an active bead, the event's lead is
// that bead. Transports of one stream must not disagree on the shape.
func TestSessionPeekRawPendingCarriesReadTimeLineage(t *testing.T) {
	state := newFakeState(t)
	store := beads.NewMemStore()
	state.stores = map[string]beads.Store{"a": store}
	work, err := store.Create(beads.Bead{Title: "helper work"})
	if err != nil {
		t.Fatalf("Create work: %v", err)
	}
	inProgress := "in_progress"
	assignee := "worker-session"
	if err := store.Update(work.ID, beads.UpdateOpts{Status: &inProgress, Assignee: &assignee}); err != nil {
		t.Fatalf("Update work: %v", err)
	}

	srv := New(state)
	info := session.Info{ID: "worker-session", SessionName: "worker-1", Template: "worker", Provider: "test"}
	handle := &mutableStructuredPendingHandle{output: "peek output"}
	handle.SetPending(&worker.PendingInteraction{RequestID: "request-1", Kind: "approval", Prompt: "Proceed?"})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	rec := newSyncResponseRecorder()
	done := make(chan struct{})
	go func() {
		srv.streamSessionPeekRaw(ctx, rec, info, handle)
		close(done)
	}()
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(hangBudget):
			t.Error("raw stream did not stop after cancellation")
		}
	}()

	want := `"lead":"` + work.ID + `"`
	if body := waitForRecorderSubstring(t, rec, want, 3*time.Second); !strings.Contains(body, want) {
		t.Fatalf("raw stream body missing pending lead %q: %s", work.ID, body)
	}
}

// TestSessionPeekRawPendingRefreshesLineageOnNewRequest proves the raw
// stream re-resolves its bead lineage when a new pending request ID appears
// (gascity#90 review F2): after the session's work moves from bead A to bead
// B mid-stream, the next pending event carries B as its lead instead of the
// stream's initial, now-stale lead.
func TestSessionPeekRawPendingRefreshesLineageOnNewRequest(t *testing.T) {
	state := newFakeState(t)
	store := beads.NewMemStore()
	state.stores = map[string]beads.Store{"a": store}
	beadA, err := store.Create(beads.Bead{Title: "first work"})
	if err != nil {
		t.Fatalf("Create bead A: %v", err)
	}
	inProgress := "in_progress"
	assignee := "worker-session"
	if err := store.Update(beadA.ID, beads.UpdateOpts{Status: &inProgress, Assignee: &assignee}); err != nil {
		t.Fatalf("Update bead A: %v", err)
	}

	srv := New(state)
	info := session.Info{ID: "worker-session", SessionName: "worker-1", Template: "worker", Provider: "test"}
	handle := &mutableStructuredPendingHandle{output: "peek output"}
	handle.SetPending(&worker.PendingInteraction{RequestID: "request-1", Kind: "approval", Prompt: "Proceed?"})

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	rec := newSyncResponseRecorder()
	done := make(chan struct{})
	go func() {
		srv.streamSessionPeekRaw(ctx, rec, info, handle)
		close(done)
	}()
	defer func() {
		cancel()
		select {
		case <-done:
		case <-time.After(hangBudget):
			t.Error("raw stream did not stop after cancellation")
		}
	}()

	wantA := `"lead":"` + beadA.ID + `"`
	if body := waitForRecorderSubstring(t, rec, wantA, 3*time.Second); !strings.Contains(body, wantA) {
		t.Fatalf("raw stream body missing pending lead %q: %s", beadA.ID, body)
	}

	// The session's work moves to a newer bead mid-stream; the next
	// pending request must carry the fresh lead.
	movedAway := "someone-else"
	if err := store.Update(beadA.ID, beads.UpdateOpts{Assignee: &movedAway}); err != nil {
		t.Fatalf("Update bead A: %v", err)
	}
	beadB, err := store.Create(beads.Bead{Title: "second work"})
	if err != nil {
		t.Fatalf("Create bead B: %v", err)
	}
	if err := store.Update(beadB.ID, beads.UpdateOpts{Status: &inProgress, Assignee: &assignee}); err != nil {
		t.Fatalf("Update bead B: %v", err)
	}
	handle.SetPending(&worker.PendingInteraction{RequestID: "request-2", Kind: "approval", Prompt: "Proceed again?"})
	state.eventProv.(*events.Fake).Record(events.Event{Type: events.WorkerOperation, Subject: info.ID})

	wantB := `"lead":"` + beadB.ID + `"`
	if body := waitForRecorderSubstring(t, rec, wantB, 3*time.Second); !strings.Contains(body, wantB) {
		t.Fatalf("raw stream body missing refreshed pending lead %q: %s", beadB.ID, body)
	}
}

// TestStructuredPeekPendingRefreshesLineageOnNewRequest proves the structured
// peek stream, on both transports, re-resolves its bead lineage when a new
// pending request ID appears (gascity#90 review F2): after the session's
// work moves mid-stream, the next pending event carries the new lead.
func TestStructuredPeekPendingRefreshesLineageOnNewRequest(t *testing.T) {
	for _, transport := range []string{"legacy", "huma"} {
		t.Run(transport, func(t *testing.T) {
			fs := newSessionFakeState(t)
			store := beads.NewMemStore()
			fs.stores = map[string]beads.Store{"a": store}
			beadA, err := store.Create(beads.Bead{Title: "first work"})
			if err != nil {
				t.Fatalf("Create bead A: %v", err)
			}
			inProgress := "in_progress"
			assignee := "session-1"
			if err := store.Update(beadA.ID, beads.UpdateOpts{Status: &inProgress, Assignee: &assignee}); err != nil {
				t.Fatalf("Update bead A: %v", err)
			}

			srv := New(fs)
			info := session.Info{ID: "session-1", SessionName: "worker-1", Template: "worker", Provider: "test"}
			handle := &mutableStructuredPendingHandle{output: "fallback output"}
			handle.SetPending(&worker.PendingInteraction{RequestID: "request-1", Kind: "approval", Prompt: "Proceed?"})

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan struct{})
			var waitForLead func(string)

			switch transport {
			case "legacy":
				rec := newSyncResponseRecorder()
				go func() {
					srv.streamSessionPeekStructured(ctx, rec, info, handle, false, "")
					close(done)
				}()
				waitForLead = func(lead string) {
					want := `"lead":"` + lead + `"`
					if body := waitForRecorderSubstring(t, rec, want, hangBudget); !strings.Contains(body, want) {
						t.Fatalf("structured stream body missing pending lead %q: %s", lead, body)
					}
				}
			case "huma":
				leads := make(chan string, 4)
				go func() {
					srv.streamSessionPeekStructuredHuma(ctx, func(msg StringIDMessage) error {
						raw, _ := json.Marshal(msg.Data)
						var pending struct {
							RequestID string `json:"request_id"`
							Lead      string `json:"lead"`
						}
						if json.Unmarshal(raw, &pending) == nil && pending.RequestID != "" && pending.Lead != "" {
							leads <- pending.Lead
						}
						return nil
					}, info, handle, false, "")
					close(done)
				}()
				waitForLead = func(want string) {
					select {
					case got := <-leads:
						if got != want {
							t.Fatalf("pending lead = %q, want %q", got, want)
						}
					case <-time.After(hangBudget):
						t.Fatalf("structured stream missing pending lead %q", want)
					}
				}
			}

			waitForLead(beadA.ID)

			movedAway := "someone-else"
			if err := store.Update(beadA.ID, beads.UpdateOpts{Assignee: &movedAway}); err != nil {
				t.Fatalf("Update bead A: %v", err)
			}
			beadB, err := store.Create(beads.Bead{Title: "second work"})
			if err != nil {
				t.Fatalf("Create bead B: %v", err)
			}
			if err := store.Update(beadB.ID, beads.UpdateOpts{Status: &inProgress, Assignee: &assignee}); err != nil {
				t.Fatalf("Update bead B: %v", err)
			}
			handle.SetPending(&worker.PendingInteraction{RequestID: "request-2", Kind: "approval", Prompt: "Proceed again?"})
			fs.eventProv.(*events.Fake).Record(events.Event{Type: events.WorkerOperation, Subject: info.ID})
			waitForLead(beadB.ID)

			cancel()
			select {
			case <-done:
			case <-time.After(hangBudget):
				t.Fatal("structured stream did not stop after cancellation")
			}
		})
	}
}

func TestStructuredPeekEmitsSameRequestPendingUpdates(t *testing.T) {
	for _, transport := range []string{"legacy", "huma"} {
		t.Run(transport, func(t *testing.T) {
			fs := newSessionFakeState(t)
			srv := New(fs)
			info := session.Info{ID: "session-1", SessionName: "worker-1", Template: "worker", Provider: "test"}
			handle := &mutableStructuredPendingHandle{output: "fallback output"}
			handle.SetPending(&worker.PendingInteraction{RequestID: "request-1", Kind: "approval", Prompt: "Proceed?"})

			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			done := make(chan struct{})
			var waitForPrompt func(string)

			switch transport {
			case "legacy":
				rec := newSyncResponseRecorder()
				go func() {
					srv.streamSessionPeekStructured(ctx, rec, info, handle, false, "")
					close(done)
				}()
				waitForPrompt = func(prompt string) {
					if body := waitForRecorderSubstring(t, rec, prompt, hangBudget); !strings.Contains(body, prompt) {
						t.Fatalf("structured stream body missing pending prompt %q: %s", prompt, body)
					}
				}
			case "huma":
				prompts := make(chan string, 2)
				go func() {
					srv.streamSessionPeekStructuredHuma(ctx, func(msg StringIDMessage) error {
						raw, _ := json.Marshal(msg.Data)
						var pending struct {
							Prompt string `json:"prompt"`
						}
						if json.Unmarshal(raw, &pending) == nil && pending.Prompt != "" {
							prompts <- pending.Prompt
						}
						return nil
					}, info, handle, false, "")
					close(done)
				}()
				waitForPrompt = func(want string) {
					select {
					case got := <-prompts:
						if got != want {
							t.Fatalf("pending prompt = %q, want %q", got, want)
						}
					case <-time.After(hangBudget):
						t.Fatalf("structured stream missing pending prompt %q", want)
					}
				}
			}

			waitForPrompt("Proceed?")
			handle.SetPending(&worker.PendingInteraction{RequestID: "request-1", Kind: "approval", Prompt: "Updated prompt"})
			fs.eventProv.(*events.Fake).Record(events.Event{Type: events.WorkerOperation, Subject: info.ID})
			waitForPrompt("Updated prompt")

			cancel()
			select {
			case <-done:
			case <-time.After(hangBudget):
				t.Fatal("structured stream did not stop after cancellation")
			}
		})
	}
}

type structuredPromotionHandle struct {
	worker.Handle

	mu           sync.Mutex
	pendingReads int
}

func (h *structuredPromotionHandle) Peek(context.Context, int) (string, error) {
	return "fallback output", nil
}

func (h *structuredPromotionHandle) History(context.Context, worker.HistoryRequest) (*worker.HistorySnapshot, error) {
	return &worker.HistorySnapshot{
		GCSessionID:           "provider-session-1",
		LogicalConversationID: "provider-session-1",
		ProviderSessionID:     "provider-session-1",
		TranscriptStreamID:    "stream-1",
		Generation:            worker.Generation{ID: "generation-1"},
		Cursor:                worker.Cursor{AfterEntryID: "history-1"},
		Continuity:            worker.Continuity{Status: worker.ContinuityStatusContinuous},
		TailState: worker.TailState{
			Activity:    worker.TailActivityIdle,
			LastEntryID: "history-1",
		},
		Entries: []worker.HistoryEntry{{
			ID:     "history-1",
			Kind:   "assistant",
			Actor:  worker.ActorAssistant,
			Status: worker.ResultStatusFinal,
			Text:   "history output",
		}},
	}, nil
}

func (h *structuredPromotionHandle) Pending(context.Context) (*worker.PendingInteraction, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.pendingReads++
	if h.pendingReads == 1 {
		return &worker.PendingInteraction{RequestID: "request-1", Kind: "approval", Prompt: "Proceed?"}, nil
	}
	return nil, nil
}

func (h *structuredPromotionHandle) TranscriptPath(context.Context) (string, error) {
	return "", errors.New("no transcript path for synthetic promotion test")
}

var _ worker.Handle = (*structuredPromotionHandle)(nil)

type mutableStructuredPendingHandle struct {
	worker.Handle

	mu      sync.Mutex
	output  string
	pending *worker.PendingInteraction
}

func (h *mutableStructuredPendingHandle) Peek(context.Context, int) (string, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.output, nil
}

func (h *mutableStructuredPendingHandle) History(context.Context, worker.HistoryRequest) (*worker.HistorySnapshot, error) {
	return nil, worker.ErrHistoryUnavailable
}

func (h *mutableStructuredPendingHandle) Pending(context.Context) (*worker.PendingInteraction, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return clonePendingInteraction(h.pending), nil
}

func (h *mutableStructuredPendingHandle) TranscriptPath(context.Context) (string, error) {
	return "", worker.ErrHistoryUnavailable
}

func (h *mutableStructuredPendingHandle) SetPending(pending *worker.PendingInteraction) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.pending = clonePendingInteraction(pending)
}

func clonePendingInteraction(pending *worker.PendingInteraction) *worker.PendingInteraction {
	if pending == nil {
		return nil
	}
	cloned := *pending
	cloned.Options = append([]string(nil), pending.Options...)
	cloned.Metadata = cloneStringMap(pending.Metadata)
	return &cloned
}

var _ worker.Handle = (*mutableStructuredPendingHandle)(nil)
