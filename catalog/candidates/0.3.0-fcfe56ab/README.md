# AWS installation candidate 0.3.0

This catalog is for the separately authorized AWS acceptance installation. It is
not a published stable release or evidence that cloud acceptance has completed.
The stable catalog is unchanged. Do not point customer installations at this index.

`inventory.json` records the source commits, immutable runtime images, requirements
digest, and candidate build. `release.json` contains the real deployment contract
and checksummed CRD from that exact public source. Published chart/archive metadata
is omitted because those artifacts have not been accepted or published.

Bootstrap the supervisor chart from the recorded public source commit with the
recorded supervisor image digest, the immutable URL of this candidate index as
`releaseCatalogURL`, and the externally prepared test namespace as `watchNamespace`.
Pin the PIGDeployment to 0.3.0. The supervisor renders its analyzer workloads directly;
this candidate path does not require final chart packages or fabricated acceptance.

The index references the manifest's full commit and checksum. Preserve both commits
while this environment or its reports use them. Candidate use does not activate
stable. After real canonical acceptance, submit evidence through the normal reviewed
publication process. The published charts and final stable manifest require a fresh
clean-install and canonical acceptance check.
