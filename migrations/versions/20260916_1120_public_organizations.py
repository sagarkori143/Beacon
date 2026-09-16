"""Mark which organizations answer questions from the public site.

Visitors do not sign in. The landing page lists organizations and anyone may ask
one of them a question, so an organization's active knowledge becomes readable
by anyone who can reach the site.

The default is ``true``, which is the product decision: every tenant is publicly
askable. This exists as a column rather than as an implicit "all of them" so a
single organization can be withdrawn later without a migration and without
touching any code.

Revision ID: 3c5e81a4d762
Revises: 7a2f5c91b4e3
Create Date: 2026-09-16 11:20:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3c5e81a4d762"
down_revision: str | None = "7a2f5c91b4e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "is_public",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    # Every listing filters on this, and it is the only predicate on a table
    # read once per landing-page view.
    op.create_index("ix_organizations_is_public", "organizations", ["is_public"])

    # The public site reads this table with no tenant context at all, and the
    # existing policy admits only "your own organization" or a platform
    # operator -- so without this the landing page would come back empty, with
    # no error to explain why.
    #
    # Deliberately FOR SELECT only. Permissive policies OR together, so adding
    # `is_public` to the existing FOR ALL policy would also let any session
    # UPDATE or DELETE another organization's row; app_rw holds both grants.
    # Reads widen, writes do not.
    op.execute(
        """
        CREATE POLICY public_read ON organizations
            FOR SELECT
            USING (is_public AND is_active)
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS public_read ON organizations")
    op.drop_index("ix_organizations_is_public", table_name="organizations")
    op.drop_column("organizations", "is_public")
