# Manual analyzer chart

Create a named analyzer installation in Promptless Settings and deliver its
credential through `secrets.existingSecretName` and `secrets.installTokenKey`.
The analyzer and migration Job resolve the installation identity from this
credential. `instructionHub.runtimeBaseUrl` defaults to
`https://api.gopromptless.ai`; override it only for another Promptless environment.
Credential rotation immediately revokes the previous credential. Update the
external Secret and restart the analyzer with the replacement credential.

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
