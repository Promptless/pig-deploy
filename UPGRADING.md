# Operator CRD upgrades

Use this procedure when an accepted release changes required `PIGDeployment`
fields. The CRD is shared across namespaces. Coordinate every supervisor using it
before applying the target schema. Automatic updates accept only additive CRD
changes; [Helm does not upgrade CRDs](https://helm.sh/docs/chart_best_practices/custom_resource_definitions/).

Run these commands from the accepted release's source checkout with the intended
Kubernetes context selected. Substitute each supervisor's namespace and release
name for `pig-system` and `pig-supervisor` below.

1. Save the current CRD and deployment records.

   ```sh
   kubectl get crd pigdeployments.governance.promptless.ai -o yaml > pig-crd-backup.yaml
   kubectl get pigdeployments --all-namespaces -o yaml > pig-deployments-backup.yaml
   ```

   Keep the GitOps manifests and accepted image digests for every namespace.
   The exported records include status and Secret references; preserve them as
   recovery evidence rather than applying the backup over newer state.

2. Stop every supervisor after its active release transition finishes.

   Confirm every deployment has `status.phase: complete` and no maintenance Job
   is running. Suspend GitOps reconciliation for the supervisor deployments and
   `PIGDeployment` resources during the handoff. For each supervisor, run:

   ```sh
   kubectl -n pig-system scale deployment/pig-supervisor --replicas=0
   kubectl -n pig-system wait --for=delete pod -l app.kubernetes.io/name=pig-supervisor --timeout=120s
   ```

   Confirm no supervisor Pods remain before continuing. Leave the analyzer
   workloads, database, object storage, and customer Secrets in place.

3. Prepare each existing GitOps deployment manifest against the target schema.

   Preserve its name, namespace, release policy, hosted credential reference,
   endpoint, storage, identity, scheduling, and model configuration. The `analysis`
   object contains only `activationAt`, `quietWindowHours`, and `model`. Use
   [the example](examples/pig-deployment.yaml) for their shape. Select instruction
   repositories in Promptless Settings.

   Validate each prepared manifest, replacing `deployment.yaml` with its path:

   ```sh
   uv run python -c 'import sys, yaml; from pathlib import Path; from pig_supervisor.models import DeploymentSpec; DeploymentSpec.model_validate(yaml.safe_load(Path(sys.argv[1]).read_text())["spec"])' deployment.yaml
   ```

   Confirm each release policy selects an accepted release compatible with the
   target CRD. Resolve validation errors before touching the installed schema.

4. Apply the target CRD through its bootstrap owner.

   ```sh
   kubectl apply --server-side --field-manager=pig-bootstrap -f charts/pig-supervisor/crds/pigdeployments.yaml
   kubectl wait --for=condition=Established crd/pigdeployments.governance.promptless.ai --timeout=60s
   ```

   Resolve any field-ownership conflict with the existing bootstrap owner before
   proceeding. Do not delete the CRD: deletion also removes its deployment records.
   See [kubectl apply](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_apply/).

5. Apply the prepared deployment manifests through their GitOps owner.

   If kubectl is that owner, validate and apply each manifest:

   ```sh
   kubectl apply --dry-run=server -f deployment.yaml
   kubectl apply -f deployment.yaml
   ```

   Re-read each deployment and compare its spec with the prepared manifest.
   Confirm its UID, status, and Secret references are preserved.

6. Start every supervisor with the accepted target image.

   Update its bootstrap configuration to the target chart and immutable image
   digest, preserving its watched namespace and release catalog. Resume deployment
   GitOps reconciliation. Keep any Flux HelmRelease that competes with supervisor
   self-updates suspended, as required by the [ownership contract](CONTRACT.md#ownership).

   Confirm the supervisor acquires its namespace Lease and reconciles the current
   deployment generation. Wait for `Ready=True` and matching release/configuration
   acceptance before considering the upgrade complete.
