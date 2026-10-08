package supervisor

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
)

func TestPatchLeaseLockPreservesPythonPermissionsAndWriteBoundary(t *testing.T) {
	now := time.Now()
	gate := &WriteGate{now: func() time.Time { return now }}
	record := resourcelock.LeaderElectionRecord{HolderIdentity: "first", LeaseDurationSeconds: 300, RenewTime: metav1.Now()}
	stored := &coordinationv1.Lease{TypeMeta: metav1.TypeMeta{APIVersion: "coordination.k8s.io/v1", Kind: "Lease"}, ObjectMeta: metav1.ObjectMeta{Name: "pig-supervisor", Namespace: "pig", ResourceVersion: "17"}, Spec: resourcelock.LeaderElectionRecordToLeaseSpec(&record)}
	conflict := false
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case "GET":
			respondJSON(w, stored)
		case "PATCH":
			body := requestBody(t, r)
			if str(child(body, "metadata"), "resourceVersion") != "17" || r.Header.Get("Content-Type") != "application/merge-patch+json" {
				t.Error("lease patch lost optimistic lock")
			}
			if conflict {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(409)
				respondJSON(w, Obj{"kind": "Status", "apiVersion": "v1", "code": 409, "reason": "Conflict"})
				return
			}
			now = now.Add(10 * time.Second)
			_ = decode(body["spec"], &stored.Spec)
			respondJSON(w, stored)
		case "POST":
			_ = json.NewDecoder(r.Body).Decode(stored)
			respondJSON(w, stored)
		default:
			t.Errorf("existing RBAC does not grant %s", r.Method)
			w.WriteHeader(403)
		}
	}))
	defer server.Close()
	client, err := coordinationclient.NewForConfig(&rest.Config{Host: server.URL, ContentConfig: rest.ContentConfig{ContentType: "application/json"}})
	if err != nil {
		t.Fatal(err)
	}
	lock := &PatchLeaseLock{Client: client.Leases("pig"), Namespace: "pig", Holder: "first", Gate: gate}
	if _, _, err = lock.Get(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err = lock.Update(context.Background(), record); err != nil {
		t.Fatal(err)
	}
	if gate.remaining() != 260*time.Second {
		t.Fatal("network latency must consume write window", gate.remaining())
	}
	conflict = true
	if err = lock.Update(context.Background(), record); !apierrors.IsConflict(err) {
		t.Fatal(err)
	}
	if gate.remaining() > 0 {
		t.Fatal("conflicting renewal retained write authority")
	}
	conflict = false
	if err = lock.Create(context.Background(), record); err != nil {
		t.Fatal(err)
	}
	other := "other"
	stored.Spec.HolderIdentity = &other
	if _, _, err = lock.Get(context.Background()); err != nil {
		t.Fatal(err)
	}
	if gate.remaining() > 0 {
		t.Fatal("observed competitor retained write authority")
	}
}

func TestManagerReconcilesWithBootstrapPermissionsAndStops(t *testing.T) {
	d := exampleDeployment(t)
	child(d, "spec")["release"] = Obj{"paused": true}
	persisted := make(chan Obj, 10)
	var lease Obj
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if r.URL.Query().Get("watch") == "true" {
			t.Error("manager unexpectedly needs watch RBAC")
			w.WriteHeader(403)
			return
		}
		switch {
		case strings.Contains(path, "/leases"):
			if r.Method == "GET" && lease == nil {
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(404)
				respondJSON(w, Obj{"kind": "Status", "apiVersion": "v1", "code": 404, "reason": "NotFound"})
				return
			}
			if r.Method != "GET" {
				if r.Method != "POST" && r.Method != "PATCH" {
					t.Errorf("lease requires unsupported verb %s", r.Method)
				}
				lease = requestBody(t, r)
				lease["apiVersion"] = "coordination.k8s.io/v1"
				lease["kind"] = "Lease"
				child(lease, "metadata")["resourceVersion"] = "1"
			}
			respondJSON(w, lease)
		case strings.HasSuffix(path, "/pigdeployments"):
			respondJSON(w, Obj{"apiVersion": "governance.promptless.ai/v1alpha1", "kind": "PIGDeploymentList", "items": []any{d}})
		case strings.HasSuffix(path, "/status"):
			body := requestBody(t, r)
			persisted <- child(body, "status")
			respondJSON(w, d)
		case strings.Contains(path, "/helmreleases"), strings.Contains(path, "/ingresses/"):
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(404)
			respondJSON(w, Obj{"kind": "Status", "apiVersion": "v1", "code": 404, "reason": "NotFound"})
		case strings.Contains(path, "/serviceaccounts/"):
			respondJSON(w, Obj{"kind": "ServiceAccount", "apiVersion": "v1", "metadata": Obj{"resourceVersion": "1"}})
		case strings.Contains(path, "/secrets/"):
			respondJSON(w, Obj{"kind": "Secret", "apiVersion": "v1", "metadata": Obj{"resourceVersion": "1"}, "data": Obj{"install-token": "opaque", "postgres-dsn": "opaque", "model-api-key": "opaque"}})
		case strings.Contains(path, "/configmaps/"):
			respondJSON(w, Obj{"kind": "ConfigMap", "apiVersion": "v1", "metadata": Obj{"resourceVersion": "1"}, "data": Obj{"ca.pem": "public-ca"}})
		default:
			t.Errorf("unexpected API %s %s", r.Method, path)
			w.WriteHeader(403)
		}
	}))
	defer server.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() {
		done <- Run(ctx, &rest.Config{Host: server.URL, ContentConfig: rest.ContentConfig{ContentType: "application/json"}}, Options{Namespace: "pig", SystemNamespace: "pig-system", Holder: "test", CatalogURL: "http://127.0.0.1:9"})
	}()
	select {
	case status := <-persisted:
		found := false
		for _, c := range items(status["conditions"]) {
			if str(object(c), "type") == "Blocked" && str(object(c), "reason") == "Paused" {
				found = true
			}
		}
		if !found {
			t.Errorf("paused deployment not reconciled: %v", status)
		}
	case err := <-done:
		t.Fatalf("manager exited: %v", err)
	case <-time.After(10 * time.Second):
		t.Fatal("manager did not reconcile")
	}
	cancel()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("manager did not stop")
	}
}
