# Manual analyzer chart

Create a named analyzer installation in Promptless Settings and deliver its
credential through `secrets.existingSecretName` and `secrets.installTokenKey`.
The analyzer resolves its installation identity from this credential. The migration
Job uses database credentials and needs no hosted connection.
`pig.runtimeBaseUrl` defaults to
`https://api.gopromptless.ai`; override it only for another Promptless environment.
Credential rotation immediately revokes the previous credential. Update the
external Secret and restart the analyzer with the replacement credential.

Select instruction repositories in Promptless Settings. Set
`pig.analysis.activationAt` to enable trace analysis from that time.
To enable instruction catalog indexing without trace analysis, set
`pig.analysis.catalogEnabled: true` and leave `activationAt` empty.
Both modes use `pig.analysis.modelApi` and
`pig.analysis.mirrorRoot`.

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

For separate database roles, set `secrets.migrationPostgresDsnKey` to the owner's
key in the existing Secret. Only the migration Job receives that credential;
`secrets.customerPostgresDsnKey` supplies application access. Both DSNs require
`sslmode=verify-full`. To supply a database CA bundle through Kubernetes, create
its ConfigMap in the analyzer namespace before installing the chart.
Set `pig.postgresCaConfigMapName` and
`pig.postgresCaConfigMapKey` to mount its bundle in both the analyzer
and migration Job as `PGSSLROOTCERT`. For RDS, use the
[AWS regional CA bundle](../../examples/aws/README.md). The migration hook needs
the ConfigMap before Helm installs ordinary chart resources.

The chart uses `/readyz` for storage readiness and `/healthz` for process liveness.
See [storage operations](../../STORAGE.md) for permissions and recovery.

## Optional HTTPS ingress

`gateway.enabled` defaults to `false`, and the Service defaults to `ClusterIP`.
Cloud agents need an HTTPS endpoint reachable from their sandbox. Claude Tag
requires a public endpoint because its proxy blocks private IP ranges. Use this
Ingress or an existing customer gateway to reach the private Service. See
[the network contract](../../CONTRACT.md#https-ingress) for exact routes,
authentication requirements, and sandbox verification.

Add this `gateway` block to your chart values for ingress-nginx with a
customer-managed TLS Secret in the analyzer namespace:

```yaml
gateway:
  enabled: true
  className: nginx
  annotations:
    nginx.ingress.kubernetes.io/ssl-redirect: "true"
    nginx.ingress.kubernetes.io/proxy-body-size: "256m"
  hosts:
    - host: pig.example.com
  tls:
    - hosts:
        - pig.example.com
      secretName: pig-tls
```

Enabled gateways require `hosts` and `tls`. A controller-managed certificate may
omit `secretName`; retain matching TLS hosts and configure the certificate and
HTTPS listener through that controller. The chart does not install a controller
or provision certificates. Configure DNS and disable or redirect HTTP.

The default `gateway.paths` exposes only the five exact collector paths listed
in the contract. If you customize paths, keep exact matches and exclude admin
routes. Match the controller's request limit to the worker's trace batch limit,
which defaults to 256 MiB. See [ingress-nginx request limits](https://kubernetes.github.io/ingress-nginx/user-guide/nginx-configuration/annotations/#custom-max-body-size).
Set `gateway.enabled: false` in a Helm upgrade to remove the chart-owned Ingress.
