"""empty recommendation batches

An empty generate used to write no rows, so the next read of "the latest batch"
found the previous one and resurrected picks the user had already rejected,
losing the FR-10 explanation with them. An empty batch is now one marker row:
`release_id` null, `empty_reason` set.

Revision ID: 9c1f4a7b2e05
Revises: 306081445359
Create Date: 2026-07-25 14:10:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9c1f4a7b2e05"
down_revision: str | None = "306081445359"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # batch_alter_table because SQLite cannot relax a NOT NULL in place; Alembic
    # recreates the table and copies the rows.
    with op.batch_alter_table("recommendation", schema=None) as batch_op:
        batch_op.add_column(sa.Column("empty_reason", sa.String(), nullable=True))
        batch_op.alter_column("release_id", existing_type=sa.String(), nullable=True)


def downgrade() -> None:
    # Marker rows have no release, so they cannot survive the column going back
    # to NOT NULL. Dropping them restores the old behaviour exactly.
    op.execute(sa.text("DELETE FROM recommendation WHERE release_id IS NULL"))
    with op.batch_alter_table("recommendation", schema=None) as batch_op:
        batch_op.alter_column("release_id", existing_type=sa.String(), nullable=False)
        batch_op.drop_column("empty_reason")
