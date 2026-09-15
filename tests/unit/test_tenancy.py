"""Tenant scope rules.

These are small functions, and they are the ones that decide whether one hotel
can read another's documents. They get their own tests.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.enums import Role
from app.core.errors import TenantScopeError
from app.core.tenancy import Principal, TenantContext, scopes_for, system_principal

pytestmark = pytest.mark.unit

ORG_A = uuid.uuid4()
ORG_B = uuid.uuid4()
GINZA = uuid.uuid4()
CHIYODA = uuid.uuid4()


class TestNarrowing:
    def test_unpinned_principal_may_narrow_to_a_location(self) -> None:
        scope = TenantContext(ORG_A).narrowed_to(GINZA)
        assert scope.organization_id == ORG_A
        assert scope.location_id == GINZA

    def test_pinned_principal_may_restate_its_own_location(self) -> None:
        scope = TenantContext(ORG_A, GINZA).narrowed_to(GINZA)
        assert scope.location_id == GINZA

    def test_pinned_principal_cannot_move_to_another_location(self) -> None:
        """The Ginza front desk asking for Chiyoda's documents is an error.

        A silent fallback to their own scope would be worse: the caller would
        believe they had Chiyoda's answer.
        """
        with pytest.raises(TenantScopeError):
            TenantContext(ORG_A, GINZA).narrowed_to(CHIYODA)

    def test_narrowing_to_none_is_a_no_op(self) -> None:
        scope = TenantContext(ORG_A, GINZA)
        assert scope.narrowed_to(None) == scope

    def test_ownership_is_asserted_not_assumed(self) -> None:
        TenantContext(ORG_A).assert_owns(ORG_A)
        with pytest.raises(TenantScopeError):
            TenantContext(ORG_A).assert_owns(ORG_B)


class TestPrincipals:
    def test_tenant_derives_from_the_principal(self) -> None:
        principal = Principal(
            user_id=uuid.uuid4(),
            organization_id=ORG_A,
            location_id=GINZA,
            role=Role.USER,
        )
        assert principal.tenant == TenantContext(ORG_A, GINZA)

    def test_admin_scopes_are_a_superset_of_user_scopes(self) -> None:
        assert scopes_for(Role.USER) < scopes_for(Role.ADMIN)

    def test_users_cannot_write_knowledge(self) -> None:
        user = Principal(
            user_id=uuid.uuid4(),
            organization_id=ORG_A,
            role=Role.USER,
            scopes=scopes_for(Role.USER),
        )
        assert user.has_scope("knowledge:read")
        assert not user.has_scope("documents:write")
        with pytest.raises(TenantScopeError):
            user.require_admin()

    def test_admin_holds_every_scope_implicitly(self) -> None:
        admin = Principal(
            user_id=uuid.uuid4(),
            organization_id=ORG_A,
            role=Role.ADMIN,
            scopes=scopes_for(Role.ADMIN),
        )
        admin.require_admin()
        assert admin.has_scope("anything:at:all")

    def test_worker_principal_is_scoped_to_one_organization(self) -> None:
        """Background work goes through the same tenant machinery as requests."""
        worker = system_principal(ORG_A, uuid.uuid4())
        assert worker.tenant.organization_id == ORG_A
        assert worker.is_admin
