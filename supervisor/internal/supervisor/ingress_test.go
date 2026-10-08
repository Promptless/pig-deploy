package supervisor

import (
	"context"
	"fmt"
	"net/http"
	"reflect"
	"testing"
)

func TestOptionalIngressContract(t *testing.T) {
	for _, endpoint := range []Obj{nil, {}, {"enabled": false}, {"hostname": "pig.example.com", "ingressClassName": "nginx"}} {
		d := exampleDeployment(t)
		s := child(d, "spec")
		delete(s, "endpoint")
		if endpoint != nil {
			s["endpoint"] = endpoint
		}
		spec, err := ParseSpec(s)
		if err != nil {
			t.Fatal(err)
		}
		resources := AnalyzerResources(d, spec, Release{}, "hash")
		if len(resources) != 2 || str(resources[1], "kind") != "Service" || str(child(resources[1], "spec"), "type") != "ClusterIP" {
			t.Fatal("disabled endpoint must leave only Deployment and private Service")
		}
	}
	for _, endpoint := range []Obj{{"enabled": true, "hostname": "pig.example.com"}, {"enabled": true, "ingressClassName": "nginx"}, {"enabled": true, "hostname": "pig.example.com", "ingressClassName": " "}} {
		s := child(exampleDeployment(t), "spec")
		s["endpoint"] = endpoint
		if _, err := ParseSpec(s); err == nil {
			t.Fatal("enabled endpoint accepted without host and controller")
		}
	}
	d := exampleDeployment(t)
	child(d, "spec")["endpoint"] = Obj{"enabled": true, "hostname": "pig.example.com", "ingressClassName": "nginx"}
	spec, err := ParseSpec(child(d, "spec"))
	if err != nil {
		t.Fatal(err)
	}
	resources := AnalyzerResources(d, spec, Release{}, "hash")
	if len(resources) != 3 || str(resources[2], "kind") != "Ingress" {
		t.Fatal("explicit endpoint did not generate ingress")
	}
	rule := object(items(child(resources[2], "spec")["rules"])[0])
	paths := []string{}
	for _, value := range items(child(rule, "http")["paths"]) {
		path := object(value)
		if str(path, "pathType") != "Exact" {
			t.Fatal("collector route must be exact")
		}
		paths = append(paths, str(path, "path"))
	}
	if !reflect.DeepEqual(paths, []string{"/healthz", "/v0/host-enrollment/policy", "/v0/host-enrollment/check-ins", "/v0/cloud-enrollment/leases", "/v0/traces/batches"}) {
		t.Fatal("collector route contract changed", paths)
	}
}

func TestOwnedIngressDeletion(t *testing.T) {
	for _, deleteStatus := range []int{200, 202, 404, 409} {
		t.Run(fmt.Sprint(deleteStatus), func(t *testing.T) {
			exists, deleting, deletes := true, false, 0
			kube, _ := testKube(t, func(w http.ResponseWriter, r *http.Request) {
				if r.URL.Path != "/apis/networking.k8s.io/v1/namespaces/pig/ingresses/worker-analyzer" {
					t.Error("unexpected path", r.URL.Path)
				}
				if r.Method == "GET" {
					if !exists {
						w.Header().Set("Content-Type", "application/json")
						w.WriteHeader(404)
						respondJSON(w, Obj{"apiVersion": "v1", "kind": "Status", "code": 404, "reason": "NotFound"})
						return
					}
					metadata := Obj{"uid": "ingress-uid", "resourceVersion": "7", "ownerReferences": []any{Obj{"uid": "deployment-uid", "controller": true}}}
					if deleting {
						metadata["deletionTimestamp"] = "2026-10-08T00:00:00Z"
					}
					respondJSON(w, Obj{"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": metadata})
					return
				}
				if r.Method != "DELETE" {
					t.Error("unexpected method", r.Method)
				}
				deletes++
				preconditions := child(requestBody(t, r), "preconditions")
				if str(preconditions, "uid") != "ingress-uid" || str(preconditions, "resourceVersion") != "7" {
					t.Error("missing deletion preconditions", preconditions)
				}
				exists, deleting = deleteStatus == 202 || deleteStatus == 409, deleteStatus == 202
				w.Header().Set("Content-Type", "application/json")
				w.WriteHeader(deleteStatus)
				respondJSON(w, Obj{"apiVersion": "v1", "kind": "Status", "code": deleteStatus})
			})
			deleted, err := kube.DeleteOwnedIngress(context.Background(), "pig", "worker-analyzer", "deployment-uid")
			if deleteStatus == 409 {
				if deleted || statusCode(err) != 409 {
					t.Fatal("ownership/replacement conflict ignored", deleted, err)
				}
				return
			}
			if err != nil || deleted != !exists {
				t.Fatal("delete acknowledgement confused with disappearance", deleted, err)
			}
			if _, err = kube.DeleteOwnedIngress(context.Background(), "pig", "worker-analyzer", "deployment-uid"); err != nil || deletes != 1 {
				t.Fatal("repeated a pending or completed delete", deletes, err)
			}
		})
	}
}

func TestIngressDeletionRejectsForeignOwnershipAndExpiredLease(t *testing.T) {
	for index, owners := range []any{[]any{}, []any{Obj{"uid": "other", "controller": true}}, []any{Obj{"uid": "ours"}}, []any{Obj{"uid": "ours", "controller": true}}} {
		kube, gate := testKube(t, func(w http.ResponseWriter, r *http.Request) {
			if r.Method != "GET" {
				t.Error("unsafe delete reached API")
			}
			respondJSON(w, Obj{"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": Obj{"uid": "ingress", "resourceVersion": "7", "ownerReferences": owners}})
		})
		// The first three cases fail ownership checks; the last fails the lease guard.
		if index == 3 {
			gate.Revoke()
		}
		deleted, err := kube.DeleteOwnedIngress(context.Background(), "pig", "worker-analyzer", "ours")
		if deleted || statusCode(err) != 409 {
			t.Fatal("unsafe ingress deletion allowed", deleted, err)
		}
	}
}
