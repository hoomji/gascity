package beads

import (
	"context"
)

// ContextCommandRunner executes a command in the given directory bound to
// ctx, returning stdout bytes. It is the per-call-context twin of
// CommandRunner: canceling ctx kills the child (via the same
// killCommandTree path the fixed-parent runners use) instead of leaving it
// to run past the caller's budget. A nil ctx behaves like
// context.Background.
type ContextCommandRunner func(ctx context.Context, dir, name string, args ...string) ([]byte, error)

// ExecContextCommandRunnerWithEnv returns a ContextCommandRunner that uses
// os/exec and applies the provided environment overrides, binding every
// invocation to the ctx it is called with. Per-command timeouts, telemetry,
// and BD_BIN pinning match ExecCommandRunnerWithEnv exactly; both are thin
// wrappers over runExecCommand.
func ExecContextCommandRunnerWithEnv(env map[string]string) ContextCommandRunner {
	return execContextCommandRunner(env, false, processEnvSnapshotExcludingNativeDoltOpen)
}

// ExecContextCommandRunnerWithEnvWithoutAmbientBeads is the
// WithoutAmbientBeads form of ExecContextCommandRunnerWithEnv.
func ExecContextCommandRunnerWithEnvWithoutAmbientBeads(env map[string]string) ContextCommandRunner {
	return execContextCommandRunner(env, true, processEnvSnapshotExcludingNativeDoltOpen)
}

func execContextCommandRunner(env map[string]string, withoutAmbientBeads bool, baseEnvFn func() []string) ContextCommandRunner {
	return func(ctx context.Context, dir, name string, args ...string) ([]byte, error) {
		if ctx == nil {
			ctx = context.Background()
		}
		return runExecCommand(ctx, dir, name, args, env, withoutAmbientBeads, baseEnvFn)
	}
}

// ContextLister is an optional Store capability: List with cancellation.
// Implementations must answer identically to List for a live context, and
// must surface a dead context as ctx.Err() — never as an empty result — so
// a deadline-sensitive caller can tell "no rows" from "we stopped looking"
// (the same contract as Counter and ContextReadyReader).
//
// Stores that shell out (BdStore) kill the in-flight child on cancellation;
// in-process stores honor at least the pre-read check. Callers must use
// ListWithContext rather than asserting this directly so stores without the
// capability still get the pre-read check.
type ContextLister interface {
	ListContext(ctx context.Context, query ListQuery) ([]Bead, error)
}

// ListWithContext lists through the context-aware path when the store
// implements it, else honors a dead context and falls back to List. A nil
// ctx behaves like context.Background.
func ListWithContext(ctx context.Context, store Store, query ListQuery) ([]Bead, error) {
	if ctx == nil {
		ctx = context.Background()
	}
	if lister, ok := store.(ContextLister); ok {
		return lister.ListContext(ctx, query)
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	return store.List(query)
}
