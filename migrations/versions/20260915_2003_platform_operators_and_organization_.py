"""Platform operators, and organization visibility for them.

Adds ``platform_users`` -- accounts that operate the deployment and belong to no
organization -- and widens the ``organizations`` policy so such a session can
enumerate and create tenants.

The widening is deliberately narrow. It applies to ``organizations`` and nothing
else: every tenant table still keys on ``app.current_org_id``, so a platform
operator must name the organization they are acting on, and no transaction can
span two of them. See docs/tenant-isolation.md.

Revision ID: 7a2f5c91b4e3
Revises: d189249c1e61
Create Date: 2026-09-15 20:03:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a2f5c91b4e3"
down_revision: str | None = "d189249c1e61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: An organization is visible to a session scoped to it, or to a platform
#: operator's session. `nullif(..., '')` keeps an unset value denying rather than
#: raising on the uuid cast.
_ORG_VISIBLE = (
    "id = nullif(current_setting('app.current_org_id', true), '')::uuid"
    " OR current_setting('app.platform_admin', true) = 'on'"
)


def upgrade() -> None:
    op.create_table(
        "platform_users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_users")),
        sa.UniqueConstraint("email", name=op.f("uq_platform_users_email")),
    )
    op.create_index(op.f("ix_platform_users_email"), "platform_users", ["email"], unique=True)

    # Replace the organizations policy with one that also admits a platform
    # operator. WITH CHECK is added so an operator can create a tenant; without
    # it the INSERT would be checked against the USING clause and rejected,
    # since the new row's id is not the session's current organization.
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON organizations")
    op.execute(
        f"""
        CREATE POLICY tenant_isolation ON organizations
            USING ({_ORG_VISIBLE})
            WITH CHECK ({_ORG_VISIBLE})
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE ON platform_users TO app_rw;
            END IF;
        END
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_isolation ON organizations")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON organizations
            USING (id = nullif(current_setting('app.current_org_id', true), '')::uuid)
        """
    )
    op.drop_index(op.f("ix_platform_users_email"), table_name="platform_users")
    op.drop_table("platform_users")
