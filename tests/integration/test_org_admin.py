"""An organization administrator managing their own people.

The happy paths here are thin on purpose. What is worth testing is the set of
ways an admin could break their own organization or reach outside it -- and, for
the last-admin rule, that the guard actually holds when two requests race, which
a plain count would not.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.core.db import UnitOfWork
from app.core.enums import Role
from app.core.errors import AuthenticationError, ConflictError, NotFoundError, TenantScopeError
from app.core.tenancy import Principal, TenantContext, scopes_for
from app.services.auth.service import AuthService
from app.services.users import management

pytestmark = [pytest.mark.integration]

PASSWORD = "org-admin-password-123"


@dataclass(slots=True)
class People:
    admin: Principal
    coadmin: Principal
    member: Principal
    tenant: TenantContext


@pytest.fixture
async def people(
    settings: Settings,
    org_uow: UnitOfWork,
    org_tenant: TenantContext,
    seeded_org: dict,
    app_engine: None,
) -> AsyncIterator[People]:
    """Exactly two admins and one member, all with real passwords.

    The `seeded_org` fixture already inserts an admin row with an unusable
    password hash, purely so `documents.created_by` has something to point at.
    It is retired here, because "exactly two admins" is the precondition every
    last-admin test below depends on -- leaving a third one around would make
    those tests pass while proving nothing.
    """
    auth = AuthService(settings)
    suffix = uuid.uuid4().hex[:8]

    async with org_uow.begin() as session:
        from app.repositories.user import get_user

        placeholder = await get_user(session, org_tenant, seeded_org["admin_id"])
        placeholder.is_active = False

    admin = await auth.create_user(
        org_uow, org_tenant, email=f"a1-{suffix}@t.test", password=PASSWORD, role=Role.ADMIN
    )
    coadmin = await auth.create_user(
        org_uow, org_tenant, email=f"a2-{suffix}@t.test", password=PASSWORD, role=Role.ADMIN
    )
    member = await auth.create_user(
        org_uow, org_tenant, email=f"u1-{suffix}@t.test", password=PASSWORD, role=Role.USER
    )
    yield People(admin=admin, coadmin=coadmin, member=member, tenant=org_tenant)


def actor_for(principal: Principal) -> management.Actor:
    return management.Actor(
        label=principal.email or str(principal.user_id),
        user_id=principal.user_id,
        location_id=principal.location_id,
    )


class TestListing:
    async def test_an_admin_sees_only_their_own_organizations_users(
        self, org_uow, people, seeded_org
    ) -> None:
        from app.repositories.user import count_users, list_users

        async with org_uow.begin() as session:
            users = await list_users(session, people.tenant, limit=200)
            total = await count_users(session, people.tenant)

        assert {u.organization_id for u in users} == {seeded_org["organization_id"]}
        assert total == len(users)

    async def test_the_total_matches_the_filter_it_was_asked_for(self, org_uow, people) -> None:
        """A page and its total must be built from the same predicates.

        Otherwise a table says "3 results" above a list of one -- the classic
        symptom of a count query that drifted from its list query.
        """
        from app.repositories.user import count_users, list_users

        async with org_uow.begin() as session:
            admins = await list_users(session, people.tenant, role=Role.ADMIN, limit=200)
            admin_total = await count_users(session, people.tenant, role=Role.ADMIN)

        assert len(admins) == admin_total
        assert all(u.role is Role.ADMIN for u in admins)

    async def test_pagination_returns_distinct_pages(self, org_uow, people) -> None:
        from app.repositories.user import list_users

        async with org_uow.begin() as session:
            first = await list_users(session, people.tenant, limit=1, offset=0)
            second = await list_users(session, people.tenant, limit=1, offset=1)

        assert len(first) == len(second) == 1
        assert first[0].id != second[0].id

    async def test_a_foreign_user_id_is_not_found(self, org_uow, people) -> None:
        with pytest.raises(NotFoundError):
            await management.update_user(
                org_uow,
                people.tenant,
                user_id=uuid.uuid4(),
                changes={"full_name": "Nobody"},
                actor=actor_for(people.admin),
            )


class TestLastAdminProtection:
    """An organization must never be left with no way back in."""

    async def test_the_last_admin_cannot_be_disabled(self, org_uow, people) -> None:
        await management.set_user_active(
            org_uow,
            people.tenant,
            user_id=people.coadmin.user_id,
            active=False,
            actor=actor_for(people.admin),
        )

        # `admin` is now the only one left, and nobody can remove them.
        with pytest.raises(ConflictError):
            await management.set_user_active(
                org_uow,
                people.tenant,
                user_id=people.admin.user_id,
                active=False,
                actor=actor_for(people.coadmin),
            )

    async def test_the_last_admin_cannot_be_demoted(self, org_uow, people) -> None:
        await management.set_user_active(
            org_uow,
            people.tenant,
            user_id=people.coadmin.user_id,
            active=False,
            actor=actor_for(people.admin),
        )

        with pytest.raises(ConflictError):
            await management.update_user(
                org_uow,
                people.tenant,
                user_id=people.admin.user_id,
                changes={"role": Role.USER},
                actor=actor_for(people.coadmin),
            )

    async def test_the_admin_check_takes_a_lock_that_actually_blocks(
        self, settings, db_engine, people
    ) -> None:
        """The guard is a read-then-write, so the read must block a rival.

        Worth stating why this is tested at the lock rather than by racing two
        demotions: two such demotions serialise on their own often enough that
        the race almost never shows up, so that test passes with or without the
        lock and proves nothing. This one is discriminating -- remove
        ``.with_for_update()`` from ``lock_active_admins`` and the contender
        acquires immediately instead of timing out.
        """
        from sqlalchemy import text

        from app.repositories.user import lock_active_admins

        maker = async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def holder() -> None:
            async with UnitOfWork(people.tenant, settings, sessionmaker=maker).begin() as session:
                await lock_active_admins(session, people.tenant)
                holding.set()
                await release.wait()

        async def contender() -> str:
            await holding.wait()
            async with UnitOfWork(people.tenant, settings, sessionmaker=maker).begin() as session:
                # Without a short timeout a real block would hang the suite
                # instead of failing it.
                await session.execute(text("SET LOCAL lock_timeout = '1500ms'"))
                try:
                    await lock_active_admins(session, people.tenant)
                except Exception as exc:  # noqa: BLE001 - the block is the result
                    return type(exc).__name__
                return "acquired"

        held = asyncio.create_task(holder())
        outcome = await contender()
        release.set()
        await held

        assert outcome != "acquired", (
            "a second transaction read the admin count while the first still "
            "held it -- both could then demote the other and leave none"
        )


class TestSelfHarm:
    async def test_an_admin_cannot_disable_themselves(self, org_uow, people) -> None:
        """Refused even though a second admin exists, because it is never meant."""
        with pytest.raises(ConflictError):
            await management.set_user_active(
                org_uow,
                people.tenant,
                user_id=people.admin.user_id,
                active=False,
                actor=actor_for(people.admin),
            )

    async def test_an_admin_cannot_demote_themselves(self, org_uow, people) -> None:
        with pytest.raises(ConflictError):
            await management.update_user(
                org_uow,
                people.tenant,
                user_id=people.admin.user_id,
                changes={"role": Role.USER},
                actor=actor_for(people.admin),
            )


class TestScopeEscalation:
    async def test_a_user_cannot_be_moved_to_another_organizations_location(
        self, org_uow, people
    ) -> None:
        with pytest.raises(NotFoundError):
            await management.update_user(
                org_uow,
                people.tenant,
                user_id=people.member.user_id,
                changes={"location_id": uuid.uuid4()},
                actor=actor_for(people.admin),
            )

    async def test_a_pinned_admin_cannot_unpin_a_user_to_organization_wide(
        self, org_uow, people, seeded_org
    ) -> None:
        """The trap `narrowed_to` does not catch.

        `TenantContext.narrowed_to(None)` returns the context unchanged, so a
        check built on it would read "no location requested, nothing to verify"
        and let a branch-scoped admin hand someone organization-wide access.
        """
        pinned = management.Actor(
            label="branch-admin",
            user_id=people.admin.user_id,
            location_id=seeded_org["locations"]["alpha"],
        )
        with pytest.raises(TenantScopeError):
            await management.update_user(
                org_uow,
                people.tenant,
                user_id=people.member.user_id,
                changes={"location_id": None},
                actor=pinned,
            )

    async def test_a_pinned_admin_cannot_move_a_user_to_another_branch(
        self, org_uow, people, seeded_org
    ) -> None:
        pinned = management.Actor(
            label="branch-admin",
            user_id=people.admin.user_id,
            location_id=seeded_org["locations"]["alpha"],
        )
        with pytest.raises(TenantScopeError):
            await management.update_user(
                org_uow,
                people.tenant,
                user_id=people.member.user_id,
                changes={"location_id": seeded_org["locations"]["beta"]},
                actor=pinned,
            )


class TestSessionRevocation:
    async def test_disabling_a_user_stops_them_signing_in(self, org_uow, people, settings) -> None:
        auth = AuthService(settings)
        signed_in = await auth.login(people.member.email, PASSWORD)

        await management.set_user_active(
            org_uow,
            people.tenant,
            user_id=people.member.user_id,
            active=False,
            actor=actor_for(people.admin),
        )

        with pytest.raises(AuthenticationError):
            await auth.refresh(signed_in.tokens.refresh_token)
        with pytest.raises(AuthenticationError):
            await auth.login(people.member.email, PASSWORD)

    async def test_a_role_change_ends_the_users_sessions(self, org_uow, people, settings) -> None:
        """Promotion and demotion both change what the token means.

        Access tokens are not re-read from the database per request, so without
        this a demoted admin would keep administrator power until their token
        expired -- up to half an hour of privilege they no longer have.
        """
        auth = AuthService(settings)
        signed_in = await auth.login(people.member.email, PASSWORD)

        await management.update_user(
            org_uow,
            people.tenant,
            user_id=people.member.user_id,
            changes={"role": Role.ADMIN},
            actor=actor_for(people.admin),
        )

        with pytest.raises(AuthenticationError):
            await auth.refresh(signed_in.tokens.refresh_token)

    async def test_renaming_a_user_does_not_sign_them_out(self, org_uow, people, settings) -> None:
        """The other half of the rule: a cosmetic edit must not log anyone out."""
        auth = AuthService(settings)
        signed_in = await auth.login(people.member.email, PASSWORD)

        await management.update_user(
            org_uow,
            people.tenant,
            user_id=people.member.user_id,
            changes={"full_name": "Renamed Person"},
            actor=actor_for(people.admin),
        )

        refreshed = await auth.refresh(signed_in.tokens.refresh_token)
        assert refreshed.access_token


class TestPasswords:
    async def test_an_admin_reset_replaces_the_password_and_ends_sessions(
        self, org_uow, people, settings
    ) -> None:
        auth = AuthService(settings)
        signed_in = await auth.login(people.member.email, PASSWORD)

        new_password = "a-brand-new-password-456"
        await management.reset_password(
            org_uow,
            people.tenant,
            user_id=people.member.user_id,
            password=new_password,
            actor=actor_for(people.admin),
        )

        with pytest.raises(AuthenticationError):
            await auth.refresh(signed_in.tokens.refresh_token)
        with pytest.raises(AuthenticationError):
            await auth.login(people.member.email, PASSWORD)

        assert await auth.login(people.member.email, new_password)

    async def test_changing_your_own_password_needs_the_current_one(
        self, org_uow, people, settings
    ) -> None:
        auth = AuthService(settings)
        with pytest.raises(AuthenticationError):
            await auth.change_password(
                org_uow,
                people.member,
                current_password="not-the-right-password",
                new_password="some-new-password-789",
            )
        # Unchanged, so the old one still works.
        assert await auth.login(people.member.email, PASSWORD)

    async def test_changing_your_own_password_returns_usable_tokens(
        self, org_uow, people, settings
    ) -> None:
        """You must not be signed out by your own successful password change."""
        auth = AuthService(settings)
        new_password = "chosen-by-the-user-321"

        tokens = await auth.change_password(
            org_uow, people.member, current_password=PASSWORD, new_password=new_password
        )

        refreshed = await auth.refresh(tokens.refresh_token)
        assert refreshed.access_token
        assert await auth.login(people.member.email, new_password)


class TestUserCreation:
    async def test_a_duplicate_email_is_a_conflict_not_a_crash(
        self, org_uow, org_tenant, people, settings
    ) -> None:
        """The directory is global, so the clash is caught before the insert.

        Without the pre-check this surfaces as a primary-key violation from deep
        inside the transaction -- a 500 where the caller deserves a 409.
        """
        with pytest.raises(ConflictError):
            await AuthService(settings).create_user(
                org_uow,
                org_tenant,
                email=people.member.email,
                password="another-password-123",
                role=Role.USER,
            )

    async def test_scopes_follow_the_role(self, people) -> None:
        assert people.admin.scopes == scopes_for(Role.ADMIN)
        assert people.member.scopes == scopes_for(Role.USER)
