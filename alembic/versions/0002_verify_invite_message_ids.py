"""verify_tokens: store invite message for deletion after success

Revision ID: 0002_verify_invite_message_ids
Revises: 0001_initial
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_verify_invite_message_ids"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("verify_tokens", sa.Column("invite_message_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("verify_tokens", sa.Column("invite_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("verify_tokens", "invite_message_id")
    op.drop_column("verify_tokens", "invite_message_chat_id")
