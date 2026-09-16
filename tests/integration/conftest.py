"""Opt-in integration tests; every Kubernetes command targets the disposable kind cluster."""

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-kubernetes", action="store_true", help="Use the disposable kind-pig-ci cluster")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-kubernetes"):
        return
    for item in items:
        if "tests/integration/" in item.nodeid:
            item.add_marker(pytest.mark.skip(reason="requires --run-kubernetes and a disposable kind-pig-ci cluster"))
