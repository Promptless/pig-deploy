# Manual analyzer chart

Create a named analyzer installation in Promptless Settings and deliver its
credential through `secrets.existingSecretName` and `secrets.installTokenKey`.
The analyzer resolves its installation identity from this credential. The migration
Job uses database credentials and needs no hosted connection.
`instructionHub.runtimeBaseUrl` defaults to
`https://api.gopromptless.ai`; override it only for another Promptless environment.
Credential rotation immediately revokes the previous credential. Update the
external Secret and restart the analyzer with the replacement credential.

Select instruction repositories in Promptless Settings. Set
`instructionHub.analysis.activationAt` to enable trace analysis from that time.
To enable instruction catalog indexing without trace analysis, set
`instructionHub.analysis.catalogEnabled: true` and leave `activationAt` empty.
Both modes use `instructionHub.analysis.modelApi` and
`instructionHub.analysis.mirrorRoot`.

Create the analyzer ServiceAccount before installing the chart. Apply the cloud
identity annotations from Terraform's `deployment_configuration` output to that
account, then set `serviceAccount.name` to its name. The default is
`pig-analyzer`, with `serviceAccount.create: false`. The analyzer and migration
Job use this account.

The migration Job runs before Helm installs ordinary chart resources, so
`serviceAccount.create: true` requires `migrationJob.enabled: false`. Use that
combination only when you manage migrations separately. See [Helm hook ordering](https://helm.sh/docs/topics/charts_hooks/).

Set `nodeSelector` to apply the same node placement to the analyzer and migration
Job. Follow the [GKE example](../../examples/gcp/README.md) for Standard clusters
that use workload identity.

When exposing trace uploads through ingress-nginx, set
`gateway.annotations.nginx.ingress.kubernetes.io/proxy-body-size` to `"256m"`
to match the worker's default request limit. Configure equivalent limits when
using another ingress controller. See [ingress-nginx request limits](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#custom-max-body-size).

For separate database roles, set `secrets.migrationPostgresDsnKey` to the owner's
key in the existing Secret. Only the migration Job receives that credential;
`secrets.customerPostgresDsnKey` supplies application access. Both DSNs require
`sslmode=verify-full`. Use `instructionHub.postgresCaConfigMapName` and
`instructionHub.postgresCaConfigMapKey` to mount a private CA.

The chart uses `/readyz` for storage readiness and `/healthz` for process liveness.
See [storage operations](../../STORAGE.md) for permissions and recovery.
