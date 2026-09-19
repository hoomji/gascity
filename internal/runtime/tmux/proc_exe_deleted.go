package tmux

// processExeDeleted reports whether the executable backing pid has been
// unlinked from disk. On Linux the kernel renders the /proc/<pid>/exe target
// with a trailing " (deleted)" once the image is replaced in place.
//
// A deleted image is a process left over from a pre-upgrade binary. Its process
// NAME still matches the configured process name, so name-only liveness keeps
// treating it as alive and the reconciler never recycles the stale session —
// the control-dispatcher can keep serving a stream for hours on a binary that
// no longer exists. Liveness matching consults this so such a process reads as
// dead and the existing zombie/recycle paths replace it with a fresh start.
//
// It is a variable so tests can pin both arms without a real unlinked binary.
var processExeDeleted = processExeDeletedImpl
