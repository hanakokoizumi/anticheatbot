"""group_settings: delete join message + post-verify welcome

Revision ID: 0003_group_join_post_verify
Revises: 0002_verify_invite_message_ids
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_group_join_post_verify"
down_revision = "0002_verify_invite_message_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "group_settings",
        sa.Column("delete_join_message", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "group_settings",
        sa.Column(
            "post_verify_behavior",
            sa.String(length=32),
            nullable=False,
            server_default="delete_only",
        ),
    )
    op.add_column(
        "group_settings",
        sa.Column("welcome_message_markdown", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("group_settings", "welcome_message_markdown")
    op.drop_column("group_settings", "post_verify_behavior")
    op.drop_column("group_settings", "delete_join_message")
