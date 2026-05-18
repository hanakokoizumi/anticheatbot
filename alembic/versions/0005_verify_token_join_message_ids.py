"""verify_tokens: optional new_chat_members service message ids for timeout cleanup

Revision ID: 0005_verify_token_join_message_ids
Revises: 0004_block_guest_bots
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_verify_token_join_message_ids"
down_revision = "0004_block_guest_bots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verify_tokens",
        sa.Column("join_message_chat_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "verify_tokens",
        sa.Column("join_message_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("verify_tokens", "join_message_id")
    op.drop_column("verify_tokens", "join_message_chat_id")
