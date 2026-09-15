# PIG AZURE infrastructure module

Use the [existing-cluster example](../../examples/azure/README.md) for
prerequisites, network/identity boundaries, state configuration, and recovery
defaults. This module creates application infrastructure only; it does not create
a cluster or install Kubernetes workloads.

- [Inputs](variables.tf) define sizing, identity, existing network, and backup options.
- [Resources](main.tf) define the complete cloud write scope for plan review.
- [Output](outputs.tf) exports nonsecret deployment configuration.
- [Mock plans](tests/contract.tftest.hcl) validate privacy, recovery, and identity
  constraints without cloud credentials.

The example references this directory from the same pinned checkout. A remote
module consumer must pin `ref` to the full source commit from the accepted release
manifest and commit the root provider lock file. Do not follow an unpinned branch.
