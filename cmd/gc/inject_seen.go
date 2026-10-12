package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/gastownhall/gascity/internal/citylayout"
	"github.com/gastownhall/gascity/internal/fsys"
	"github.com/gastownhall/gascity/internal/mail"
)

// injectAlwaysEnv disables once-per-change injection: every hook run emits its
// full block, exactly as it did before the seen-state existed. Debugging aid.
const injectAlwaysEnv = "GC_INJECT_ALWAYS"

// Seen-state kinds. Each kind gets its own file per session because the step
// and mail hooks fire on the same UserPromptSubmit event and run concurrently;
// one shared file would let their read-modify-write cycles clobber each other.
const (
	injectSeenKindStep = "step"
	injectSeenKindMail = "mail"
)

// injectSeen remembers the digest of what a hook last delivered to one session,
// so a hook that re-renders an unchanged block can say so in a line instead of
// repeating the block on every prompt.
//
// A nil *injectSeen is valid and means "dedup is off": Unchanged reports false,
// so the caller emits in full, and Record and Forget do nothing. Every failure
// to read or write the state degrades to that same full emit; a missing or
// unreadable file means "never shown".
type injectSeen struct {
	path string
}

type injectSeenRecord struct {
	SHA256 string `json:"sha256"`
}

// openInjectSeen returns the seen-state for one hook kind of the calling
// session, stored under <city>/.gc/runtime/inject-seen. It returns nil, so the
// caller always emits in full, when GC_INJECT_ALWAYS is set, when the city is
// unknown, or when the session has no identity (GC_SESSION_ID, else GC_ALIAS) to
// key the state on. A compaction or restarted session carries a new session id
// and so starts from "never shown".
func openInjectSeen(cityPath, kind string) *injectSeen {
	if injectAlwaysEnabled() {
		return nil
	}
	cityPath = strings.TrimSpace(cityPath)
	if cityPath == "" {
		return nil
	}
	key := strings.TrimSpace(os.Getenv("GC_SESSION_ID"))
	if key == "" {
		key = strings.TrimSpace(os.Getenv("GC_ALIAS"))
	}
	if key == "" {
		return nil
	}
	return &injectSeen{path: filepath.Join(citylayout.RuntimeDataDir(cityPath), "inject-seen", injectSeenFileName(key, kind))}
}

func injectAlwaysEnabled() bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(injectAlwaysEnv))) {
	case "", "0", "false", "no", "off":
		return false
	}
	return true
}

// injectSeenFileName maps a session identity to a single path element. Aliases
// are qualified names that contain "/", so the readable part is sanitized and a
// digest of the raw key keeps two identities that sanitize alike apart.
func injectSeenFileName(key, kind string) string {
	var readable strings.Builder
	for _, r := range key {
		switch {
		case r >= 'a' && r <= 'z', r >= 'A' && r <= 'Z', r >= '0' && r <= '9', r == '-', r == '_':
			readable.WriteRune(r)
		default:
			readable.WriteByte('_')
		}
	}
	name := readable.String()
	if len(name) > 64 {
		name = name[:64]
	}
	sum := sha256.Sum256([]byte(key))
	return name + "." + hex.EncodeToString(sum[:4]) + "." + kind + ".json"
}

// Unchanged reports whether digest equals the one last recorded.
func (s *injectSeen) Unchanged(digest string) bool {
	if s == nil || digest == "" {
		return false
	}
	data, err := os.ReadFile(s.path)
	if err != nil {
		return false
	}
	var rec injectSeenRecord
	if err := json.Unmarshal(data, &rec); err != nil {
		return false
	}
	return rec.SHA256 == digest
}

// Record stores digest as the last delivered one. Call it only after the block
// reached the hook transport: recording a block that was never written would
// suppress its only delivery. Best-effort; a failed write means the next run
// emits in full.
func (s *injectSeen) Record(digest string) {
	if s == nil || digest == "" {
		return
	}
	data, err := json.Marshal(injectSeenRecord{SHA256: digest})
	if err != nil {
		return
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return
	}
	_ = fsys.WriteFileAtomic(fsys.OSFS{}, s.path, data, 0o644)
}

// Forget drops the recorded digest. Called when there is nothing to show, so
// the same content returning later is shown in full again rather than assumed
// to still be in the agent's context.
func (s *injectSeen) Forget() {
	if s == nil {
		return
	}
	_ = os.Remove(s.path)
}

func injectDigest(parts ...string) string {
	h := sha256.New()
	for _, p := range parts {
		h.Write([]byte(p)) //nolint:errcheck // hash.Hash.Write never fails
		h.Write([]byte{0}) //nolint:errcheck // hash.Hash.Write never fails
	}
	return hex.EncodeToString(h.Sum(nil))
}

// mailUnreadDigest identifies the set of unread message ids addressed to
// recipient, independent of the order they arrive in.
func mailUnreadDigest(recipient string, messages []mail.Message) string {
	ids := make([]string, 0, len(messages))
	for _, m := range messages {
		ids = append(ids, m.ID)
	}
	sort.Strings(ids)
	return injectDigest(append([]string{recipient}, ids...)...)
}

// injectSeenCityPath resolves the city for hooks that are not handed one.
// GC_CITY wins, as it does for the step injection; otherwise the usual local
// resolution applies. An unresolvable city returns "", which turns dedup off.
func injectSeenCityPath() string {
	if city := strings.TrimSpace(os.Getenv("GC_CITY")); city != "" {
		return city
	}
	city, err := resolveCity()
	if err != nil {
		return ""
	}
	return city
}
