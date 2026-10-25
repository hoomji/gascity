package api

import (
	"context"
	"fmt"
	"net/http"
	"sync"

	"github.com/danielgtaylor/huma/v2"
	"github.com/gastownhall/gascity/internal/api/apierr"
)

// defaultBDReadMaxConcurrent bounds how many list requests may hold the bd
// read semaphore at once, supervisor-wide. On 2026-10-24 a 2s dashboard poll
// with a 1.5s client timeout stacked ~1,100 abandoned bd children (each
// running 30s-16min) because nothing capped concurrent bd reads; 16 keeps a
// busy fleet of dashboard polls unthrottled while making that pile-up
// structurally impossible. Requests past the cap fail fast with a 503 and
// Retry-After instead of queueing behind the storm.
const defaultBDReadMaxConcurrent = 16

// bdReadRetryAfterSeconds is the Retry-After delta the saturated list paths
// advertise, matching the agent-visibility retryable precedent.
const bdReadRetryAfterSeconds = "1"

// bdReadGuard is the supervisor-wide backstop for the /sessions and /beads
// list paths: a non-blocking semaphore that 503s saturated reads plus a
// coalescing flight that runs one shared read per distinct request key. One
// guard is shared by every per-city Server (see
// SupervisorMux.sharedBDReadGuard) so a storm against one city cannot starve
// the semaphore city-by-city.
type bdReadGuard struct {
	sem chan struct{}

	mu    sync.Mutex
	calls map[string]*bdFlightCall
}

// bdFlightCall is one in-flight coalesced list read.
type bdFlightCall struct {
	done chan struct{} // closed when fn returns; val/err are stable after
	val  any
	err  error
	// cancel kills the shared read. It fires only when the last waiter
	// leaves, so one disconnect neither orphans a 16-minute child (the
	// pre-flight behavior) nor fails the followers still waiting on it.
	cancel context.CancelFunc
	// waiters counts joined callers that have not left yet, guarded by
	// bdReadGuard.mu.
	waiters int
}

func newBDReadGuard(maxConcurrent int) *bdReadGuard {
	if maxConcurrent <= 0 {
		maxConcurrent = defaultBDReadMaxConcurrent
	}
	return &bdReadGuard{sem: make(chan struct{}, maxConcurrent)}
}

// acquire takes one semaphore slot, failing fast with a 503 overload error
// when saturated. A dead context fails with ctx.Err() before touching the
// semaphore. The caller must call the returned release func.
func (g *bdReadGuard) acquire(ctx context.Context) (func(), error) {
	if g == nil {
		return func() {}, nil
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	select {
	case g.sem <- struct{}{}:
		return func() { <-g.sem }, nil
	default:
		return nil, bdReadOverloadError()
	}
}

// bdReadOverloadError is the 503 + Retry-After a saturated list read owes
// its caller. Both list routes already register 503 in their error statuses,
// so this changes no OpenAPI surface.
func bdReadOverloadError() error {
	return huma.ErrorWithHeaders(
		apierr.ServiceUnavailable.Msg("bd read concurrency saturated; retry shortly"),
		http.Header{"Retry-After": []string{bdReadRetryAfterSeconds}},
	)
}

// do runs fn coalesced by key: concurrent callers with the same key share one
// execution and each receives its result. The shared read runs on a context
// that is canceled when the last waiter leaves — a lone disconnect kills the
// backing bd child (via the ctx-bound store reads inside fn), while followers
// keep a shared read alive for each other. A caller whose own context dies
// first returns ctx.Err() without disturbing the rest.
func (g *bdReadGuard) do(ctx context.Context, key string, fn func(context.Context) (any, error)) (any, error) {
	if g == nil {
		return fn(ctx)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	g.mu.Lock()
	call, ok := g.calls[key]
	if !ok {
		// WithoutCancel first: the shared read must outlive any one
		// waiter's context (a leader disconnect must not fail followers),
		// then WithCancel so the last waiter out still kills it.
		callCtx, cancel := context.WithCancel(context.WithoutCancel(ctx))
		call = &bdFlightCall{done: make(chan struct{}), cancel: cancel}
		if g.calls == nil {
			g.calls = make(map[string]*bdFlightCall)
		}
		g.calls[key] = call
		go func() {
			defer func() {
				// A panic must fail waiters, never hang them on done.
				if r := recover(); r != nil {
					call.err = fmt.Errorf("coalesced list read panicked: %v", r)
				}
				close(call.done)
				g.mu.Lock()
				if g.calls[key] == call {
					delete(g.calls, key)
				}
				g.mu.Unlock()
			}()
			call.val, call.err = fn(callCtx)
		}()
	}
	call.waiters++
	g.mu.Unlock()

	select {
	case <-ctx.Done():
		g.leave(key, call)
		return nil, ctx.Err()
	case <-call.done:
		g.leave(key, call)
		return call.val, call.err
	}
}

// leave drops one waiter; the last waiter out cancels the shared read so an
// abandoned coalesced read cannot outlive every client that wanted it. It
// also detaches the flight from the key, so a caller that arrives after a
// mass disconnect starts a fresh read instead of joining one already
// canceled by departed waiters (which would serve it a spurious
// cancellation error). The fn goroutine's deferred cleanup re-deletes
// idempotently.
func (g *bdReadGuard) leave(key string, call *bdFlightCall) {
	g.mu.Lock()
	defer g.mu.Unlock()
	call.waiters--
	if call.waiters <= 0 {
		call.waiters = 0
		call.cancel()
		if g.calls[key] == call {
			delete(g.calls, key)
		}
	}
}

// acquireBDRead takes one bd-read semaphore slot for a list request. A nil
// Server or guard (zero-value Servers in tests) skips the bound.
func (s *Server) acquireBDRead(ctx context.Context) (func(), error) {
	if s == nil || s.bdRead == nil {
		return func() {}, nil
	}
	return s.bdRead.acquire(ctx)
}

// coalesceList runs fn coalesced by key through the shared read guard. A nil
// Server or guard runs fn directly. See bdReadGuard.do for the waiter and
// cancellation contract.
func (s *Server) coalesceList(ctx context.Context, key string, fn func(context.Context) (any, error)) (any, error) {
	if s == nil || s.bdRead == nil {
		return fn(ctx)
	}
	return s.bdRead.do(ctx, key, fn)
}

// beadListFlightKey is the coalescing key for GET /beads: every input that
// shapes the response, city-scoped because the guard is supervisor-wide.
// blocking is part of the key because it shapes the body: blocking callers
// bypass the response cache so the body reflects the event they waited for,
// and sharing a flight with a non-blocking caller would serve them a cached
// body that predates it. The raw Index/Wait values stay out: the per-caller
// event wait runs outside the flight, so only the bypass bit reaches the
// shared read.
func beadListFlightKey(city string, input *BeadListInput, blocking bool) string {
	return fmt.Sprintf("beads:v1:%q:%q:%q:%q:%q:%q:%t:%t:%q:%d",
		city, input.Status, input.Type, input.Label, input.Assignee, input.Rig,
		input.All, blocking, input.Cursor, input.Limit)
}

// sessionListFlightKey is the coalescing key for GET /sessions: every input
// that shapes the response, city-scoped because the guard is supervisor-wide.
func sessionListFlightKey(city string, input *SessionListInput) string {
	return fmt.Sprintf("sessions:v1:%q:%q:%q:%t:%q:%d",
		city, input.State, input.Template, input.Peek, input.Cursor, input.Limit)
}
