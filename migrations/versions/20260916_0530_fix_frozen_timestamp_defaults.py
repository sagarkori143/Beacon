"""Give audit_log and ingestion_job_events a real created_at default.

Both columns were declared ``server_default="now()"`` -- a Python *string*
rather than ``func.now()``. SQLAlchemy renders a string server default as a
quoted SQL literal, so the DDL said::

    created_at timestamptz NOT NULL DEFAULT 'now()'

PostgreSQL coerces that literal to a timestamp **once, when the column is
created**, and stores the result as a constant. Every row inserted afterwards
therefore received the same value: the moment the migration ran.

Nothing failed. No error was raised. The audit log simply recorded every event
as having happened at the same instant, and the ingestion history could not be
ordered by time -- which is most of what an audit log and a stage timeline are
for.

Existing rows cannot be repaired: the real times were never written anywhere.
They keep the frozen value, which at least makes the affected range obvious.

Revision ID: 9b1f4d07ac25
Revises: 3c5e81a4d762
Create Date: 2026-09-16 05:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "9b1f4d07ac25"
down_revision: str | None = "3c5e81a4d762"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("audit_log", "ingestion_job_events")


def upgrade() -> None:
    for table in _TABLES:
        # Unquoted now(), so it is evaluated per row rather than once per DDL.
        op.execute(f"ALTER TABLE {table} ALTER COLUMN created_at SET DEFAULT now()")


def downgrade() -> None:
    # Restoring the broken default would re-freeze it at *this* moment, which is
    # not the original value and not useful. Drop it instead: the models supply
    # the column and the application always sets it going forward.
    for table in _TABLES:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN created_at DROP DEFAULT")
