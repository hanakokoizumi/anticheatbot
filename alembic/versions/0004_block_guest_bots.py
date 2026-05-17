"""group_settings: block guest bot spam messages

Revision ID: 0004_block_guest_bots
Revises: 0003_group_join_post_verify
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_block_guest_bots"
down_revision = "0003_group_join_post_verify"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_settings",
        sa.Column("block_guest_bots", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("group_settings", "block_guest_bots")
