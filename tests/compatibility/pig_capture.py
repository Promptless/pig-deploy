"""Capture the Python supervisor's tested API transcript for Go migration parity.

Run from a checkout of baseline 9d75514 with external-ingress.patch applied, with this file on PYTHONPATH:
  PIG_TRANSCRIPTS=/absolute/output.jsonl uv run pytest -p pig_capture tests/test_supervisor.py
Each reconciliation is independent and records immutable inputs, API observations,
and intended writes. The Go replay also restarts the controller for every record.
"""

import json
import os
from copy import deepcopy
from functools import wraps
from pathlib import Path

import httpx
from pig_supervisor.controller import Controller
from pig_supervisor.kube import KubeError

CURRENT = ""
RECORDS = []


def pytest_runtest_setup(item):
    global CURRENT
    CURRENT = item.nodeid


original = Controller.reconcile


@wraps(original)
def capture(self, deployment, population, now):
    record = {
        "test": CURRENT,
        "deployment": deepcopy(deployment),
        "population": population,
        "now": now.isoformat(),
        "namespace": self.namespace,
        "systemNamespace": self.system_namespace,
        "supervisorName": self.supervisor_name,
        "catalogURL": self.catalog_url,
        "events": [],
    }
    old_kube, old_catalog = self.kube, self.catalog

    class Recorder:
        def __getattr__(self, name):
            def call(*args, **kwargs):
                event = {"operation": name, "args": deepcopy(args), "kwargs": deepcopy(kwargs)}
                record["events"].append(event)
                try:
                    result = getattr(old_kube, name)(*args, **kwargs)
                    event["result"] = deepcopy(result)
                    return result
                except KubeError as exc:
                    event["error"] = {"status": exc.status, "message": str(exc)}
                    raise

            return call

    def request(req):
        response = old_catalog.send(req)
        record["events"].append(
            {"operation": "http", "url": str(req.url), "status": response.status_code, "body": response.read().decode()}
        )
        return response

    self.kube = Recorder()
    self.catalog = httpx.Client(transport=httpx.MockTransport(request))
    try:
        return original(self, deployment, population, now)
    except Exception as exc:
        record["error"] = type(exc).__name__
        raise
    finally:
        self.catalog.close()
        self.kube, self.catalog = old_kube, old_catalog
        RECORDS.append(record)


Controller.reconcile = capture


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["PIG_TRANSCRIPTS"]).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in RECORDS))
