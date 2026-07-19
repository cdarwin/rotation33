"""baseline

Revision ID: 33b4a35f9a67
Revises:
Create Date: 2026-07-19 15:37:20.825488

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "33b4a35f9a67"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
