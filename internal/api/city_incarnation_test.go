package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Red-on-old: a request carrying a stale X-GC-City-Incarnation must be
// refused with 412 instead of executing against the replacement city.
// On city-next (no precondition) these requests return 200.
const (
	testIncarnationCurrent = "11111111-1111-4111-8111-111111111111"
	testIncarnationStale   = "22222222-2222-4222-8222-222222222222"
)

func TestCityIncarnationMismatchIsRefused(t *testing.T) {
	fs := newFakeState(t)
	fs.incarnationID = testIncarnationCurrent
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("GET", cityURL(fs, ""), nil)
	req.Header.Set("X-GC-City-Incarnation", testIncarnationStale)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusPreconditionFailed {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, http.StatusPreconditionFailed, w.Body.String())
	}
	var body struct {
		Type   string `json:"type"`
		Code   string `json:"code"`
		Errors []struct {
			Location string `json:"location"`
			Value    string `json:"value"`
		} `json:"errors"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if body.Code != "city-incarnation-mismatch" {
		t.Errorf("code = %q, want %q", body.Code, "city-incarnation-mismatch")
	}
	if body.Type != "urn:gascity:error:city-incarnation-mismatch" {
		t.Errorf("type = %q, want the city-incarnation-mismatch URN", body.Type)
	}
	if len(body.Errors) == 0 || body.Errors[0].Value != testIncarnationCurrent {
		t.Errorf("errors[0].value = %v, want the current incarnation %q", body.Errors, testIncarnationCurrent)
	}
}

func TestCityIncarnationMatchProceeds(t *testing.T) {
	fs := newFakeState(t)
	fs.incarnationID = testIncarnationCurrent
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("GET", cityURL(fs, ""), nil)
	req.Header.Set("X-GC-City-Incarnation", testIncarnationCurrent)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, http.StatusOK, w.Body.String())
	}
}

func TestCityIncarnationAbsentHeaderProceeds(t *testing.T) {
	fs := newFakeState(t)
	fs.incarnationID = testIncarnationCurrent
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("GET", cityURL(fs, ""), nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, http.StatusOK, w.Body.String())
	}
}

func TestCityIncarnationUnknownServerIDProceeds(t *testing.T) {
	fs := newFakeState(t) // incarnationID empty: state carries no identity
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("GET", cityURL(fs, ""), nil)
	req.Header.Set("X-GC-City-Incarnation", testIncarnationStale)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, http.StatusOK, w.Body.String())
	}
}

func TestCitiesListExposesIncarnationID(t *testing.T) {
	fs := newFakeState(t)
	fs.incarnationID = testIncarnationCurrent
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("GET", "/v0/cities", nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp struct {
		Items []map[string]any `json:"items"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(resp.Items) != 1 {
		t.Fatalf("items = %d, want 1", len(resp.Items))
	}
	if resp.Items[0]["incarnation_id"] != testIncarnationCurrent {
		t.Errorf("incarnation_id = %v, want %q", resp.Items[0]["incarnation_id"], testIncarnationCurrent)
	}
}

func TestCityUnregisterIgnoresIncarnationHeader(t *testing.T) {
	// Unregister is supervisor-scope (outside bindCity): the header is
	// undeclared there and must not be enforced. The test mux wires no
	// initializer, so the request reaches the 501, not a 412.
	fs := newFakeState(t)
	fs.incarnationID = testIncarnationCurrent
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("POST", cityURL(fs, "/unregister"), nil)
	req.Header.Set("X-GC-Request", "test")
	req.Header.Set("X-GC-City-Incarnation", testIncarnationStale)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want %d (a 412 would mean the precondition leaked into unregister); body = %s",
			w.Code, http.StatusNotImplemented, w.Body.String())
	}
}

func TestCityGetExposesIncarnationID(t *testing.T) {
	fs := newFakeState(t)
	fs.incarnationID = testIncarnationCurrent
	h := newTestCityHandler(t, fs)

	req := httptest.NewRequest("GET", cityURL(fs, ""), nil)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d; body = %s", w.Code, http.StatusOK, w.Body.String())
	}
	var resp map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if resp["incarnation_id"] != testIncarnationCurrent {
		t.Errorf("incarnation_id = %v, want %q", resp["incarnation_id"], testIncarnationCurrent)
	}
}
