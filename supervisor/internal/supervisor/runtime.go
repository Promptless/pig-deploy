package supervisor

import (
	"context"
	"fmt"
	"net/http"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	coordinationclient "k8s.io/client-go/kubernetes/typed/coordination/v1"
	"k8s.io/client-go/rest"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/manager"
	metricsserver "sigs.k8s.io/controller-runtime/pkg/metrics/server"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	"sigs.k8s.io/controller-runtime/pkg/source"
)

type Options struct{ Namespace, SystemNamespace, Holder, SupervisorName, CatalogURL string }

// Run uses the standard manager, workqueue and client-go leader election. A
// namespace poll is deliberate: releases and referenced Secrets can change
// independently of the CR, and existing installations have no watch permission.
func Run(ctx context.Context, config *rest.Config, options Options) error {
	if options.Namespace == "" || options.SystemNamespace == "" || options.Holder == "" || !validCatalogURL(options.CatalogURL) {
		return fmt.Errorf("WATCH_NAMESPACE, POD_NAMESPACE, POD_UID and a valid RELEASE_CATALOG_URL are required")
	}
	if options.SupervisorName == "" {
		options.SupervisorName = "pig-supervisor"
	}
	config = rest.CopyConfig(config)
	config.Timeout = 15 * time.Second
	config.UserAgent = "pig-supervisor"
	gate := &WriteGate{}
	defer gate.Revoke()
	kube, err := NewKube(config, gate)
	if err != nil {
		return fmt.Errorf("initialize Kubernetes client: %w", safeKubeError(err, "initialize", "client"))
	}
	coordination, err := coordinationclient.NewForConfig(config)
	if err != nil {
		return err
	}
	lease := &PatchLeaseLock{Client: coordination.Leases(options.Namespace), Namespace: options.Namespace, Holder: options.Holder, Gate: gate}
	renewDeadline, retryPeriod := 240*time.Second, 15*time.Second
	mgr, err := ctrl.NewManager(config, manager.Options{
		Scheme: runtime.NewScheme(), MapperProvider: func(*rest.Config, *http.Client) (meta.RESTMapper, error) { return RESTMapper(), nil },
		NewClient:      func(*rest.Config, client.Options) (client.Client, error) { return kube.Client, nil },
		LeaderElection: true, LeaderElectionResourceLockInterface: lease, LeaseDuration: duration(LeaseDuration), RenewDeadline: &renewDeadline, RetryPeriod: &retryPeriod,
		// Do not release before all in-flight writes have finished during shutdown.
		LeaderElectionReleaseOnCancel: false, Metrics: metricsserver.Options{BindAddress: "0"}, HealthProbeBindAddress: "0",
	})
	if err != nil {
		return err
	}
	c := &Controller{Kube: kube, Catalog: NewCatalog(options.CatalogURL), Namespace: options.Namespace, SystemNamespace: options.SystemNamespace, SupervisorName: options.SupervisorName}
	events := make(chan event.GenericEvent, 1)
	if err = mgr.Add(manager.RunnableFunc(func(ctx context.Context) error {
		ticker := time.NewTicker(15 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case events <- event.GenericEvent{Object: &corev1.ConfigMap{ObjectMeta: metav1.ObjectMeta{Namespace: options.Namespace, Name: "reconcile"}}}:
			default:
			}
			select {
			case <-ctx.Done():
				return nil
			case <-ticker.C:
			}
		}
	})); err != nil {
		return err
	}
	runner := &namespaceReconciler{controller: c}
	if err = ctrl.NewControllerManagedBy(mgr).Named("pig-supervisor").WithOptions(controller.Options{MaxConcurrentReconciles: 1}).WatchesRawSource(source.Channel(events, &handler.EnqueueRequestForObject{})).Complete(runner); err != nil {
		return err
	}
	return mgr.Start(ctx)
}
func duration(v time.Duration) *time.Duration { return &v }

type namespaceReconciler struct{ controller *Controller }

func (r *namespaceReconciler) Reconcile(ctx context.Context, _ reconcile.Request) (reconcile.Result, error) {
	c := r.controller
	deployments, err := c.Kube.List(ctx, "PIGDeployment", c.Namespace, "")
	if err != nil {
		return reconcile.Result{}, err
	}
	now := time.Now().UTC()
	for _, deployment := range deployments {
		if err = c.Reconcile(ctx, deployment, len(deployments), now); err != nil {
			// Errors from dependency adapters are sanitized at their boundary.
			ctrl.LoggerFrom(ctx).Error(err, "Reconciliation blocked")
			reason := "DependencyUnavailable"
			if statusCode(err) == 409 {
				reason = "KubernetesConflict"
			}
			status := copyObj(child(deployment, "status"))
			for _, entry := range []struct {
				kind  string
				truth bool
			}{{"Ready", false}, {"Blocked", true}} {
				Condition(status, entry.kind, entry.truth, reason, "A catalog or Kubernetes check failed; inspect supervisor logs for the failed operation and cause.", number(child(deployment, "metadata"), "generation"), now)
			}
			if statusErr := c.Kube.Status(ctx, deployment, status); statusErr != nil {
				return reconcile.Result{}, statusErr
			}
		}
	}
	return reconcile.Result{}, nil
}
