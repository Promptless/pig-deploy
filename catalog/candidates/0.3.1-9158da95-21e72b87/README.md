# AWS update candidate 0.3.1

This candidate upgrades the tested 0.3.0 analyzer to the merged dependency
security update, including AnyIO 4.14.2. Its schema revision remains 3. The
supervisor image and both source charts report version 0.3.1.

`inventory.json` records exact source commits, image digests, requirements digest,
successful build runs, and execution checks. Both images pulled anonymously in
EKS; the analyzer reported the expected schema and maintenance capabilities.
Live upgrade acceptance is pending.

Use the [AWS update test catalog](../../testing/aws-updates/README.md) to exercise
the transition. Its existing 0.3.0 entry stays immutable. The public stable catalog
is unchanged, and no rollback path is declared.
