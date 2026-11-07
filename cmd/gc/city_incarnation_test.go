package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/google/uuid"

	"github.com/gastownhall/gascity/internal/api"
	"github.com/gastownhall/gascity/internal/cityidentity"
	"github.com/gastownhall/gascity/internal/cityinit"
	"github.com/gastownhall/gascity/internal/events"
	"github.com/gastownhall/gascity/internal/fsys"
	"github.com/gastownhall/gascity/internal/runtime"
)

const testIncarnationID = "44444444-4444-4444-8444-444444444444"

func writeTestCityID(t *testing.T, cityPath, id string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Join(cityPath, ".gc"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(cityPath, ".gc", "city-id"), []byte(id+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestDoInitMintsCityID(t *testing.T) {
	cityPath := filepath.Join(t.TempDir(), "id-city")
	var stdout, stderr bytes.Buffer
	wiz := wizardConfig{configName: "empty"}
	if code := doInit(fsys.OSFS{}, cityPath, wiz, "id-city", &stdout, &stderr, false); code != 0 {
		t.Fatalf("doInit = %d, want 0; stderr=%s", code, stderr.String())
	}
	raw, err := os.ReadFile(filepath.Join(cityPath, ".gc", "city-id"))
	if err != nil {
		t.Fatalf("read city-id: %v", err)
	}
	id := strings.TrimSpace(string(raw))
	if _, err := uuid.Parse(id); err != nil {
		t.Errorf("city-id %q is not a UUID: %v", id, err)
	}

	// A refused re-init never rewrites the id.
	var stdout2, stderr2 bytes.Buffer
	_ = doInit(fsys.OSFS{}, cityPath, wiz, "id-city", &stdout2, &stderr2, false)
	raw2, err := os.ReadFile(filepath.Join(cityPath, ".gc", "city-id"))
	if err != nil {
		t.Fatalf("read city-id after re-init: %v", err)
	}
	if string(raw2) != string(raw) {
		t.Errorf("city-id after re-init = %q, want minted %q", raw2, raw)
	}
}

func newIncarnationTestControllerState(t *testing.T, cityPath string) *controllerState {
	t.Helper()
	writeCityRuntimeConfig(t, filepath.Join(cityPath, "city.toml"), "fake")
	cfg, _ := loadCityRuntimeControllerConfig(t, cityPath)
	return newControllerState(context.Background(), cfg, runtime.NewFake(), events.NewFake(), "test-city", cityPath)
}

func TestControllerStateLatchesIncarnationID(t *testing.T) {
	cityPath := t.TempDir()
	writeTestCityID(t, cityPath, testIncarnationID)
	cs := newIncarnationTestControllerState(t, cityPath)
	if got := cs.CityIncarnationID(); got != testIncarnationID {
		t.Errorf("CityIncarnationID = %q, want %q", got, testIncarnationID)
	}
	// The latch backs the API's optional capability.
	if got := api.StateIncarnationID(cs); got != testIncarnationID {
		t.Errorf("StateIncarnationID = %q, want %q", got, testIncarnationID)
	}
}

func TestControllerStateWithoutCityIDHasEmptyIncarnation(t *testing.T) {
	cityPath := t.TempDir()
	cs := newIncarnationTestControllerState(t, cityPath)
	if got := cs.CityIncarnationID(); got != "" {
		t.Errorf("CityIncarnationID = %q, want empty", got)
	}
}

func TestCheckReloadCityIncarnation(t *testing.T) {
	match := t.TempDir()
	writeTestCityID(t, match, testIncarnationID)
	replaced := t.TempDir()
	writeTestCityID(t, replaced, "55555555-5555-4555-8555-555555555555")
	missing := t.TempDir()
	malformed := t.TempDir()
	writeTestCityID(t, malformed, "not-a-uuid")

	tests := []struct {
		name      string
		cityPath  string
		bootedID  string
		wantErr   bool
		wantInErr string
	}{
		{"match proceeds", match, testIncarnationID, false, ""},
		{"replacement refused", replaced, testIncarnationID, true, "restart"},
		{"missing file refused", missing, testIncarnationID, true, "unreadable"},
		{"malformed file refused", malformed, testIncarnationID, true, "unreadable"},
		{"empty boot latch skips", missing, "", false, ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := checkReloadCityIncarnation(tc.cityPath, tc.bootedID)
			if tc.wantErr && err == nil {
				t.Fatal("check = nil, want a refusal")
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("check = %v, want nil", err)
			}
			if tc.wantErr && !strings.Contains(err.Error(), tc.wantInErr) {
				t.Errorf("check = %q, want it to mention %q", err, tc.wantInErr)
			}
		})
	}
}

func TestCityRegistryListCitiesCarriesIncarnation(t *testing.T) {
	reg := newCityRegistry()
	reg.Add("/path/a", &managedCity{
		name:    "city-a",
		started: true,
		cr: &CityRuntime{
			cityName: "city-a",
			cs:       &controllerState{cityName: "city-a", cityPath: "/path/a", incarnationID: testIncarnationID},
		},
	})
	reg.Add("/path/b", &managedCity{name: "city-b", status: "starting"})

	var withID, withoutID *api.CityInfo
	for _, ci := range reg.ListCities() {
		ci := ci
		switch ci.Name {
		case "city-a":
			withID = &ci
		case "city-b":
			withoutID = &ci
		}
	}
	if withID == nil || withoutID == nil {
		t.Fatalf("ListCities = %v, want city-a and city-b", reg.ListCities())
	}
	if withID.IncarnationID != testIncarnationID {
		t.Errorf("city-a incarnation_id = %q, want %q", withID.IncarnationID, testIncarnationID)
	}
	if withoutID.IncarnationID != "" {
		t.Errorf("city-b incarnation_id = %q, want empty (no state yet)", withoutID.IncarnationID)
	}
}

func TestSingleCityStateResolverListCitiesCarriesIncarnation(t *testing.T) {
	r := &singleCityStateResolver{state: &controllerState{cityName: "solo", cityPath: "/solo", incarnationID: testIncarnationID}}
	infos := r.ListCities()
	if len(infos) != 1 {
		t.Fatalf("len = %d, want 1", len(infos))
	}
	if infos[0].IncarnationID != testIncarnationID {
		t.Errorf("incarnation_id = %q, want %q", infos[0].IncarnationID, testIncarnationID)
	}
}

func TestCityCreatedEventCarriesIncarnation(t *testing.T) {
	cityPath := t.TempDir()
	writeTestCityID(t, cityPath, testIncarnationID)
	e := cityInitLifecycleEvents{}
	if err := e.CityCreated(cityPath, "id-city"); err != nil {
		t.Fatalf("CityCreated: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(cityPath, ".gc", "events.jsonl"))
	if err != nil {
		t.Fatalf("read events log: %v", err)
	}
	var found bool
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var ev events.Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("unmarshal event: %v", err)
		}
		if ev.Type != events.CityCreated {
			continue
		}
		found = true
		var payload api.CityLifecyclePayload
		if err := json.Unmarshal(ev.Payload, &payload); err != nil {
			t.Fatalf("unmarshal payload: %v", err)
		}
		if payload.IncarnationID != testIncarnationID {
			t.Errorf("payload incarnation_id = %q, want %q", payload.IncarnationID, testIncarnationID)
		}
	}
	if !found {
		t.Error("no city.created event in the log")
	}
}

type stubInitRegistry struct{}

func (stubInitRegistry) Register(_ context.Context, _, _ string) error { return nil }
func (stubInitRegistry) Find(_ context.Context, _ string) (cityinit.RegisteredCity, error) {
	return cityinit.RegisteredCity{}, errors.New("not found")
}

func (stubInitRegistry) Unregister(_ context.Context, _ cityinit.RegisteredCity) error {
	return nil
}

type stubInitReloader struct{}

func (stubInitReloader) Reload() error                { return nil }
func (stubInitReloader) ReloadAfterUnregister() error { return nil }

// TestServiceScaffoldEmitsCreatedWithMintedID composes the real service
// orchestration with the real initializer and the real lifecycle
// recorder (registry/reloader stubbed): the city.created event must
// carry the id the scaffold minted, pinning mint-before-emit order.
func TestServiceScaffoldEmitsCreatedWithMintedID(t *testing.T) {
	cityPath := filepath.Join(t.TempDir(), "svc-city")
	svc, err := cityinit.NewService(cityinit.ServiceDeps{
		FS:              fsys.OSScaffoldFS{},
		Initializer:     initializerAdapter{},
		Registry:        stubInitRegistry{},
		Reloader:        stubInitReloader{},
		LifecycleEvents: cityInitLifecycleEvents{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	_, err = svc.Scaffold(context.Background(), cityinit.InitRequest{
		Dir:                   cityPath,
		ConfigName:            "empty",
		Provider:              "codex",
		SkipProviderReadiness: true,
	})
	if err != nil {
		t.Fatalf("Scaffold: %v", err)
	}
	minted, err := cityidentity.Load(cityPath)
	if err != nil {
		t.Fatalf("Load minted id: %v", err)
	}
	raw, err := os.ReadFile(filepath.Join(cityPath, ".gc", "events.jsonl"))
	if err != nil {
		t.Fatalf("read events log: %v", err)
	}
	var found bool
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var ev events.Event
		if err := json.Unmarshal([]byte(line), &ev); err != nil {
			t.Fatalf("unmarshal event: %v", err)
		}
		if ev.Type != events.CityCreated {
			continue
		}
		found = true
		var payload api.CityLifecyclePayload
		if err := json.Unmarshal(ev.Payload, &payload); err != nil {
			t.Fatalf("unmarshal payload: %v", err)
		}
		if payload.IncarnationID != minted {
			t.Errorf("payload incarnation_id = %q, want the minted %q", payload.IncarnationID, minted)
		}
	}
	if !found {
		t.Error("no city.created event in the log")
	}
}

func TestEnsureCityScaffoldMintsIDForExistingCity(t *testing.T) {
	// An older city (scaffold without an id) gains one on next ensure.
	cityPath := t.TempDir()
	for _, dir := range []string{".gc", ".gc/cache", ".gc/runtime"} {
		if err := os.MkdirAll(filepath.Join(cityPath, dir), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.WriteFile(filepath.Join(cityPath, ".gc", "events.jsonl"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := ensureCityScaffold(cityPath); err != nil {
		t.Fatalf("ensureCityScaffold: %v", err)
	}
	id, err := cityidentity.Load(cityPath)
	if err != nil {
		t.Fatalf("Load after ensure: %v", err)
	}
	if _, err := uuid.Parse(id); err != nil {
		t.Errorf("minted %q is not a UUID: %v", id, err)
	}
}
