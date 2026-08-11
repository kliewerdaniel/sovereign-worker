"""§43 trust-boundary doc — content integrity.

The doc is only useful if it names *real* modules, functions, and test files.
This test fails if a cited path does not exist in the tree, so the doc cannot
silently drift into fiction as the code changes.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def doc():
    with open(os.path.join(ROOT, "docs", "TRUST_BOUNDARY.md"), encoding="utf-8") as fh:
        return fh.read()


def test_doc_exists():
    assert os.path.exists(os.path.join(ROOT, "docs", "TRUST_BOUNDARY.md"))


def test_doc_names_only_real_modules(doc):
    # every `sworker/<file>.py` citation must exist
    for m in re.findall(r"sworker/([\w]+\.py)", doc):
        assert os.path.exists(os.path.join(ROOT, "sworker", m)), f"missing module: sworker/{m}"


def test_doc_names_only_real_tests(doc):
    # every `tests/<file>.py` citation must exist
    for t in re.findall(r"tests/([\w]+\.py)", doc):
        assert os.path.exists(os.path.join(ROOT, "tests", t)), f"missing test: tests/{t}"


def test_doc_states_core_invariant(doc):
    # the spine must be present, verbatim intent
    assert "The model proposes" in doc
    assert "engine disposes" in doc.lower()


def test_doc_names_real_engine_symbols(doc):
    # the cited engine/store/auth symbols must actually be defined
    from sworker import engine, store, auth
    from sworker.engine import WorkerEngine
    from sworker.store import WorkerStore
    from sworker.auth import AuthProvider
    assert hasattr(WorkerEngine, "_resolve_secret")
    assert hasattr(WorkerEngine, "cancel")
    assert hasattr(WorkerStore, "verify_audit_chain")
    assert hasattr(AuthProvider, "authenticate")


def test_doc_names_real_connector_symbols(doc):
    from sworker.connectors import ConnectorManager
    assert hasattr(ConnectorManager, "resolve_credentials")
