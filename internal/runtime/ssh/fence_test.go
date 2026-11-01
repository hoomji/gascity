package ssh

import (
	"context"
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/gastownhall/gascity/internal/runtime"
)

// fencedEnv names an incarnation the way the session layer's starts do.
func fencedEnv() map[string]string {
	return map[string]string{
		runtime.InstanceTokenEnv: "tok-1",
		runtime.RuntimeEpochEnv:  "3",
		"GC_RIG":                 "r",
	}
}

func isFencePublish(argv []string) bool {
	return len(argv) >= 2 && argv[0] == "tmux" && argv[1] == "set-option" && slices.Contains(argv, runtime.TmuxInputFenceOption)
}

// Kills a Start that leaves the agent pane unfenced: every fenced op would
// then miss on the session gc just started.
func TestStartPublishesTheIncarnationFenceOnTheAgentPane(t *testing.T) {
	created := false
	f := &fakeRunner{respond: answerStaging(func(argv []string) ([]byte, int, error) {
		switch {
		case isTmux("has-session")(argv):
			if created {
				return nil, 0, nil
			}
			return nil, 1, nil
		case isTmux("new-session")(argv), isTmux("start-server")(argv):
			created = true
			return []byte("%1\n"), 0, nil
		}
		return nil, 0, nil
	})}
	cfg := runtime.Config{Command: "agent", Env: fencedEnv()}
	if err := providerWith(f).Start(context.Background(), "s", cfg); err != nil {
		t.Fatalf("Start: %v", err)
	}
	got := firstCall(f, isFencePublish)
	fence, _ := runtime.InputFenceFromEnv(cfg.Env)
	want := append([]string{"tmux"}, runtime.TmuxPublishInputFenceArgs("=s:@1.%1", fence)...)
	if !slices.Equal(got, want) {
		t.Fatalf("fence publish =\n  %v\nwant\n  %v", got, want)
	}
	for _, c := range f.calls {
		if strings.Contains(strings.Join(c, " "), "tok-1") {
			t.Fatalf("the instance token reached argv: %v", c)
		}
	}
}

// Kills a Start that keeps a session it could not fence.
func TestStartStopsTheSessionWhenTheFenceCannotBePublished(t *testing.T) {
	created := false
	f := &fakeRunner{respond: answerStaging(func(argv []string) ([]byte, int, error) {
		switch {
		case isTmux("has-session")(argv):
			if created {
				return nil, 0, nil
			}
			return nil, 1, nil
		case isTmux("new-session")(argv), isTmux("start-server")(argv):
			created = true
			return []byte("%1\n"), 0, nil
		case isFencePublish(argv):
			return nil, 1, nil
		}
		return nil, 0, nil
	})}
	err := providerWith(f).Start(context.Background(), "s", runtime.Config{Command: "agent", Env: fencedEnv()})
	if err == nil || !strings.Contains(err.Error(), "input fence") {
		t.Fatalf("Start with a failing publish = %v, want an input fence error", err)
	}
	if firstCall(f, isTmux("kill-session")) == nil {
		t.Fatal("a session that could not be fenced was left running")
	}
}

// Kills a Relaunch whose respawned pane keeps the old incarnation's fence.
func TestRelaunchSetsTheFenceInTheRespawnCommandList(t *testing.T) {
	f := &fakeRunner{respond: answerStaging(nil)}
	cfg := runtime.Config{Command: "agent", Env: fencedEnv()}
	if err := providerWith(f).Relaunch(context.Background(), "s", cfg); err != nil {
		t.Fatalf("Relaunch: %v", err)
	}
	got := firstCall(f, isTmux("respawn-pane"))
	fence, _ := runtime.InputFenceFromEnv(cfg.Env)
	tail := append([]string{";"}, runtime.TmuxPublishInputFenceArgs("=s:@1.%1", fence)...)
	if len(got) < len(tail) || !slices.Equal(got[len(got)-len(tail):], tail) {
		t.Fatalf("respawn argv %v does not end with the fence publish %v", got, tail)
	}

	f = &fakeRunner{respond: answerStaging(nil)}
	if err := providerWith(f).Relaunch(context.Background(), "s", runtime.Config{Command: "agent"}); err != nil {
		t.Fatalf("Relaunch: %v", err)
	}
	got = firstCall(f, isTmux("respawn-pane"))
	clear := append([]string{";"}, runtime.TmuxClearInputFenceArgs("=s:@1.%1")...)
	if !slices.Equal(got[len(got)-len(clear):], clear) {
		t.Fatalf("respawn argv %v without an incarnation does not clear the fence", got)
	}
}

func TestSSHReportsAtomicInputFencing(t *testing.T) {
	if got := runtime.InputFencingFor(NewSeamBacked(Endpoint{Host: "box"}), "s"); got != runtime.InputFencingAtomic {
		t.Fatalf("ssh seam-backed provider fences %q, want atomic", got)
	}
}

// A session the box definitely lacks is an absent miss; a connection that
// does not answer is unavailable and never reads as absent or as sent.
func TestFencedNudgeAbsentAndUnreachable(t *testing.T) {
	fence, _ := runtime.InputFenceFromEnv(fencedEnv())
	absent := &fakeRunner{rawPanes: true, code: 1}
	err := providerWith(absent).NudgeFenced(context.Background(), "s", runtime.TextContent("hi"), runtime.FencedNudge{Fence: fence})
	var miss *runtime.FenceMismatchError
	if !errors.As(err, &miss) || miss.Found != runtime.FenceFoundAbsent {
		t.Fatalf("NudgeFenced to a missing session = %v, want an absent miss", err)
	}

	down := &fakeRunner{rawPanes: true, code: -1, err: errors.New("ssh box: connection failed (ssh exit 255)")}
	err = providerWith(down).NudgeFenced(context.Background(), "s", runtime.TextContent("hi"), runtime.FencedNudge{Fence: fence})
	if !errors.Is(err, runtime.ErrRuntimeUnavailable) || errors.As(err, &miss) {
		t.Fatalf("NudgeFenced over a dead connection = %v, want ErrRuntimeUnavailable and no miss", err)
	}
	for _, c := range down.calls {
		if isTmux("send-keys")(c) || isTmux("paste-buffer")(c) {
			t.Fatalf("input was sent over a connection that never answered the fence read: %v", c)
		}
	}
}
