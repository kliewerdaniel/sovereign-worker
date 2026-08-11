"""RBAC tests (spec §5)."""

import pytest

from sworker.rbac import PERMISSIONS, RBAC


def test_admin_grants_everything():
    rbac = RBAC()
    assert rbac.authorize("admin", "run:create")
    assert rbac.authorize("admin", "secret:manage")
    assert rbac.authorize("admin", sorted(PERMISSIONS)[0])


def test_viewer_is_read_only():
    rbac = RBAC()
    assert rbac.authorize("viewer", "run:read")
    assert not rbac.authorize("viewer", "run:create")
    assert not rbac.authorize("viewer", "approval:decide")


def test_unknown_role_denies():
    rbac = RBAC()
    assert not rbac.authorize("ghost", "run:create")


def test_unknown_permission_denies():
    rbac = RBAC()
    assert not rbac.authorize("admin", "does:not:exist")


def test_custom_role_only_known_grants():
    rbac = RBAC({"intern": ["run:read", "made:up:perm"]})
    assert rbac.authorize("intern", "run:read")
    assert not rbac.authorize("intern", "made:up:perm")
    assert not rbac.authorize("intern", "run:create")


def test_operator_can_decide_approvals():
    rbac = RBAC()
    assert rbac.authorize("operator", "approval:decide")
    assert rbac.authorize("operator", "schedule:manage")
    assert not rbac.authorize("operator", "user:manage")
