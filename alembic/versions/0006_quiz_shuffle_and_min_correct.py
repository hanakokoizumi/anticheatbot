"""verify_quiz_sessions: shuffle_maps_json for per-option randomization in verify UI

Revision ID: 0006_quiz_shuffle_and_min_correct
Revises: 0005_verify_token_join_message_ids
Create Date: 2026-05-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_quiz_shuffle_and_min_correct"
down_revision = "0005_verify_token_join_message_ids"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "verify_quiz_sessions",
        sa.Column(
            "shuffle_maps_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("verify_quiz_sessions", "shuffle_maps_json")
