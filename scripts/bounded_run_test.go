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
	"syscall"
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

	if !processGone(grandchild) {
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

// TestBoundedRunPerBinaryLetsChurningBinariesOutliveTheBudget is the gs-jfy
// fix-round regression: `go test -timeout` bounds each test binary, not the
// whole multi-package invocation, so a run whose total exceeds timeout+grace
// while no single binary does is healthy and must NOT be killed. The product
// churns six 2s binaries past an 8s per-binary budget (~12s total); the
// wall-clock bound a33b43357 put on unit-core kills it at 8s instead.
func TestBoundedRunPerBinaryLetsChurningBinariesOutliveTheBudget(t *testing.T) {
	root := repoRoot(t)
	boundedRun := filepath.Join(root, "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}
	dir := t.TempDir()
	logPath := filepath.Join(dir, "runner.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		t.Fatalf("create runner log: %v", err)
	}
	t.Cleanup(func() { _ = logFile.Close() })

	// Each child argv[0] ends in .test, the shape the per-binary watchdog
	// matches; a subshell per child keeps argv[0] spoofing (exec -a) from
	// replacing the loop itself.
	product := fmt.Sprintf(`for i in 1 2 3 4 5 6; do ( exec -a %s sleep 2 ); done`,
		shellQuote(filepath.Join(dir, "pkg.test")))
	cmd := harnessBash(fmt.Sprintf(`exec %s 8s churn --per-binary --quit-grace 1 -- bash -c %s`,
		shellQuote(boundedRun), shellQuote(product)))
	cmd.Dir = root
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	start := time.Now()
	if err := cmd.Start(); err != nil {
		t.Fatalf("start bounded-run: %v", err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	})
	if err := waitWithin(t, cmd, 120*time.Second); err != nil {
		t.Fatalf("bounded-run killed a healthy churning run: %v\n%s", err, readLogFile(t, logPath))
	}
	elapsed := time.Since(start)
	// A run that finished before the budget would prove nothing: the whole
	// point is the total outlived the per-binary budget.
	if elapsed <= 8*time.Second {
		t.Fatalf("churning run finished in %s, before its 8s budget; the fixture must outlive the budget to prove per-binary scoping", elapsed)
	}
	if out := readLogFile(t, logPath); strings.Contains(out, "exceeded its") {
		t.Fatalf("per-binary watchdog fired on a run whose binaries stayed under budget:\n%s", out)
	}
}

// TestBoundedRunPerBinaryKillsAnAgedOutTestBinary pins the other half of the
// gs-jfy contract: a test binary that itself outlives timeout+grace IS the
// wedge the bound exists for, so the watchdog must kill the group, name the
// culprit binary and the per-binary budget, and print the still-running list
// with the rerun hint — the same diagnostics the wall-clock kill gives.
func TestBoundedRunPerBinaryKillsAnAgedOutTestBinary(t *testing.T) {
	root := repoRoot(t)
	boundedRun := filepath.Join(root, "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}
	dir := t.TempDir()
	logPath := filepath.Join(dir, "runner.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		t.Fatalf("create runner log: %v", err)
	}
	t.Cleanup(func() { _ = logFile.Close() })
	pidFile := filepath.Join(dir, "wedged.pid")

	// Ignored dispositions survive exec, so the spoofed .test binary below
	// drops the watchdog's SIGQUIT the way a wedged runtime does; only the
	// SIGKILL escalation ends it.
	product := fmt.Sprintf(`trap '' TERM INT QUIT HUP
echo $$ > %s
exec -a %s sleep %d
`, shellQuote(pidFile), shellQuote(filepath.Join(dir, "wedged.test")), fixtureLifetimeSeconds)
	cmd := harnessBash(fmt.Sprintf(`exec %s 5s wedge --per-binary --quit-grace 2 --rerun-hint rerun-hint-marker -- bash -c %s`,
		shellQuote(boundedRun), shellQuote(product)))
	cmd.Dir = root
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	start := time.Now()
	if err := cmd.Start(); err != nil {
		t.Fatalf("start bounded-run: %v", err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	})
	wedged := waitForPIDFile(t, pidFile)

	waitErr := waitWithin(t, cmd, 90*time.Second)
	elapsed := time.Since(start)
	if waitErr == nil {
		t.Fatalf("bounded-run exited 0; an aged-out test binary must be killed")
	}
	var exitErr *exec.ExitError
	if !errors.As(waitErr, &exitErr) {
		t.Fatalf("wait bounded-run: %v", waitErr)
	}
	// Bound 5s plus 2s quit grace: generous ceiling, but far short of the
	// 300s fixture lifetime a missed kill would run to.
	if elapsed > 45*time.Second {
		t.Fatalf("bounded-run took %s to kill an aged-out test binary with a 5s budget and 2s grace", elapsed)
	}

	out := readLogFile(t, logPath)
	for _, want := range []string{
		"exceeded its 5s per-binary budget",
		"wedged.test",
		"still running:",
		"rerun: rerun-hint-marker",
	} {
		if !strings.Contains(out, want) {
			t.Errorf("bounded-run output lacks %q:\n%s", want, out)
		}
	}
	if runtime.GOOS == "linux" && !strings.Contains(out, fmt.Sprintf("pid %d", wedged)) {
		t.Errorf("bounded-run output does not name the aged-out binary pid %d:\n%s", wedged, out)
	}
	if !processGone(wedged) {
		t.Fatalf("wedged binary pid %d survived the per-binary kill: the wrapper must leave no survivor", wedged)
	}
}

// TestUnitCoreJobRunsUnderBoundedRun pins the ci-gke0 fix 2 wiring: the
// unit-core sweep is a bare `go test -timeout` like each cmd/gc shard's
// product run, so it runs under the same external supervision — the wrapper
// budget derives from the fan-out's one shared go timeout plus the same
// watchdog grace the shards use, never a second number. The sweep covers many
// packages in one invocation, so unlike the single-package shards it runs
// under --per-binary: a wall-clock bound fires on a healthy run whose total
// legitimately exceeds timeout+grace (gs-jfy fix round).
func TestUnitCoreJobRunsUnderBoundedRun(t *testing.T) {
	script := localParallelScript(t)
	// Unresolved body: resolveShellVars would substitute the deadline the
	// script computes, hiding the wiring this pins.
	body := shellFunctionBody(t, script, "add_unit_core_job")
	for _, want := range []string{
		`add_job "unit-core"`,
		"scripts/bounded-run",
		"${unit_core_deadline}s",
		"--per-binary",
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

// TestUnitCoreJobHintQuotingSurvivesEvalPath proves the gs-o4i4 fix round:
// add_unit_core_job builds its bounded-run invocation as a string passed to
// add_job, and the runner later evaluates it via `bash -lc`. A substring
// assertion (TestUnitCoreJobRunsUnderBoundedRun) passes even when the hint
// quoting is broken; this test generates the real jobspec from the real
// function, evaluates it the way the runner does against a stub
// bounded-run, and checks the hint arrives as one argument. It passes on
// e8335c0d (the quoting was already correct); it guards the eval path, not
// a behavior change.
func TestUnitCoreJobHintQuotingSurvivesEvalPath(t *testing.T) {
	script := localParallelScript(t)
	addJob := shellFunctionBody(t, script, "add_job")
	addUnitCore := shellFunctionBody(t, script, "add_unit_core_job")
	gen := fmt.Sprintf(`set -euo pipefail
add_job() {
%s
}
add_unit_core_job() {
%s
}
go_test_timeout="20m"
unit_core_deadline="1800"
jobspecs=()
add_unit_core_job
printf '%%s' "${jobspecs[0]}"
`, addJob, addUnitCore)
	genCmd := harnessBash(gen)
	jobspecOut, err := genCmd.CombinedOutput()
	if err != nil {
		t.Fatalf("generate unit-core jobspec: %v\n%s", err, jobspecOut)
	}
	jobspec := string(jobspecOut)
	const prefix = "unit-core::"
	if !strings.HasPrefix(jobspec, prefix) {
		t.Fatalf("unit-core jobspec has no %q prefix:\n%s", prefix, jobspec)
	}
	command := strings.TrimPrefix(jobspec, prefix)

	dir := t.TempDir()
	scriptsDir := filepath.Join(dir, "scripts")
	if err := os.Mkdir(scriptsDir, 0o755); err != nil {
		t.Fatalf("create scripts dir: %v", err)
	}
	argvLog := filepath.Join(dir, "argv.log")
	writeExecutable(t, filepath.Join(scriptsDir, "bounded-run"), fmt.Sprintf(`#!/bin/sh
printf '<%%s>\n' "$@" > %q
exit 0
`, argvLog))
	binDir := filepath.Join(dir, "bin")
	if err := os.Mkdir(binDir, 0o755); err != nil {
		t.Fatalf("create bin dir: %v", err)
	}
	writeExecutable(t, filepath.Join(binDir, "go"), `#!/bin/sh
if [ "${1:-}" = "list" ]; then
  printf '%s\n' 'github.com/gastownhall/gascity/scripts'
  exit 0
fi
exit 0
`)

	evalCmd := harnessBash("bash -lc " + shellQuote(command))
	evalCmd.Dir = dir
	evalCmd.Env = append(os.Environ(), "PATH="+binDir+string(os.PathListSeparator)+os.Getenv("PATH"))
	if out, err := evalCmd.CombinedOutput(); err != nil {
		t.Fatalf("eval unit-core jobspec: %v\n%s", err, out)
	}
	argv, err := os.ReadFile(argvLog)
	if err != nil {
		t.Fatalf("stub bounded-run never ran (argv log missing): %v", err)
	}
	log := string(argv)
	hintLine := "<--rerun-hint>\n<make test-fast-parallel>"
	if !strings.Contains(log, hintLine) {
		t.Fatalf("rerun hint did not survive the eval path as one argument; stub saw:\n%s", log)
	}
	if !strings.Contains(log, "<--per-binary>") {
		t.Fatalf("stub bounded-run argv lacks --per-binary:\n%s", log)
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

// TestBoundedRunWallClockMessageIsLabelNeutral pins the gs-o4i4 fix round:
// the wall-clock watchdog also bounds the whole push-time suite (a bazel run
// or a non-go wedge), so its kill message must not claim the run "defeated
// go's own -timeout". The per-binary watchdog keeps that wording, since it
// only fires on *.test members. Fails on e8335c0d, whose wall-clock message
// carries the go-specific clause.
func TestBoundedRunWallClockMessageIsLabelNeutral(t *testing.T) {
	root := repoRoot(t)
	boundedRun := filepath.Join(root, "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}
	cmd := harnessBash(fmt.Sprintf(`exec %s 2s suite --quit-grace 1 -- sleep %d`,
		shellQuote(boundedRun), fixtureLifetimeSeconds))
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatalf("bounded-run exited 0; a bound kill must be a failure")
	}
	output := string(out)
	if !strings.Contains(output, "exceeded its 2s budget") {
		t.Errorf("bounded-run output lacks the budget line:\n%s", output)
	}
	if strings.Contains(output, "defeated go's own -timeout") {
		t.Errorf("wall-clock watchdog message claims a go timeout defeat; it must be label-neutral:\n%s", output)
	}
}

// TestBoundedRunWallClockIgnoresInheritedPerBinaryMode pins the gs-xjon fix
// round: the unit-core sweep runs under --per-binary, which exports
// GC_HARNESS_WATCHDOG_MODE into every nested process, including the scripts
// package's own test binary. A wall-clock run must take its mode from its own
// flags, never from the environment: with per-binary inherited, the per-binary
// watchdog never fires on a non-.test tree and the bound silently disarms
// (the GTX reds: a 300s sleep exiting 0, a SIGTERM-ignoring tree surviving
// past 90s). The sibling wall-clock tests only catch this when the suite
// itself runs nested; this one sets the inheritance explicitly, so it fails
// standalone before the fix and passes after.
func TestBoundedRunWallClockIgnoresInheritedPerBinaryMode(t *testing.T) {
	root := repoRoot(t)
	boundedRun := filepath.Join(root, "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}
	// A 30s product keeps a regression failing fast; the 2s bound plus 1s
	// grace must still end it in seconds.
	cmd := harnessBash(fmt.Sprintf(`exec %s 2s suite --quit-grace 1 -- sleep 30`,
		shellQuote(boundedRun)))
	cmd.Dir = root
	cmd.Env = append(os.Environ(), "GC_HARNESS_WATCHDOG_MODE=per-binary")
	start := time.Now()
	out, err := cmd.CombinedOutput()
	elapsed := time.Since(start)
	if err == nil {
		t.Fatalf("bounded-run exited 0 with per-binary mode inherited; the wall-clock bound must fire")
	}
	if elapsed > 45*time.Second {
		t.Fatalf("bounded-run took %s to fire a 2s wall-clock bound with per-binary mode inherited", elapsed)
	}
	if output := string(out); !strings.Contains(output, "exceeded its 2s budget") {
		t.Errorf("bounded-run output lacks the budget line:\n%s", output)
	}
}

// TestBoundedRunDoubleTerminateOnSignalIsSafe pins the gs-o4i4 fix round:
// bounded_run_on_signal tears the supervised tree down, then re-raises the
// signal; the EXIT trap then runs gc_harness_terminate_supervised a second
// time. That double invocation is idempotent by design (the second call finds
// empty or drained groups and returns), but was untested. This test sends
// SIGTERM to a live bounded-run and checks the runner dies by signal, the
// product is reaped, and the run exits promptly rather than hanging or
// erroring in the second terminate. It passes on e8335c0d (the path was
// already idempotent); it guards the signal path, not a behavior change.
func TestBoundedRunDoubleTerminateOnSignalIsSafe(t *testing.T) {
	root := repoRoot(t)
	boundedRun := filepath.Join(root, "scripts", "bounded-run")
	if _, err := os.Stat(boundedRun); err != nil {
		t.Fatalf("bounded wrapper is missing: %v", err)
	}
	dir := t.TempDir()
	logPath := filepath.Join(dir, "runner.log")
	logFile, err := os.Create(logPath)
	if err != nil {
		t.Fatalf("create runner log: %v", err)
	}
	t.Cleanup(func() { _ = logFile.Close() })
	pidFile := filepath.Join(dir, "product.pid")

	product := fmt.Sprintf(`echo $$ > %s; exec sleep %d`,
		shellQuote(pidFile), fixtureLifetimeSeconds)
	cmd := harnessBash(fmt.Sprintf(`exec %s 60s sigtest --quit-grace 1 -- bash -c %s`,
		shellQuote(boundedRun), shellQuote(product)))
	cmd.Dir = root
	cmd.Stdout = logFile
	cmd.Stderr = logFile

	start := time.Now()
	if err := cmd.Start(); err != nil {
		t.Fatalf("start bounded-run: %v", err)
	}
	t.Cleanup(func() {
		if cmd.Process != nil {
			_ = cmd.Process.Kill()
		}
	})
	productPid := waitForPIDFile(t, pidFile)

	if err := cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatalf("signal bounded-run: %v", err)
	}
	waitErr := waitWithin(t, cmd, 60*time.Second)
	elapsed := time.Since(start)
	if waitErr == nil {
		t.Fatalf("bounded-run exited 0 after SIGTERM; signal death must be a failure")
	}
	var exitErr *exec.ExitError
	if !errors.As(waitErr, &exitErr) {
		t.Fatalf("wait bounded-run: %v", waitErr)
	}
	if elapsed > 45*time.Second {
		t.Fatalf("bounded-run took %s to die after SIGTERM; the double terminate must not hang", elapsed)
	}
	if !processGone(productPid) {
		t.Fatalf("product pid %d survived SIGTERM to bounded-run: the signal path must reap the tree", productPid)
	}
	if out := readLogFile(t, logPath); strings.Contains(out, "unbound variable") {
		t.Fatalf("double terminate hit a nounset error:\n%s", out)
	}
}

// readLogFile returns the runner log at path. Callers read only after the
// runner has exited, so the file is complete.
func readLogFile(t *testing.T, path string) string {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read runner log: %v", err)
	}
	return string(data)
}
