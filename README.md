                ,-,------,
              _ \(\(_,--'
         <`--'\>/(/(__
         /. .  `'` '  \
        (`')  ,        @
         `-._,        /
            )-)_/--( >
           ''''  ''''

# PIG deployment

Deploy Promptless Instruction Governance into an existing EKS cluster. The 0.3.0
release targets AWS clean installations; Azure/AKS and GCP/GKE support is experimental.
Terraform provisions the customer database, native object storage, network access,
workload identity, and recovery settings. A namespace-scoped supervisor installs
and updates the analyzer from immutable releases.

This repository contains intentionally public deployment assets and supervisor
source. Analyzer application source is maintained separately.

## Start here

- [Infrastructure examples](examples/README.md): AWS, Azure, and GCP prerequisites,
  remote state, Terraform ownership, and the Kubernetes handoff.
- [Deployment contract](CONTRACT.md): release policy, credentials, recovery,
  capacity acknowledgement, and acceptance.
- [Release operations](RELEASING.md): artifact integrity, publication gates, and
  engineering validation still required before the first release.
- [Customer guides](https://promptless.ai/docs/governance/deploy-the-worker/plan-your-deployment/).

The source prepares the **0.3.1 upgrade candidate** for the
[AWS unattended update test](catalog/testing/aws-updates/README.md).
No installable release is implied
by this checkout: `catalog/stable.json` starts empty. A release becomes eligible
for automatic updates only after accepted images and charts are publicly
available and its reviewed catalog promotion is merged. Do not use the existing
0.2.0 worker image for the supervisor contract.

## Repository layout

| Path | Responsibility |
| --- | --- |
| `modules/{aws,azure,gcp}` | Cloud resources for an existing cluster and network |
| `examples/{aws,azure,gcp}` | Locked providers, remote state, and local module references |
| `charts/pig-supervisor` | One-time bootstrap, CRD, and bounded Kubernetes RBAC |
| `charts/pig-trace-analyzer` | Operator-managed analyzer installation |
| `supervisor/pig_supervisor` | Reconciliation, release verification, and publication tools |
| `catalog` | Immutable manifests and the stable release index |
| `releases/requirements` | Reviewed live checks and operator prerequisites |
| `schemas` | Generated CR, release, and acceptance-evidence schemas |

Published charts live at `oci://ghcr.io/promptless/charts/pig-supervisor` and
`oci://ghcr.io/promptless/charts/pig-trace-analyzer`. Every packaged chart
contains an immutable image digest. Source charts require an explicit digest.

## Local validation

See [CI checks](CI.md) for required pull-request checks, real Kubernetes
integration tests, security scanning, and local reproduction.

Use Python 3.11+, uv 0.8.22, Helm 3.18.6, Go from
`tests/crd-validation/go.mod`, and Terraform 1.11.4. Provider versions and
checksums are committed with each example and module.

```sh
uv sync --frozen
uv run ruff check supervisor tests scripts
uv run ruff format --check supervisor tests scripts
uv run pytest -q
uv run python scripts/generate-schema.py
uv build
(cd tests/crd-validation && go run .)
terraform fmt -check -recursive
```

Each cloud module has credential-free mock plans. For example:

```sh
terraform -chdir=examples/aws init -backend=false -input=false -lockfile=readonly
terraform -chdir=examples/aws validate
terraform -chdir=modules/aws init -backend=false -input=false -lockfile=readonly
terraform -chdir=modules/aws test
```

These checks exercise contracts and migration coordination. They do not prove
cloud identity, networking, database TLS, host enrollment, analysis, or Dashboard
synchronization in a real customer environment.
