package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"k8s.io/client-go/rest"
)

func testKube(t *testing.T, handler http.HandlerFunc) (*Kube, *WriteGate) {
	t.Helper()
	server := httptest.NewServer(handler)
	t.Cleanup(server.Close)
	gate := &WriteGate{}
	gate.renew(time.Now())
	kube, err := NewKube(&rest.Config{Host: server.URL, Timeout: time.Second}, gate)
	if err != nil {
		t.Fatal(err)
	}
	return kube, gate
}
func respondJSON(w http.ResponseWriter, document any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(document)
}
func requestBody(t *testing.T, r *http.Request) Obj {
	t.Helper()
	var value Obj
	if err := json.NewDecoder(r.Body).Decode(&value); err != nil {
		t.Error(err)
	}
	return value
}
func TestStatusPatchDeletionAndConflict(t *testing.T) {
	calls := 0
	kube, _ := testKube(t, func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.Method != "PATCH" || !strings.HasSuffix(r.URL.Path, "/pigdeployments/worker/status") || r.Header.Get("Content-Type") != "application/merge-patch+json" {
			t.Errorf("unexpected request %s %s", r.Method, r.URL)
		}
		body := requestBody(t, r)
		if str(child(body, "metadata"), "resourceVersion") != "17" || child(body, "status")["retryAt"] != nil || str(child(body, "status"), "phase") != "quiesce" {
			t.Error(body)
		}
		if calls == 2 {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(409)
			respondJSON(w, Obj{"kind": "Status", "apiVersion": "v1", "status": "Failure", "reason": "Conflict", "code": 409, "message": "private-token"})
			return
		}
		respondJSON(w, Obj{"apiVersion": "governance.promptless.ai/v1alpha1", "kind": "PIGDeployment", "status": body["status"]})
	})
	d := Obj{"metadata": Obj{"namespace": "pig", "name": "worker", "resourceVersion": "17"}, "status": Obj{"phase": "preflight", "retryAt": "later"}}
	if err := kube.Status(context.Background(), d, Obj{"phase": "quiesce"}); err != nil {
		t.Fatal(err)
	}
	err := kube.Status(context.Background(), d, Obj{"phase": "quiesce"})
	if statusCode(err) != 409 || strings.Contains(err.Error(), "private-token") {
		t.Fatal(err)
	}
	if str(child(d, "status"), "retryAt") != "later" {
		t.Fatal("mutated input")
	}
}
func TestWriteGateExpiryAndCancellation(t *testing.T) {
	sent := 0
	kube, gate := testKube(t, func(w http.ResponseWriter, r *http.Request) {
		sent++
		respondJSON(w, Obj{"apiVersion": "v1", "kind": "Service"})
	})
	now := time.Now()
	gate.now = func() time.Time { return now }
	gate.renew(now)
	patch := func() error {
		_, err := kube.Patch(context.Background(), "Service", "leases-prod", "leases", Obj{"spec": Obj{}})
		return err
	}
	if err := patch(); err != nil {
		t.Fatal(err)
	}
	now = now.Add(writeWindow)
	if err := patch(); statusCode(err) != 409 {
		t.Fatalf("expired gate: %v", err)
	}
	if sent != 1 {
		t.Fatal("expired write reached API")
	}
	gate.renew(now)
	gate.Revoke()
	if err := patch(); statusCode(err) != 409 {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	gate.renew(now)
	if _, err := kube.Patch(ctx, "Service", "pig", "test", Obj{}); err == nil {
		t.Fatal("canceled request succeeded")
	}
}
func TestApplyDoesNotForceOrAdopt(t *testing.T) {
	owner := "ours"
	patches := 0
	kube, _ := testKube(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method == "GET" {
			respondJSON(w, Obj{"kind": "Service", "apiVersion": "v1", "metadata": Obj{"ownerReferences": []any{Obj{"uid": owner}}}})
			return
		}
		patches++
		if r.Header.Get("Content-Type") != "application/apply-patch+yaml" || r.URL.Query().Get("fieldManager") != "pig-supervisor" || r.URL.Query().Get("force") == "true" {
			t.Errorf("unsafe apply: %s %v", r.URL, r.Header)
		}
		respondJSON(w, requestBody(t, r))
	})
	d := Obj{"apiVersion": "v1", "kind": "Service", "metadata": Obj{"name": "test", "namespace": "pig", "ownerReferences": []any{Obj{"uid": "ours"}}}, "spec": Obj{}}
	if _, err := kube.Apply(context.Background(), d); err != nil {
		t.Fatal(err)
	}
	owner = "theirs"
	if _, err := kube.Apply(context.Background(), d); statusCode(err) != 409 {
		t.Fatal(err)
	}
	if patches != 1 {
		t.Fatal("adopted someone else's object")
	}
}
func TestProjectedTokenConfigurationAndSafeFailures(t *testing.T) {
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("projected-token"), 0600); err != nil {
		t.Fatal(err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer projected-token" {
			t.Error("projected token missing")
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(403)
		fmt.Fprint(w, `{"kind":"Status","apiVersion":"v1","reason":"Forbidden","code":403,"message":"secret-response"}`)
	}))
	defer server.Close()
	kube, err := NewKube(&rest.Config{Host: server.URL, BearerTokenFile: token}, &WriteGate{})
	if err != nil {
		t.Fatal(err)
	}
	_, err = kube.Get(context.Background(), "Secret", "pig", "credential")
	if statusCode(err) != 403 || strings.Contains(err.Error(), "secret-response") || strings.Contains(err.Error(), "projected-token") {
		t.Fatal(err)
	}
}
