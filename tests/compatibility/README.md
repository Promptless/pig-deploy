# Python to Go reconciliation compatibility

`supervisor/internal/supervisor/testdata/python.jsonl.gz` records 1,500
reconciliations across 86 Python test scenarios at baseline commit
`2ec62f33c673e23ec2d79d02c9725adee920540d`. It contains synthetic test data only.
The Go compatibility test replays each reconciliation from durable state,
checking the sequence of catalog reads, Kubernetes reads, and writes, including
configuration hashes, immutable Job names, ownership, migration hazards, retry,
rollback, acceptance, and supervisor self-update.

The replay supplies the recorded transition UUID. It normalizes equivalent
timestamp representations, JSON formatting inside `PIG_REQUIREMENTS`, and the
safe validation-error message. All other resource fields and operations must
match. This proves parity with the recorded cases; it does not replace the Go
client, leadership, admission, or live analyzer acceptance tests.

To reproduce the fixture, create a separate checkout of the baseline, put this
directory on `PYTHONPATH`, and run there:

```sh
PYTHONPATH=/absolute/path/to/this/directory \
  PIG_TRANSCRIPTS=/tmp/pig-python.jsonl \
  uv run pytest -p pig_capture tests/test_supervisor.py
gzip -n -c /tmp/pig-python.jsonl > /tmp/python.jsonl.gz
```

The baseline tests generate UUIDs, so new captures are behaviorally equivalent
rather than byte-for-byte reproducible. `pig_capture.py` is a baseline-only
pytest plugin, not part of the supervisor runtime or current Python test suite.
