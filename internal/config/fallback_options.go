package config

import (
	"errors"
	"fmt"
	"slices"
	"sort"
	"strings"
)

// ValidateFallbackOptionDefaults checks every agent's fallback_option_defaults:
// each provider key must be listed in the agent's fallback_providers, and
// each option must pass that provider's options_schema under the check
// provider option_defaults get at load (ValidateOptionDefaults over the
// provider resolved as validateResolvedProviderOptions resolves it). A
// provider missing from the catalog is reported by ValidateProviderReferences,
// so its options are not checked here.
func ValidateFallbackOptionDefaults(cfg *City) error {
	if cfg == nil {
		return nil
	}
	var errs []error
	for _, agent := range cfg.Agents {
		if len(agent.FallbackOptionDefaults) == 0 {
			continue
		}
		providers := make([]string, 0, len(agent.FallbackOptionDefaults))
		for name := range agent.FallbackOptionDefaults {
			providers = append(providers, name)
		}
		sort.Strings(providers)
		for _, name := range providers {
			if !slices.Contains(agent.FallbackProviders, name) {
				errs = append(errs, fmt.Errorf("agent %q: fallback_option_defaults key %q is not listed in fallback_providers", agent.QualifiedName(), name))
				continue
			}
			spec, ok := cfg.Providers[strings.TrimSpace(name)]
			if !ok {
				continue
			}
			resolved, err := resolveCustomProviderForValidation(name, spec, cfg.Providers)
			if err != nil {
				errs = append(errs, fmt.Errorf("agent %q: fallback_option_defaults.%s: resolving provider: %w", agent.QualifiedName(), name, err))
				continue
			}
			if err := ValidateOptionDefaults(resolved.OptionsSchema, agent.FallbackOptionDefaults[name]); err != nil {
				errs = append(errs, fmt.Errorf("agent %q: fallback_option_defaults.%s: %w", agent.QualifiedName(), name, err))
			}
		}
	}
	return errors.Join(errs...)
}
