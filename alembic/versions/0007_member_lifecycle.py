"""Persist Telegram membership lifecycle for duplicate and delayed join updates."""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0007_member_lifecycle"
down_revision = "0006_quiz_shuffle_and_min_correct"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user_chat_states", sa.Column("membership_status", sa.String(16), nullable=True))
    op.add_column("user_chat_states", sa.Column("membership_update_id", sa.BigInteger(), nullable=True))
    op.add_column("user_chat_states", sa.Column("membership_event_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_chat_states", sa.Column("membership_join_sources", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_chat_states", "membership_join_sources")
    op.drop_column("user_chat_states", "membership_event_at")
    op.drop_column("user_chat_states", "membership_update_id")
    op.drop_column("user_chat_states", "membership_status")
