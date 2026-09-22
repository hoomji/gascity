package main

import (
	"fmt"
	"io"
	"strings"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/beads"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/runtime"
)

// processRuntimeBinaryStale is the seam over runtimeBinaryStale. It lets the
// process-table sweep exercise stale-binary replacement without a real
// reinstalled gc binary on disk.
var processRuntimeBinaryStale = runtimeBinaryStale

// isControlDispatcherSessionTemplate reports whether a session bead's template
// names a control-dispatcher agent. It accepts the bare name
// ("control-dispatcher"), a qualified binding ("core.control-dispatcher"), and
// a rig-qualified form ("gateway-llm/core.control-dispatcher").
func isControlDispatcherSessionTemplate(template string) bool {
	template = strings.TrimSpace(template)
	if template == "" {
		return false
	}
	if template == config.ControlDispatcherAgentName {
		return true
	}
	_, name := config.ParseQualifiedName(template)
	return name == config.ControlDispatcherAgentName ||
		strings.HasSuffix(name, "."+config.ControlDispatcherAgentName)
}

// isControlDispatcherSessionBead reads the template off a session bead. Session
// beads write "template"; the generic gc.template key is accepted as a
// fallback for older rows.
func isControlDispatcherSessionBead(b beads.Bead) bool {
	template := strings.TrimSpace(b.Metadata["template"])
	if template == "" {
		template = strings.TrimSpace(b.Metadata[beadmeta.TemplateMetadataKey])
	}
	return isControlDispatcherSessionTemplate(template)
}

// requestStaleControllerRuntimeRestart handles one live runtime whose session
// bead is still open. A control-dispatcher bound to an open bead is normally
// left alone, but if the process is running a gc binary that `gc install`
// replaced, it is exactly the stale-server defect: it keeps serving the stream
// from deleted code.
//
// The remedy has two parts:
//
//   - An untracked runtime is a duplicate the provider no longer owns, so it is
//     terminated directly.
//   - restart_requested is set on the session bead so the reconciler stops and
//     re-starts the tracked controller on the current binary. That stop/start
//     also releases the single-server lock, letting the replacement claim it.
func requestStaleControllerRuntimeRestart(
	scanner runtime.ProcessTableScanner,
	store beads.Store,
	live runtime.LiveRuntime,
	bead beads.Bead,
	stderr io.Writer,
) {
	if scanner == nil || store == nil || !isControlDispatcherSessionBead(bead) {
		return
	}
	if !processRuntimeBinaryStale(live.PID) {
		return
	}
	if !live.IsTracked {
		if err := scanner.TerminateRuntime(live); err != nil {
			fmt.Fprintf(stderr, "session reconciler: terminating stale untracked controller pid=%d session=%s: %v\n", live.PID, live.SessionID, err) //nolint:errcheck
		} else {
			fmt.Fprintf(stderr, "session reconciler: terminated stale untracked controller pid=%d session=%s (running a replaced gc binary)\n", live.PID, live.SessionID) //nolint:errcheck
		}
	}
	if err := store.SetMetadata(live.SessionID, "restart_requested", "true"); err != nil {
		fmt.Fprintf(stderr, "session reconciler: requesting restart for stale controller session %s pid=%d: %v\n", live.SessionID, live.PID, err) //nolint:errcheck
		return
	}
	fmt.Fprintf(stderr, "session reconciler: controller session %s pid=%d is running a replaced gc binary; requested restart\n", live.SessionID, live.PID) //nolint:errcheck
}
