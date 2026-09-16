# Continuous integration

Pull requests run without cloud credentials or access to the private analyzer.
All except dependency review also run after a merge to `main`. Workflow jobs have explicit timeouts,
and a newer commit cancels superseded checks on the same branch or pull request.

## Required pull-request checks

Configure the following status checks in the `main` branch ruleset after their
first successful run. Adding workflow files does not configure branch protection.
Dependency review requires GitHub's dependency graph; it and dependency alerts
are enabled for this repository.

| Check | Coverage |
| --- | --- |
| `contracts` | Ruff, unit and chart-render tests, actionlint, generated schemas, Kubernetes CRD validation, Python packaging, and supervisor image smoke test |
| `terraform (aws)`, `terraform (azure)`, `terraform (gcp)` | Format, locked-provider initialization, example validation, credential-free mock plans, and TFLint with provider-specific rules |
| `kubernetes (v1.30.10)`, `kubernetes (v1.37.0)` | Real API, chart installation and upgrade, and supervisor process tests in disposable kind clusters |
| `dependency-review` | Newly introduced dependencies with high or critical known vulnerabilities |
| `source-security` | Repository dependency, secret, and infrastructure configuration scans |
| `image-security` | High or critical fixable vulnerabilities in the built supervisor image |

All actions use full commit pins. Dependabot opens weekly updates for Actions,
uv, the container base image, Terraform providers, and the Go admission validator.
The security workflow also runs weekly and can be dispatched manually so newly
disclosed vulnerabilities are checked even when the repository has no new commits.
Trivy dependency and image scans block high/critical vulnerabilities with an available
fix; dependency review blocks newly introduced high/critical vulnerabilities even
without a fix. Secret and infrastructure scans block high/critical findings.
Infrastructure scanning includes all Terraform modules, the Dockerfile, and both
charts rendered with explicit required values. A failed render fails the job. Tool errors
fail their jobs. Review any future scanner exception with a specific reason and
scope instead of disabling a scanner or ignoring an entire class of findings.
The two path-scoped exceptions in [.github/trivyignore.yaml](.github/trivyignore.yaml)
cover the supervisor's required Service/Ingress permissions and Azure storage's
intentional refusal of a trusted-services firewall bypass.

## Kubernetes integration

The matrix covers the contract's Kubernetes 1.30 minimum and a current version.
Each job creates its own kind cluster and local registry, builds the checked-out
supervisor, and installs the shipped Helm chart using the local image's digest.
It does not publish an image or require GHCR credentials. The suite checks:

- A real supervisor process records the paused condition without a catalog;
  a Helm configuration upgrade replaces the process, which acquires the Lease
  and reconciles a new generation while preserving the custom resource.
- The API rejects an invalid custom resource. Status updates preserve the spec,
  reject stale resource versions, and remove obsolete status fields.
- Secret rotation changes the configuration hash using real resource versions.
- Server-side apply preserves the allocated Service IP, rejects field-manager
  conflicts, and refuses adoption of a resource belonging to another owner.
- Competing clients respect the namespace Lease, and the chart's ServiceAccount
  can update only the intended CRD while forbidden identity, Secret, RBAC, and
  cross-namespace operations return authorization errors.

The Lease expiry timestamp is advanced explicitly in handoff tests to avoid a
five-minute wait. These checks do not exercise analyzer migrations or prove
cloud identity, database TLS, trace persistence, analysis, or Dashboard delivery.
Those remain the separately authorized three-cloud gates in [RELEASING.md](RELEASING.md).

`uv run pytest -q` skips cluster tests. To run them locally, reproduce the
cluster/registry setup and image build in
[the Kubernetes workflow](.github/workflows/kubernetes.yml), then run:

```sh
export PIG_TEST_IMAGE='pig-registry:5001/pig-supervisor@sha256:REPLACE_WITH_BUILT_DIGEST'
uv run pytest tests/integration --run-kubernetes -v
```

The suite requires a disposable cluster named `pig-ci` and explicitly targets
the `kind-pig-ci` context. It creates resources in `pig-ci` and `pig-ci-system`
and installs a cluster-scoped CRD. Delete that test cluster after local runs.
CI retains JUnit results and failed-cluster logs for seven days. Kind's
post-job cleanup deletes the cluster and registry when the job ends.

TFLint can also run locally after installing the version pinned in the workflow:

```sh
config="$PWD/.github/tflint/aws.hcl"
tflint --init --config "$config"
tflint --chdir=modules/aws --config "$config"
tflint --chdir=examples/aws --config "$config"
```

Repeat with `azure` and `gcp`. The pinned kind images, Kubernetes/kind versions,
scanner version, and TFLint plugin versions need deliberate updates alongside
their validation; Dependabot does not update arbitrary workflow inputs.
