"""§51 threat-model doc — content integrity.

Same discipline as the trust-boundary doc (§43): the threat model is only
useful if every cited module, symbol, and test file actually exists. This test
fails if a citation has drifted, so the doc cannot rot into fiction.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def doc():
    with open(os.path.join(ROOT, "docs", "THREAT_MODEL.md"), encoding="utf-8") as fh:
        return fh.read()


def test_doc_exists():
    assert os.path.exists(os.path.join(ROOT, "docs", "THREAT_MODEL.md"))


def test_doc_names_only_real_modules(doc):
    for m in re.findall(r"sworker/([\w]+)\.py", doc):
        assert os.path.exists(os.path.join(ROOT, "sworker", m + ".py")), f"missing module: sworker/{m}.py"


def test_doc_names_only_real_tests(doc):
    for t in re.findall(r"tests/([\w]+)\.py", doc):
        assert os.path.exists(os.path.join(ROOT, "tests", t + ".py")), f"missing test: tests/{t}.py"


def test_doc_states_core_invariant(doc):
    low = doc.lower()
    assert "the model proposes" in low
    assert "engine" in low and "disposes" in low


def test_doc_names_real_symbols(doc):
    from sworker import permissions, connectors, auth, rbac, engine, approvals
    from sworker.approvals import ApprovalManager
    from sworker.rbac import role_satisfies
    from sworker.connectors import ConnectorManager
    from sworker.auth import AuthProvider
    from sworker.store import WorkerStore
    from sworker.engine import WorkerEngine
    assert hasattr(permissions, "classify")
    assert hasattr(permissions, "DecompositionGuard")
    assert hasattr(WorkerStore, "verify_audit_chain")
    assert hasattr(ConnectorManager, "resolve_credentials")
    assert hasattr(AuthProvider, "authenticate")
    assert hasattr(WorkerEngine, "cancel")
    assert hasattr(ApprovalManager, "vote")
    assert hasattr(role_satisfies, "__call__")
