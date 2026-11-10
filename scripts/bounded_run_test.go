package scripts_test

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// The pre-push suite's `go test` runs must each die by a hard wall-clock
// bound owned outside the go toolchain (ci-gke0 fix 2). On 2026-11-06 a push
// wedged 27+ minutes past `-timeout 20m`: the parent `go test` sat in futex
// sleep with no children and no timeout panic, and only `push --no-verify`
// got the push out. `scripts/bounded-run` is that outer bound: it runs its
// product in a supervised process group (scripts/lib/harness-reap.sh),
// kills the group past the deadline (SIGQUIT for a goroutine dump, then
// SIGKILL), and reports the bound, the processes still running at kill time,
// and how to rerun. The cmd/gc shards already had this supervision in
// scripts/test-go-test-shard; the unit-core sweep did not, and the push-time
// suite had no bound of its own.

// TestBoundedRunKillsSIGTERMIgnoringPipeHoldingTree drives the bounded
// wrapper with a product that ignores every catchable signal and parks a
// grandchild on the caller's stdout pipe — the shape that defeats both go's
// in-process timeout and a TERM-only `timeout(1)`. The wrapper must exit
// non-zero shortly after its bound, name the bound, the still-running
// processes, and the rerun, and leave no survivor holding the pipe.
func TestBoundedRunKillsSIGTERMIgnoringPipeHoldingTree(t *testing.T) {
	root := repoRoot(t)
	boundedRun := filepath.Join(root, "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}

	pidFile := filepath.Join(t.TempDir(), "grandchild.pid")
	product := fmt.Sprintf(`trap '' TERM INT QUIT HUP
sleep %d &
echo $! > %q
exec sleep %d
`, fixtureLifetimeSeconds, pidFile, fixtureLifetimeSeconds)

	stdout, stdoutWriter, err := os.Pipe()
	if err != nil {
		t.Fatalf("create stdout pipe: %v", err)
	}
	t.Cleanup(func() { _ = stdout.Close() })

	cmd := exec.Command(boundedRun, "5s", "test-bound",
		"--quit-grace", "2",
		"--rerun-hint", "rerun-hint-marker",
		"--", "sh", "-c", product)
	cmd.Dir = root
	cmd.Env = os.Environ()
	cmd.Stdout = stdoutWriter
	cmd.Stderr = stdoutWriter

	type drain struct {
		output []byte
		err    error
	}
	drained := make(chan drain, 1)
	go func() {
		output, err := io.ReadAll(stdout)
		drained <- drain{output: output, err: err}
	}()

	start := time.Now()
	if err := cmd.Start(); err != nil {
		t.Fatalf("start bounded-run: %v", err)
	}
	// From here on the supervised tree holds the only write ends: EOF on the
	// pipe proves every process that inherited it is gone.
	_ = stdoutWriter.Close()
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	})

	grandchild := waitForPIDFile(t, pidFile)

	waitErr := waitWithin(t, cmd, 90*time.Second)
	elapsed := time.Since(start)
	if waitErr == nil {
		t.Fatalf("bounded-run exited 0; a bound kill must be a failure")
	}
	var exitErr *exec.ExitError
	if !errors.As(waitErr, &exitErr) {
		t.Fatalf("wait bounded-run: %v", waitErr)
	}
	// Bound 5s plus 2s quit grace: generous ceiling, but far short of the
	// 300s fixture lifetime a missed kill would run to.
	if elapsed > 45*time.Second {
		t.Fatalf("bounded-run took %s to kill a SIGTERM-ignoring tree with a 5s bound and 2s grace", elapsed)
	}

	select {
	case got := <-drained:
		if got.err != nil {
			t.Fatalf("read bounded-run output: %v", got.err)
		}
		out := string(got.output)
		for _, want := range []string{
			"exceeded its 5s budget",
			"still running:",
			"rerun: rerun-hint-marker",
		} {
			if !strings.Contains(out, want) {
				t.Errorf("bounded-run output lacks %q:\n%s", want, out)
			}
		}
		// The still-running section names group members by pid when ps can
		// select by process group (Linux; other platforms print an
		// unavailable note instead of failing the watchdog).
		if runtime.GOOS == "linux" && !strings.Contains(out, fmt.Sprintf("pid %d", grandchild)) {
			t.Errorf("bounded-run output does not name the surviving grandchild pid %d:\n%s", grandchild, out)
		}
	case <-time.After(20 * time.Second):
		t.Fatal("a survivor of the bound kill still holds the caller's stdout 20s later")
	}

	if !processGone(grandchild, 20*time.Second) {
		t.Fatalf("grandchild pid %d survived the bound kill: the wrapper must leave no survivor", grandchild)
	}
}

// TestBoundedRunPassesThroughProductStatus pins that the wrapper is
// transparent when no bound fires: the product's exit status — including a
// SIGPIPE death — reaches the caller unchanged. push-suite.sh's run_suite
// maps 141 to a fail-closed push error, which only works if the wrapper
// neither swallows nor rewrites it.
func TestBoundedRunPassesThroughProductStatus(t *testing.T) {
	boundedRun := filepath.Join(repoRoot(t), "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}

	for _, tc := range []struct {
		name  string
		shell string
		want  int
	}{
		{name: "zero", shell: "exit 0", want: 0},
		{name: "nonzero", shell: "exit 3", want: 3},
		{name: "sigpipe", shell: "kill -PIPE $$", want: 141},
	} {
		t.Run(tc.name, func(t *testing.T) {
			cmd := exec.Command(boundedRun, "60s", "passthrough", "--", "sh", "-c", tc.shell)
			out, err := cmd.CombinedOutput()
			if tc.want == 0 {
				if err != nil {
					t.Fatalf("bounded-run over `exit 0` failed: %v\n%s", err, out)
				}
				return
			}
			var exitErr *exec.ExitError
			if !errors.As(err, &exitErr) {
				t.Fatalf("bounded-run over %q returned %v, want exit %d\n%s", tc.shell, err, tc.want, out)
			}
			if exitErr.ExitCode() != tc.want {
				t.Fatalf("bounded-run over %q exited %d, want %d\n%s", tc.shell, exitErr.ExitCode(), tc.want, out)
			}
		})
	}
}

// TestUnitCoreJobRunsUnderBoundedRun pins the ci-gke0 fix 2 wiring: the
// unit-core sweep is a bare `go test -timeout` like each cmd/gc shard's
// product run, so it runs under the same external supervision — the wrapper
// budget derives from the fan-out's one shared go timeout plus the same
// watchdog grace the shards use, never a second number.
func TestUnitCoreJobRunsUnderBoundedRun(t *testing.T) {
	script := localParallelScript(t)
	// Unresolved body: resolveShellVars would substitute the deadline the
	// script computes, hiding the wiring this pins.
	body := shellFunctionBody(t, script, "add_unit_core_job")
	for _, want := range []string{
		`add_job "unit-core"`,
		"scripts/bounded-run",
		"${unit_core_deadline}s",
		"--rerun-hint",
	} {
		if !strings.Contains(body, want) {
			t.Fatalf("unit-core job does not run under the bounded wrapper (lacks %q):\n%s", want, body)
		}
	}

	for _, want := range []string{
		"unit_core_deadline=$(",
		`gc_harness_duration_seconds "$go_test_timeout"`,
		"GO_TEST_WATCHDOG_GRACE:-10m",
	} {
		if !strings.Contains(script, want) {
			t.Fatalf("test-local-parallel does not derive the unit-core bound from the shared go timeout (lacks %q)", want)
		}
	}
}

// TestPrePushSuiteRunsUnderBoundedRun pins the hook-level half of ci-gke0
// fix 2: push-suite.sh's run_suite — the one choke point both engines run
// through — enforces a hard wall-clock bound over the whole push-time suite,
// sized to the same ceiling CI's lane gives `bazel test //...` and
// overridable without removing the bound.
func TestPrePushSuiteRunsUnderBoundedRun(t *testing.T) {
	body, err := os.ReadFile(filepath.Join(repoRoot(t), ".githooks", "lib", "push-suite.sh"))
	if err != nil {
		t.Fatalf("read push-suite.sh: %v", err)
	}
	runSuite := shellFunctionBody(t, string(body), "run_suite")
	for _, want := range []string{
		"scripts/bounded-run",
		"GC_PREPUSH_SUITE_TIMEOUT",
		"--rerun-hint",
	} {
		if !strings.Contains(runSuite, want) {
			t.Fatalf("run_suite does not enforce a wall-clock bound over the push-time suite (lacks %q):\n%s", want, runSuite)
		}
	}
}
