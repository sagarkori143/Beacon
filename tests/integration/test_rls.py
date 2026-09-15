"""Row Level Security, verified against the live database.

These tests enumerate the schema rather than spot-checking it. A table added
later without a policy fails here instead of leaking quietly, which is the only
way this kind of guarantee survives contact with a growing codebase.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.db import ORG_GUC
from app.models import GLOBAL_TABLES, TENANT_TABLES

pytestmark = [pytest.mark.integration]


@pytest.fixture
def sessionmaker_for(db_engine):  # type: ignore[no-untyped-def]
    return async_sessionmaker(db_engine, expire_on_commit=False, autoflush=False)


class TestRoleConfiguration:
    async def test_the_application_role_cannot_bypass_rls(self, sessionmaker_for) -> None:
        """The single most important database-level fact in the system.

        A superuser, or any role with BYPASSRLS, ignores every policy silently.
        Connecting the application as one would make all the isolation below
        decorative -- and nothing would report an error.
        """
        async with sessionmaker_for() as session:
            row = (
                await session.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
                )
            ).one()

        is_superuser, bypasses_rls = row
        assert not is_superuser, "the application must not connect as a superuser"
        assert not bypasses_rls, "the application role must not have BYPASSRLS"


class TestPolicyCoverage:
    async def test_every_tenant_table_has_rls_enabled_and_forced(self, sessionmaker_for) -> None:
        """FORCE is as important as ENABLE.

        Without it a table's owner is exempt from its own policies, so a
        deployment that connects as the owner sees everything with no error.
        """
        async with sessionmaker_for() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT relname, relrowsecurity, relforcerowsecurity"
                        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                        " WHERE n.nspname = 'public' AND c.relkind = 'r'"
                    )
                )
            ).all()

        state = {name: (enabled, forced) for name, enabled, forced in rows}
        missing = [table for table in TENANT_TABLES if state.get(table) != (True, True)]
        assert not missing, f"tenant tables without RLS enabled+forced: {missing}"

    async def test_every_tenant_table_has_exactly_one_policy(self, sessionmaker_for) -> None:
        """One permissive policy per table.

        Two permissive policies OR together, so adding a second is a quiet way
        to widen access.
        """
        async with sessionmaker_for() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT tablename, count(*) FROM pg_policies"
                        " WHERE schemaname = 'public' GROUP BY tablename"
                    )
                )
            ).all()

        counts = dict(rows)
        for table in TENANT_TABLES:
            assert counts.get(table) == 1, f"{table} has {counts.get(table)} policies"

    async def test_global_tables_are_documented_exceptions(self, sessionmaker_for) -> None:
        """Nothing is outside tenant scoping by accident."""
        async with sessionmaker_for() as session:
            tables = {
                row[0]
                for row in (
                    await session.execute(
                        text(
                            "SELECT c.relname FROM pg_class c"
                            " JOIN pg_namespace n ON n.oid = c.relnamespace"
                            " WHERE n.nspname='public' AND c.relkind='r'"
                            " AND NOT c.relrowsecurity"
                        )
                    )
                ).all()
            }
        undocumented = tables - set(GLOBAL_TABLES) - {"alembic_version"}
        assert not undocumented, (
            f"tables without RLS and without a documented reason: {undocumented}"
        )


class TestTenantIsolation:
    async def test_a_session_cannot_read_another_organizations_rows(
        self, sessionmaker_for, seeded_org: dict
    ) -> None:
        """The headline guarantee: Hotel A cannot see Hotel B.

        Scoped to a foreign organization, a deliberately unfiltered query over
        the seeded organization's locations must return nothing.
        """
        other_org = uuid.uuid4()

        async with sessionmaker_for() as session, session.begin():
            await session.execute(
                text(f"SELECT set_config('{ORG_GUC}', :org, true)"),
                {"org": str(other_org)},
            )
            visible = (await session.execute(text("SELECT count(*) FROM locations"))).scalar_one()

        assert visible == 0

    async def test_the_owning_session_does_see_its_rows(
        self, sessionmaker_for, seeded_org: dict
    ) -> None:
        """The counterpart: isolation that hides everything is not isolation."""
        async with sessionmaker_for() as session, session.begin():
            await session.execute(
                text(f"SELECT set_config('{ORG_GUC}', :org, true)"),
                {"org": str(seeded_org["organization_id"])},
            )
            visible = (await session.execute(text("SELECT count(*) FROM locations"))).scalar_one()

        assert visible == 2

    async def test_missing_tenant_context_denies_everything(
        self, sessionmaker_for, seeded_org: dict
    ) -> None:
        """Default-deny.

        An empty setting must deny rather than raise a cast error -- an error
        here would tempt someone to 'fix' it by loosening the policy.
        """
        async with sessionmaker_for() as session, session.begin():
            await session.execute(text(f"SELECT set_config('{ORG_GUC}', '', true)"))
            visible = (await session.execute(text("SELECT count(*) FROM locations"))).scalar_one()

        assert visible == 0

    async def test_writes_for_another_tenant_are_rejected(
        self, sessionmaker_for, seeded_org: dict
    ) -> None:
        """WITH CHECK stops a forged organization_id on insert.

        Reading is not the only direction that matters: without this, a bug that
        wrote the wrong organization id would plant a row in someone else's
        tenant.
        """
        async with sessionmaker_for() as session, session.begin():
            await session.execute(
                text(f"SELECT set_config('{ORG_GUC}', :org, true)"),
                {"org": str(seeded_org["organization_id"])},
            )
            with pytest.raises(Exception, match="row-level security|violates"):
                await session.execute(
                    text(
                        "INSERT INTO locations (id, organization_id, name, slug,"
                        " timezone, is_active, settings, created_at, updated_at)"
                        " VALUES (gen_random_uuid(), :other, 'Forged', 'forged',"
                        " 'UTC', true, '{}'::jsonb, now(), now())"
                    ),
                    {"other": str(uuid.uuid4())},
                )


class TestTransactionScoping:
    async def test_tenant_context_does_not_survive_the_transaction(
        self, sessionmaker_for, seeded_org: dict
    ) -> None:
        """`SET LOCAL` semantics, which the whole design depends on.

        If the setting outlived its transaction it would still be there when the
        connection was reused for a different tenant -- the exact cross-tenant
        read this architecture is built to make impossible.
        """
        org = str(seeded_org["organization_id"])

        async with sessionmaker_for() as session:
            async with session.begin():
                await session.execute(
                    text(f"SELECT set_config('{ORG_GUC}', :org, true)"), {"org": org}
                )
                inside = (
                    await session.execute(text(f"SELECT current_setting('{ORG_GUC}', true)"))
                ).scalar_one()

            async with session.begin():
                after = (
                    await session.execute(text(f"SELECT current_setting('{ORG_GUC}', true)"))
                ).scalar_one()

        assert inside == org
        assert after in (None, ""), "tenant context leaked past its transaction"
