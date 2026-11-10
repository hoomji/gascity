package session

import (
	"os"
	"strings"
)

// HostMetadataKey names the session row key carrying the runtime host: the
// machine the session's agent process runs on, as a hostname. Every create
// path stamps it via StampHost; consumers compare it against their own
// hostname to decide whether a session is local. Absent on rows created
// before the field existed, which readers treat as local (fail-open: v1
// cities are single-host).
const HostMetadataKey = "host"

// localSessionHostname is os.Hostname behind a seam so tests can pin the
// hostname-error path without depending on the machine they run on.
var localSessionHostname = os.Hostname

// LocalSessionHost reports the trimmed machine hostname, or "" when the host
// cannot be determined. Callers stamp "" by omitting the key.
func LocalSessionHost() string {
	host, err := localSessionHostname()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(host)
}

// StampHost records the creating host on meta when the caller did not name
// one. An explicit host (including "") is never overwritten: remote-creation
// proxies stamp their own value before calling, and a failed hostname lookup
// stays absent rather than guessing.
func StampHost(meta map[string]string) {
	if _, ok := meta[HostMetadataKey]; ok {
		return
	}
	if host := LocalSessionHost(); host != "" {
		meta[HostMetadataKey] = host
	}
}
