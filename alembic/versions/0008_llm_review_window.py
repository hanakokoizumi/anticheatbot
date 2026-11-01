"""Persist the messages eligible for edit moderation within each membership."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_llm_review_window"
down_revision = "0007_member_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "user_chat_states",
        sa.Column("llm_review_state", sa.JSON(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("user_chat_states", "llm_review_state")
