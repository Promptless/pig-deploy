# Operator CRD and ingress ownership upgrades

For 0.3.4, move the entire `spec.endpoint` configuration to Helm/GitOps. The
supervisor no longer reads or manages Ingress resources. Adopt the existing object
so its UID and load balancer are preserved. This is a coordinated bootstrap
upgrade, not an unattended supervisor update.

The CRD is shared across namespaces. Coordinate every supervisor using it before
applying the target schema. Automatic updates accept only additive CRD changes;
[Helm does not upgrade CRDs](https://helm.sh/docs/chart_best_practices/custom_resource_definitions/).
Use the accepted release's source checkout and an explicit Kubernetes context.
The commands below use `pig` for the watched namespace, `pig-system` for the Helm
release namespace, `pig-supervisor` for the release, and `acme` for PIGDeployment.
Substitute your installation's values consistently.

1. Back up the current CRD, PIGDeployment records, Ingress, and Helm values.

   ```sh
   kubectl --context CONTEXT get crd pigdeployments.governance.promptless.ai -o yaml > pig-crd-backup.yaml
   kubectl --context CONTEXT get pigdeployments --all-namespaces -o yaml > pig-deployments-backup.yaml
   kubectl --context CONTEXT -n pig get pigdeployment acme -o json > deployment-before.json
   kubectl --context CONTEXT -n pig get ingress acme-analyzer -o json > ingress-before.json
   helm --kube-context CONTEXT get values pig-supervisor -n pig-system -o yaml > supervisor-values.yaml
   ```

   Record the running supervisor's immutable image digest and release catalog;
   self-updates may have changed them since the last Helm install. Preserve status
   and Secret references as recovery evidence, without applying old exports over
   newer state. Keep these backups local to the operator workflow.

2. Finish active release transitions, then stop every supervisor sharing the CRD.

   Require `status.phase: complete` and no running maintenance Job. An installation
   paused in migration or verification must finish or recover its original
   candidate first; do not switch its catalog to a different manifest with the
   same version. Suspend GitOps reconciliation of the bootstrap release and
   PIGDeployment during the handoff. For each supervisor:

   ```sh
   kubectl --context CONTEXT -n pig-system scale deployment/pig-supervisor --replicas=0
   kubectl --context CONTEXT -n pig-system wait --for=delete pod -l app.kubernetes.io/name=pig-supervisor --timeout=120s
   ```

   Confirm no supervisor Pods remain. Leave the analyzer, database, object
   storage, and customer Secrets in place.

3. Prepare both the deployment manifest and bootstrap values.

   Remove **all of `spec.endpoint`**, including any unreleased `enabled` flag,
   from the PIGDeployment manifest. Preserve its name, namespace, release policy,
   credentials, storage, identity, scheduling, and model configuration. Validate:

   ```sh
   uv run python -c 'import sys, yaml; from pathlib import Path; from pig_supervisor.models import DeploymentSpec; DeploymentSpec.model_validate(yaml.safe_load(Path(sys.argv[1]).read_text())["spec"])' deployment.yaml
   ```

   In `supervisor-values.yaml`, set `ingress.serviceName` to `acme-analyzer` and
   copy the current Ingress's host, class, TLS Secret (if used), and controller
   annotations into `ingress.hostname`, `ingress.ingressClassName`,
   `ingress.tlsSecretName`, and `ingress.annotations`. The old
   `endpoint.ingressAnnotations` becomes `ingress.annotations`. Preserve ACM,
   ALB scheme/group, listener, DNS, and other controller settings. Do not copy
   `ownerReferences` or the `kubectl.kubernetes.io/last-applied-configuration`
   annotation. The chart adds the cloud-enrollment lease route if it was absent.

   Set the accepted **0.3.4 supervisor digest** and retain the release catalog and
   watched namespace. Compare the rendered Ingress to the saved object:

   ```sh
   helm lint charts/pig-supervisor -f supervisor-values.yaml
   helm template pig-supervisor charts/pig-supervisor -n pig-system -f supervisor-values.yaml > bootstrap-rendered.yaml
   ```

   Confirm the name, namespace, backend Service, existing routes, host, class,
   and TLS settings match. See [bootstrap values](examples/supervisor-values.yaml)
   and [HTTPS requirements](CONTRACT.md#https-ingress).

4. Apply the target CRD, then the prepared PIGDeployment through its existing owner.

   ```sh
   kubectl --context CONTEXT apply --server-side --field-manager=pig-bootstrap -f charts/pig-supervisor/crds/pigdeployments.yaml
   kubectl --context CONTEXT wait --for=condition=Established crd/pigdeployments.governance.promptless.ai --timeout=60s
   kubectl --context CONTEXT apply --dry-run=server -f deployment.yaml
   kubectl --context CONTEXT apply -f deployment.yaml
   ```

   The last two commands assume kubectl owns the manifest; use the equivalent
   GitOps operation otherwise. Resolve field-ownership conflicts with the
   bootstrap owner. Never delete/recreate the CRD or PIGDeployment. Re-read the
   deployment and confirm its UID, status, release policy, and Secret references
   remain intact and its spec has no endpoint block. Kubernetes may prune removed
   fields rather than reject them, so also update the source manifest.

5. Hand the existing Ingress to Helm while the old supervisor remains stopped.

   Refresh the JSON snapshots, then generate a metadata-only patch:

   ```sh
   kubectl --context CONTEXT -n pig get pigdeployment acme -o json > deployment-before.json
   kubectl --context CONTEXT -n pig get ingress acme-analyzer -o json > ingress-before.json
   uv run python scripts/ingress-handoff.py \
     --deployment deployment-before.json --ingress ingress-before.json \
     --release-name pig-supervisor --release-namespace pig-system > ingress-handoff.json
   cat ingress-handoff.json
   kubectl --context CONTEXT -n pig patch ingress acme-analyzer --type=json --patch-file=ingress-handoff.json
   ```

   The offline helper requires exactly one owner matching the PIGDeployment UID,
   refuses conflicting Helm ownership, and preserves existing labels/annotations.
   The patch tests the Ingress UID, resource version, and owner references before
   removing the PIGDeployment owner and adding Helm adoption metadata. If a test
   fails, re-read the object and review the new patch; do not drop its guards.
   No spec or load-balancer status is patched. Do not delete the Ingress or use
   `helm --force`, which may replace it.

6. Upgrade the bootstrap release and verify the handoff.

   ```sh
   helm --kube-context CONTEXT upgrade pig-supervisor charts/pig-supervisor \
     -n pig-system -f supervisor-values.yaml --wait --timeout 5m
   kubectl --context CONTEXT -n pig get ingress acme-analyzer -o json > ingress-after.json
   ```

   This adopts the existing Ingress, removes ingress access from the supervisor
   Role, and starts the accepted Go image. Verify that the Ingress UID, hostname,
   certificate, and load-balancer address match the backup and that it has no
   PIGDeployment owner reference. Verify HTTPS health and authenticated collector
   requests. Confirm the supervisor acquires the namespace Lease and observes the
   current deployment generation. Removing endpoint settings changes the analyzer
   configuration hash once; wait for configuration revalidation and matching
   acceptance. Later ingress annotation changes do not trigger that revalidation.

   Resume PIGDeployment GitOps reconciliation. Keep a Flux HelmRelease suspended
   if it would compete with supervisor self-updates. For later ingress changes,
   use an explicit Helm upgrade carrying forward the currently accepted image
   and catalog, or manage the rendered ingress through GitOps. Uninstalling the
   owning Helm release removes the Ingress; deleting PIGDeployment no longer does.

If the handoff stops before Helm succeeds, keep the old supervisor stopped and
repair forward using the saved object and rendered chart. Do not restart the old
image against the endpoint-free CRD/spec. Reversing the handoff requires a reviewed
CRD/spec and ownership restoration, plus analyzer/schema compatibility checks;
`helm rollback` alone does not restore those contracts. These checks do not replace
[canonical pipeline acceptance](CONTRACT.md#acceptance).
