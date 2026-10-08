# PIG bootstrap chart

This chart installs the supervisor and the required collector Ingress. Helm or
GitOps owns the Ingress; the supervisor only manages analyzer releases, workloads,
and migrations. Network changes do not enter the analyzer configuration hash.

Supply [bootstrap values](../../examples/supervisor-values.yaml) and the accepted
supervisor image digest. `ingress.serviceName` must match the stable analyzer
Service name (`<PIGDeployment name>-analyzer`) and is also the Ingress name.
The Ingress is created in `watchNamespace`, even when Helm is installed elsewhere.
Set `ingress.hostname`, `ingress.ingressClassName`, and either `tlsSecretName` or
controller certificate annotations. There is no ingress enable/disable flag.
See [HTTPS requirements](../../CONTRACT.md#https-ingress) and the
[AWS values](../../examples/aws/README.md).

For an existing installation, follow the [ownership handoff](../../UPGRADING.md)
before upgrading. It adopts the same Ingress without deleting it or changing its
UID. Removing a PIGDeployment does not remove this Helm-owned Ingress; uninstalling
its Helm release does.

Before any later Helm upgrade, carry forward the currently accepted supervisor
image digest and release catalog into the values. The supervisor may have updated
its own image since bootstrap. Keep a Flux HelmRelease suspended if it would
compete with those self-updates; reconcile ingress through an explicit Helm
upgrade with current values, or maintain the rendered Ingress directly in GitOps.
