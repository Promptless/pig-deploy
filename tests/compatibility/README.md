# Python to Go reconciliation compatibility

`supervisor/internal/supervisor/testdata/python.jsonl.gz` records 1,478
reconciliations across 84 Python test scenarios at baseline commit
`9d75514137e9d3b0ba604ad632d9637d32e1852c`. It contains synthetic test data only.
The Go compatibility test replays each reconciliation from durable state,
checking the sequence of catalog reads, Kubernetes reads, and writes, including
configuration hashes, immutable Job names, ownership, migration hazards, retry,
rollback, acceptance, and supervisor self-update.

The replay supplies the recorded transition UUID. It normalizes equivalent
timestamp representations, JSON formatting inside `PIG_REQUIREMENTS`, and the
safe validation-error message. It also adds the analyzer's
`ad.datadoghq.com/analyzer.logs` pod annotation, which the baseline predates, to
recorded Deployment and Job applies. All other resource fields and operations must
match. This proves parity with the recorded cases; it does not replace the Go
client, leadership, admission, or live analyzer acceptance tests.

To reproduce the fixture, create a separate checkout of the baseline, put this
directory on `PYTHONPATH`, apply the fixture patch, and run there:

```sh
git apply --unidiff-zero /absolute/path/to/this/directory/external-ingress.patch
PYTHONPATH=/absolute/path/to/this/directory \
  PIG_TRANSCRIPTS=/tmp/pig-python.jsonl \
  uv run pytest -p pig_capture tests/test_supervisor.py
gzip -n -c /tmp/pig-python.jsonl > /tmp/python.jsonl.gz
```

The baseline predates the ownership split. The checked-in
`external-ingress.patch` removes endpoint settings, Ingress rendering, and the
obsolete supervisor ingress tests, and makes the Service type explicitly
`ClusterIP`. This is the intentional behavior change for 0.3.4. The fixture was
recaptured from the patched Python baseline without normalizing away hashes,
resources, or operations. Chart tests cover TLS and collector routes; Kubernetes
integration covers Helm adoption with the same Ingress UID, denied supervisor
ingress access, and unchanged analyzer configuration hashes after ingress edits.

The baseline tests generate UUIDs, so new captures are behaviorally equivalent
rather than byte-for-byte reproducible. `pig_capture.py` is a baseline-only
pytest plugin, not part of the supervisor runtime or current Python test suite.
