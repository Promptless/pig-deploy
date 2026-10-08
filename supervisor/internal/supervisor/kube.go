package supervisor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/labels"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type KubeError struct {
	Status                      int
	Operation, Resource, Reason string
}

func (e *KubeError) Error() string {
	return fmt.Sprintf("Kubernetes %s %s returned %d%s", e.Operation, e.Resource, e.Status, e.Reason)
}
func statusCode(err error) int {
	var e *KubeError
	if errors.As(err, &e) {
		return e.Status
	}
	var a apierrors.APIStatus
	if errors.As(err, &a) {
		return int(a.Status().Code)
	}
	return 0
}
func safeKubeError(err error, operation, resource string) error {
	if err == nil {
		return nil
	}
	return &KubeError{Status: statusCode(err), Operation: operation, Resource: resource}
}

var resources = map[string]schema.GroupVersionResource{
	"Deployment": {Group: "apps", Version: "v1", Resource: "deployments"}, "Service": {Version: "v1", Resource: "services"}, "Ingress": {Group: "networking.k8s.io", Version: "v1", Resource: "ingresses"},
	"Job": {Group: "batch", Version: "v1", Resource: "jobs"}, "ConfigMap": {Version: "v1", Resource: "configmaps"}, "Secret": {Version: "v1", Resource: "secrets"}, "ServiceAccount": {Version: "v1", Resource: "serviceaccounts"},
	"Lease": {Group: "coordination.k8s.io", Version: "v1", Resource: "leases"}, "Pod": {Version: "v1", Resource: "pods"}, "PIGDeployment": {Group: "governance.promptless.ai", Version: "v1alpha1", Resource: "pigdeployments"},
	"HelmRelease": {Group: "helm.toolkit.fluxcd.io", Version: "v2", Resource: "helmreleases"}, "CustomResourceDefinition": {Group: "apiextensions.k8s.io", Version: "v1", Resource: "customresourcedefinitions"},
}

// A static mapper avoids discovery and cache-driven permission expansion. In
// particular HelmRelease remains an optional API whose absence returns 404.
func RESTMapper() meta.RESTMapper {
	mapper := meta.NewDefaultRESTMapper(nil)
	for kind, gvr := range resources {
		scope := meta.RESTScopeNamespace
		if kind == "CustomResourceDefinition" {
			scope = meta.RESTScopeRoot
		}
		mapper.AddSpecific(gvr.GroupVersion().WithKind(kind), gvr, gvr, scope)
	}
	return mapper
}
func kubeObject(kind, namespace, name string) *unstructured.Unstructured {
	object := &unstructured.Unstructured{}
	object.SetGroupVersionKind(resources[kind].GroupVersion().WithKind(kind))
	object.SetNamespace(namespace)
	object.SetName(name)
	return object
}

type Kube struct {
	Client client.Client
	HTTP   *http.Client
	Host   string
}

func NewKube(config *rest.Config, gate *WriteGate) (*Kube, error) {
	config = rest.CopyConfig(config)
	config.Wrap(func(rt http.RoundTripper) http.RoundTripper { return &guardedTransport{base: rt, gate: gate} })
	httpClient, err := rest.HTTPClientFor(config)
	if err != nil {
		return nil, err
	}
	cl, err := client.New(config, client.Options{Scheme: runtime.NewScheme(), Mapper: RESTMapper(), HTTPClient: httpClient})
	if err != nil {
		return nil, err
	}
	return &Kube{Client: cl, HTTP: httpClient, Host: strings.TrimRight(config.Host, "/")}, nil
}
func (k *Kube) Get(ctx context.Context, kind, namespace, name string) (Obj, error) {
	obj := kubeObject(kind, namespace, name)
	err := k.Client.Get(ctx, client.ObjectKeyFromObject(obj), obj)
	if apierrors.IsNotFound(err) {
		return nil, nil
	}
	if err != nil {
		return nil, safeKubeError(err, "GET", kind)
	}
	return obj.Object, nil
}
func (k *Kube) List(ctx context.Context, kind, namespace, selector string) ([]Obj, error) {
	list := &unstructured.UnstructuredList{}
	list.SetGroupVersionKind(resources[kind].GroupVersion().WithKind(kind + "List"))
	opts := []client.ListOption{client.InNamespace(namespace)}
	if selector != "" {
		parsed, err := labels.Parse(selector)
		if err != nil {
			return nil, invalid("label selector")
		}
		opts = append(opts, client.MatchingLabelsSelector{Selector: parsed})
	}
	if err := k.Client.List(ctx, list, opts...); err != nil {
		return nil, safeKubeError(err, "LIST", kind)
	}
	result := make([]Obj, 0, len(list.Items))
	for _, v := range list.Items {
		result = append(result, v.Object)
	}
	return result, nil
}
func (k *Kube) Apply(ctx context.Context, document Obj) (Obj, error) {
	metadata := child(document, "metadata")
	kind := str(document, "kind")
	namespace, name := str(metadata, "namespace"), str(metadata, "name")
	existing, err := k.Get(ctx, kind, namespace, name)
	if err != nil {
		return nil, err
	}
	expected := items(metadata["ownerReferences"])
	if existing != nil && len(expected) > 0 {
		owned := false
		for _, v := range items(child(existing, "metadata")["ownerReferences"]) {
			if str(object(v), "uid") == str(object(expected[0]), "uid") {
				owned = true
			}
		}
		if !owned {
			return nil, &KubeError{409, "PATCH", kind, ": resource belongs to a different owner"}
		}
	}
	data, err := json.Marshal(document)
	if err != nil {
		return nil, err
	}
	obj := kubeObject(kind, namespace, name)
	// No ForceOwnership: a conflicting field manager must block the transition.
	if err = k.Client.Patch(ctx, obj, client.RawPatch(types.ApplyPatchType, data), client.FieldOwner("pig-supervisor")); err != nil {
		return nil, safeKubeError(err, "PATCH", kind)
	}
	return obj.Object, nil
}
func (k *Kube) Patch(ctx context.Context, kind, namespace, name string, patch Obj) (Obj, error) {
	patchType := types.MergePatchType
	if kind == "Deployment" {
		patchType = types.StrategicMergePatchType
	}
	data, err := json.Marshal(patch)
	if err != nil {
		return nil, err
	}
	obj := kubeObject(kind, namespace, name)
	if err = k.Client.Patch(ctx, obj, client.RawPatch(patchType, data)); err != nil {
		return nil, safeKubeError(err, "PATCH", kind)
	}
	return obj.Object, nil
}
func (k *Kube) Status(ctx context.Context, deployment, status Obj) error {
	patch := copyObj(status)
	for key := range child(deployment, "status") {
		if _, ok := patch[key]; !ok {
			patch[key] = nil
		}
	}
	metadata := child(deployment, "metadata")
	data, err := json.Marshal(Obj{"metadata": Obj{"resourceVersion": metadata["resourceVersion"]}, "status": patch})
	if err != nil {
		return err
	}
	obj := kubeObject("PIGDeployment", str(metadata, "namespace"), str(metadata, "name"))
	return safeKubeError(k.Client.Status().Patch(ctx, obj, client.RawPatch(types.MergePatchType, data)), "PATCH", "PIGDeployment/status")
}

// DeleteOwnedIngress waits for disappearance, including finalizers. Both UID and
// resourceVersion preconditions protect replacements and concurrent owner edits.
func (k *Kube) DeleteOwnedIngress(ctx context.Context, namespace, name, ownerUID string) (bool, error) {
	existing, err := k.Get(ctx, "Ingress", namespace, name)
	if err != nil || existing == nil {
		return err == nil, err
	}
	metadata := child(existing, "metadata")
	owned := false
	for _, value := range items(metadata["ownerReferences"]) {
		owner := object(value)
		if str(owner, "uid") == ownerUID && owner["controller"] == true {
			owned = true
		}
	}
	if !owned {
		return false, &KubeError{409, "DELETE", "Ingress", ": Ingress belongs to a different owner"}
	}
	if str(metadata, "deletionTimestamp") == "" {
		uid, resourceVersion := types.UID(str(metadata, "uid")), str(metadata, "resourceVersion")
		err = k.Client.Delete(ctx, kubeObject("Ingress", namespace, name), client.Preconditions{UID: &uid, ResourceVersion: &resourceVersion})
		if err != nil && !apierrors.IsNotFound(err) {
			return false, safeKubeError(err, "DELETE", "Ingress")
		}
	}
	existing, err = k.Get(ctx, "Ingress", namespace, name)
	return err == nil && existing == nil, err
}
func (k *Kube) Request(ctx context.Context, method, path string, body Obj) (Obj, error) {
	if path == crdPath {
		if method == "GET" {
			return k.Get(ctx, "CustomResourceDefinition", "", "pigdeployments.governance.promptless.ai")
		}
		if method == "PATCH" {
			return k.Patch(ctx, "CustomResourceDefinition", "", "pigdeployments.governance.promptless.ai", body)
		}
	}
	if method != "GET" || path != "/version" {
		return nil, errors.New("unsupported Kubernetes operation")
	}
	req, err := http.NewRequestWithContext(ctx, method, k.Host+path, nil)
	if err != nil {
		return nil, safeKubeError(err, method, path)
	}
	response, err := k.HTTP.Do(req)
	if err != nil {
		return nil, safeKubeError(err, method, path)
	}
	defer response.Body.Close()
	if response.StatusCode != 200 {
		return nil, &KubeError{Status: response.StatusCode, Operation: method, Resource: path}
	}
	var document Obj
	if err = json.NewDecoder(response.Body).Decode(&document); err != nil {
		return nil, safeKubeError(err, method, path)
	}
	return document, nil
}
