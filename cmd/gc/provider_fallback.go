// provider_fallback.go — per-agent provider fallback for fresh pool creates.
//
// An agent may list fallback_providers. When a fresh pool session is
// planned and the agent's primary provider is confirmed red in the
// provider-health registry (provider_health_gate.go), the create picks the
// first fallback that is not red and records it on the new session bead as
// gc.provider_override. Every later resolution of that bead
// (resolveTemplateForSessionBeadInfo) launches it with the override preset,
// so start, resume, respawn, the respawn gate and the progress-stall
// exemption all see the session's own provider. Running and reused
// sessions are never migrated; the next fresh create returns to the primary
// as soon as the primary is no longer red.
package main

import (
	"encoding/json"
	"io"
	"strings"
	"time"

	"github.com/gastownhall/gascity/internal/beadmeta"
	"github.com/gastownhall/gascity/internal/config"
	"github.com/gastownhall/gascity/internal/events"
	"github.com/gastownhall/gascity/internal/session"
)

// providerFallbackChoice is a fresh create's switch from its agent's
// primary provider to a fallback.
type providerFallbackChoice struct {
	From   string // the red primary
	To     string // the fallback chosen
	Reason string // the health file's reason for the primary, if any
}

// agentPrimaryProviderName is the provider name an agent's sessions launch
// with when no override applies: agent.provider, then the pack-inherited
// default, then workspace.provider. It is the name the health gate keys on.
func agentPrimaryProviderName(agent *config.Agent, ws *config.Workspace) string {
	if agent == nil {
		return ""
	}
	name := strings.TrimSpace(agent.Provider)
	if name == "" {
		name = strings.TrimSpace(agent.InheritedProvider)
	}
	if name == "" && ws != nil {
		name = strings.TrimSpace(ws.Provider)
	}
	return name
}

// selectPoolCreateProvider decides which provider a fresh pool create of
// agent launches with under snap. It returns (nil, nil) when the primary is
// not red (the create proceeds exactly as without fallbacks), a choice when
// a fallback is taken, and errPoolSessionCreateProviderRed when the primary
// and every fallback are red. Unknown and stale entries are not red.
func selectPoolCreateProvider(snap *providerHealthSnapshot, agent *config.Agent, ws *config.Workspace) (*providerFallbackChoice, error) {
	primary := agentPrimaryProviderName(agent, ws)
	if !snap.red(primary) {
		return nil, nil
	}
	if agent != nil && agent.StartCommand == "" {
		for _, candidate := range agent.FallbackProviders {
			candidate = strings.TrimSpace(candidate)
			if candidate == "" || candidate == primary {
				continue
			}
			if !snap.red(candidate) {
				return &providerFallbackChoice{From: primary, To: candidate, Reason: snap.reason(primary)}, nil
			}
		}
	}
	return nil, errPoolSessionCreateProviderRed
}

// sessionProviderAgent returns the agent a session bead resolves its
// template from: agent itself, or, when the bead carries a provider
// override naming a provider in the catalog, a copy launching with that
// provider and the agent's fallback_option_defaults for it as its
// option_defaults. The overlay is recomputed from config on every
// resolution (start, resume, respawn), as option_defaults are for the
// primary; it is never stored on the bead. The copy drops the agent's provider-shaped launch overrides
// (args, option_defaults, prompt mode/flag, ready hints, process names,
// permission-warning flag, resume command): they were written for the
// primary and would mis-launch another provider. Env, lifecycle and every
// non-launch field are kept. An override naming a provider no longer in the
// catalog is ignored, so the session falls back to the primary rather than
// failing to resolve.
func sessionProviderAgent(agent *config.Agent, info session.Info, providers map[string]config.ProviderSpec) *config.Agent {
	override := strings.TrimSpace(info.ProviderOverride)
	if agent == nil || override == "" || agent.StartCommand != "" {
		return agent
	}
	if _, ok := providers[override]; !ok {
		return agent
	}
	if override == strings.TrimSpace(agent.Provider) {
		return agent
	}
	c := agent.Clone()
	c.Provider = override
	c.InheritedProvider = ""
	c.Args = nil
	// The agent's own option_defaults were written for the primary; the
	// fallback takes its per-agent overlay instead, which config.ResolveProvider
	// lays over the fallback preset's defaults through the same
	// mergeAgentOverrides path option_defaults use for the primary.
	c.OptionDefaults = nil
	if opts := agent.FallbackOptionDefaults[override]; len(opts) > 0 {
		c.OptionDefaults = make(map[string]string, len(opts))
		for k, v := range opts {
			c.OptionDefaults[k] = v
		}
	}
	c.PromptMode = ""
	c.PromptFlag = ""
	c.ReadyDelayMs = nil
	c.ReadyPromptPrefix = ""
	c.ProcessNames = nil
	c.EmitsPermissionWarning = nil
	c.ResumeCommand = ""
	return &c
}

// stampProviderFallback records choice on a planned create's metadata.
func stampProviderFallback(metadata map[string]string, choice *providerFallbackChoice) map[string]string {
	if choice == nil {
		return metadata
	}
	if metadata == nil {
		metadata = make(map[string]string, 1)
	}
	metadata[beadmeta.ProviderOverrideMetadataKey] = choice.To
	return metadata
}

// providerFallbackRecorder opens the recorder a provider.fallback event is
// written to. Tests replace it.
var providerFallbackRecorder = func(cityPath string) events.Recorder {
	return openCityRecorderAt(cityPath, io.Discard)
}

// emitProviderFallbackEvent records that the session created as info took
// choice. A nil choice or an empty session ID records nothing.
func emitProviderFallbackEvent(cityPath, agentName string, info session.Info, choice *providerFallbackChoice) {
	if choice == nil || strings.TrimSpace(info.ID) == "" || strings.TrimSpace(cityPath) == "" {
		return
	}
	rec := providerFallbackRecorder(cityPath)
	if rec == nil {
		return
	}
	if closer, ok := rec.(io.Closer); ok {
		defer closer.Close() //nolint:errcheck
	}
	fields := map[string]any{
		"agent":   agentName,
		"session": info.ID,
		"from":    choice.From,
		"to":      choice.To,
	}
	msg := "provider fallback: " + agentName + " session " + info.ID + " created with " + choice.To + " (" + choice.From + " is red)"
	if choice.Reason != "" {
		fields["reason"] = choice.Reason
		msg += ": " + choice.Reason
	}
	payload, _ := json.Marshal(fields)
	rec.Record(events.Event{
		Type:    events.ProviderFallback,
		Ts:      time.Now().UTC(),
		Actor:   "gc",
		Subject: info.ID,
		Message: msg,
		Payload: payload,
	})
}
