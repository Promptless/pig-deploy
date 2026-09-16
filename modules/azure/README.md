# PIG AZURE infrastructure module

Use the [existing-cluster example](../../examples/azure/README.md) for
prerequisites, network/identity boundaries, state configuration, and recovery
defaults. This module creates application infrastructure only; it does not create
a cluster or install Kubernetes workloads.

Set `data_plane_available = false` in the root AzureRM provider's
`features.storage` block, as shown in the
[example provider](../../examples/azure/main.tf). This lets Terraform create the
private storage account through Azure Resource Manager before its private
endpoint exists. The module's account, container, and lifecycle policy use
control-plane APIs.

- [Inputs](variables.tf) define sizing, identity, existing network, and backup options.
- [Resources](main.tf) define the complete cloud write scope for plan review.
- [Output](outputs.tf) exports nonsecret deployment configuration.
- [Mock plans](tests/contract.tftest.hcl) validate privacy, recovery, and identity
  constraints without cloud credentials.

The example references this directory from the same pinned checkout. A remote
module consumer must pin `ref` to the full source commit from the accepted release
manifest and commit the root provider lock file. Do not follow an unpinned branch.
