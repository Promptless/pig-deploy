package supervisor

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	coordinationv1 "k8s.io/api/coordination/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"
	"k8s.io/client-go/tools/leaderelection"
	"k8s.io/client-go/tools/leaderelection/resourcelock"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Invoked by tests/integration/test_kubernetes.py after installing the real chart
// and stopping its process. Never consult or mutate the user's default context.
func TestKubernetes(t *testing.T) {
	if os.Getenv("PIG_KUBERNETES_TEST") != "1" {
		t.Skip("requires disposable kind-pig-ci cluster and chart fixtures")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 120*time.Second)
	t.Cleanup(cancel)
	kubectl := func(args ...string) []byte {
		t.Helper()
		cmd := exec.CommandContext(ctx, "kubectl", append([]string{"--context", "kind-pig-ci"}, args...)...)
		out, err := cmd.Output()
		if err != nil {
			t.Fatalf("kubectl %s failed: %v", args[0], err)
		}
		return out
	}
	raw, err := clientcmd.Load(kubectl("config", "view", "--minify", "--raw", "--flatten", "-o", "json"))
	if err != nil {
		t.Fatal("load disposable cluster configuration")
	}
	configured := raw.Contexts["kind-pig-ci"]
	if configured == nil {
		t.Fatal("missing kind-pig-ci context")
	}
	cluster := raw.Clusters[configured.Cluster]
	token := strings.TrimSpace(string(kubectl("create", "token", "pig-supervisor", "-n", "pig-ci-system", "--duration=10m")))
	config := &rest.Config{Host: cluster.Server, BearerToken: token, TLSClientConfig: rest.TLSClientConfig{CAData: cluster.CertificateAuthorityData}, Timeout: 15 * time.Second}
	gate := &WriteGate{}
	kube, err := NewKube(config, gate)
	if err != nil {
		t.Fatal(err)
	}
	coordination, err := coordinationclient.NewForConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	lease := &PatchLeaseLock{Client: coordination.Leases("pig-ci"), Namespace: "pig-ci", Holder: "first", Gate: gate}
	record := resourcelock.LeaderElectionRecord{HolderIdentity: "first", LeaseDurationSeconds: 300, AcquireTime: metav1.Now(), RenewTime: metav1.Now()}
	if err = lease.Create(ctx, record); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { kubectl("delete", "lease", "pig-supervisor", "-n", "pig-ci", "--ignore-not-found") })
	if err = lease.Update(ctx, record); err != nil {
		t.Fatal("patch-only lease renewal", err)
	}
	d, err := kube.Get(ctx, "PIGDeployment", "pig-ci", "integration")
	if err != nil || d == nil {
		t.Fatal("missing fixture", err)
	}

	t.Run("admission_status_and_rotation", func(t *testing.T) {
		// Admission is exercised as the administrator; the supervisor deliberately
		// has no permission to modify a customer's PIGDeployment spec.
		out, err := exec.CommandContext(ctx, "kubectl", "--context", "kind-pig-ci", "patch", "pigdeployment", "integration", "-n", "pig-ci", "--type=merge", "-p", `{"spec":{"serviceAccountName":"INVALID NAME"}}`).CombinedOutput()
		if err == nil || !strings.Contains(string(out), "Invalid") {
			t.Fatalf("invalid spec was not rejected by admission: %s", out)
		}
		if err = kube.Status(ctx, d, Obj{"phase": "preflight", "attempt": 1, "retryAt": "2026-01-01T00:00:00Z"}); err != nil {
			t.Fatal(err)
		}
		if err = kube.Status(ctx, d, Obj{"phase": "quiesce"}); statusCode(err) != 409 {
			t.Fatal("stale status should conflict", err)
		}
		current, err := kube.Get(ctx, "PIGDeployment", "pig-ci", "integration")
		if err != nil {
			t.Fatal(err)
		}
		if err = kube.Status(ctx, current, Obj{"phase": "quiesce"}); err != nil {
			t.Fatal(err)
		}
		current, err = kube.Get(ctx, "PIGDeployment", "pig-ci", "integration")
		if err != nil {
			t.Fatal(err)
		}
		if len(child(current, "status")) != 1 || str(child(current, "status"), "phase") != "quiesce" {
			t.Fatal("removed status fields persisted")
		}
		spec, err := ParseSpec(child(d, "spec"))
		if err != nil {
			t.Fatal(err)
		}
		controller := &Controller{Kube: kube, Namespace: "pig-ci"}
		before, err := controller.ConfigurationHash(ctx, spec)
		if err != nil {
			t.Fatal(err)
		}
		ref := spec.Hosted.InstallTokenSecretRef
		patch, _ := json.Marshal(Obj{"stringData": Obj{ref.Key: "rotated-placeholder"}})
		kubectl("patch", "secret", ref.Name, "-n", "pig-ci", "--type=merge", "-p", string(patch))
		after, err := controller.ConfigurationHash(ctx, spec)
		if err != nil || before == after {
			t.Fatal("secret rotation failed to change hash", err)
		}
	})
	t.Run("apply_ownership", func(t *testing.T) {
		service := Obj{"kind": "Service", "apiVersion": "v1", "metadata": Obj{"name": "apply-test", "namespace": "pig-ci", "ownerReferences": []any{Obj{"apiVersion": d["apiVersion"], "kind": "PIGDeployment", "name": "integration", "uid": child(d, "metadata")["uid"]}}}, "spec": Obj{"selector": Obj{"app": "first"}, "ports": []any{Obj{"port": 80, "targetPort": 8080}}}}
		first, err := kube.Apply(ctx, service)
		if err != nil {
			t.Fatal(err)
		}
		child(service, "spec", "selector")["app"] = "second"
		second, err := kube.Apply(ctx, service)
		if err != nil || child(first, "spec")["clusterIP"] != child(second, "spec")["clusterIP"] {
			t.Fatal("SSA did not preserve server fields", err)
		}
		contender := copyObj(service)
		child(contender, "spec", "selector")["app"] = "contender"
		body, _ := json.Marshal(contender)
		err = kube.Client.Patch(ctx, kubeObject("Service", "pig-ci", "apply-test"), client.RawPatch(types.ApplyPatchType, body), client.FieldOwner("another-manager"))
		if !apierrors.IsConflict(err) {
			t.Fatal("field ownership was stolen", err)
		}
		object(items(child(contender, "metadata")["ownerReferences"])[0])["uid"] = "00000000-0000-0000-0000-000000000000"
		if _, err = kube.Apply(ctx, contender); statusCode(err) != 409 {
			t.Fatal("adopted another installation's resource", err)
		}
	})
	t.Run("bounded_rbac", func(t *testing.T) {
		for _, test := range []struct{ kind, namespace, name string }{{"Secret", "pig-ci", "credentials"}, {"ServiceAccount", "pig-ci", "identity"}, {"CustomResourceDefinition", "", "other.example.com"}} {
			if _, err := kube.Patch(ctx, test.kind, test.namespace, test.name, Obj{}); statusCode(err) != 403 {
				t.Errorf("%s write: %v", test.kind, err)
			}
		}
		if _, err := kube.Get(ctx, "Secret", "default", "outside"); statusCode(err) != 403 {
			t.Fatal("read outside watch namespace", err)
		}
		req, _ := http.NewRequestWithContext(ctx, "POST", config.Host+"/apis/rbac.authorization.k8s.io/v1/namespaces/pig-ci/roles", strings.NewReader(`{}`))
		req.Header.Set("Content-Type", "application/json")
		response, err := kube.HTTP.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		response.Body.Close()
		if response.StatusCode != 403 {
			t.Fatal("RBAC creation allowed", response.StatusCode)
		}
		if _, err = kube.Request(ctx, "PATCH", crdPath, Obj{"metadata": Obj{"annotations": Obj{"integration": "allowed"}}}); err != nil {
			t.Fatal("permitted CRD patch failed", err)
		}
		// The legacy Role deliberately lacks update and watch.
		if _, err = coordination.Leases("pig-ci").Update(ctx, &coordinationv1.Lease{ObjectMeta: metav1.ObjectMeta{Name: "pig-supervisor"}}, metav1.UpdateOptions{}); !apierrors.IsForbidden(err) {
			t.Fatal("test no longer exercises patch-only RBAC", err)
		}
	})
	t.Run("leader_handoff", func(t *testing.T) {
		contenderGate := &WriteGate{}
		contender := &PatchLeaseLock{Client: coordination.Leases("pig-ci"), Namespace: "pig-ci", Holder: "second", Gate: contenderGate}
		electionCtx, stop := context.WithCancel(ctx)
		defer stop()
		started := make(chan struct{})
		finished := make(chan struct{})
		elector, err := leaderelection.NewLeaderElector(leaderelection.LeaderElectionConfig{Lock: contender, LeaseDuration: 300 * time.Second, RenewDeadline: 240 * time.Second, RetryPeriod: time.Second, Callbacks: leaderelection.LeaderCallbacks{OnStartedLeading: func(ctx context.Context) { close(started); <-ctx.Done() }, OnStoppedLeading: func() {}}})
		if err != nil {
			t.Fatal(err)
		}
		go func() { defer close(finished); elector.Run(electionCtx) }()
		select {
		case <-started:
			t.Fatal("contender ignored observed expiry")
		case <-time.After(2 * time.Second):
		}
		if contenderGate.remaining() > 0 {
			t.Fatal("contender gained write authority")
		}
		// The old process is stopped. The administrator shortens only this disposable
		// Lease so the test need not spend five minutes waiting for takeover.
		gate.Revoke()
		kubectl("patch", "lease", "pig-supervisor", "-n", "pig-ci", "--type=merge", "-p", `{"spec":{"leaseDurationSeconds":1}}`)
		select {
		case <-started:
		case <-time.After(15 * time.Second):
			t.Fatal("contender did not acquire expired lease")
		}
		if contenderGate.remaining() <= 0 {
			t.Fatal("elected contender cannot write")
		}
		if _, _, err = lease.Get(ctx); err != nil {
			t.Fatal(err)
		}
		if gate.remaining() > 0 {
			t.Fatal("old leader can still write")
		}
		stale, _ := NewKube(config, gate)
		if _, err = stale.Patch(ctx, "Service", "pig-ci", "apply-test", Obj{}); statusCode(err) != 409 {
			t.Fatal(fmt.Sprintf("old leader write: %v", err))
		}
		stop()
		<-finished
		contenderGate.Revoke()
	})
}
