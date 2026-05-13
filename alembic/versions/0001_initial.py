"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-13
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "group_settings",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("verification_enabled", sa.Boolean(), nullable=False),
        sa.Column("verify_mode", sa.String(length=32), nullable=False),
        sa.Column("verify_timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("kick_on_verify_timeout", sa.Boolean(), nullable=False),
        sa.Column("turnstile_enabled", sa.Boolean(), nullable=False),
        sa.Column("rules_markdown", sa.Text(), nullable=False),
        sa.Column("canonical_locale", sa.String(length=32), nullable=False),
        sa.Column("llm_translation_enabled", sa.Boolean(), nullable=False),
        sa.Column("translation_allowed_locales", sa.Text(), nullable=True),
        sa.Column("quiz_pass_score_threshold", sa.Integer(), nullable=False),
        sa.Column("quiz_draw_count", sa.Integer(), nullable=False),
        sa.Column("llm_enabled", sa.Boolean(), nullable=False),
        sa.Column("llm_max_messages", sa.Integer(), nullable=False),
        sa.Column("llm_min_confidence_action", sa.Float(), nullable=False),
        sa.Column("spam_action", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_table(
        "known_chats",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_table(
        "verify_tokens",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["chat_id"], ["known_chats.chat_id"]),
        sa.PrimaryKeyConstraint("token"),
    )
    op.create_index("ix_verify_tokens_chat_id", "verify_tokens", ["chat_id"], unique=False)
    op.create_index("ix_verify_tokens_user_id", "verify_tokens", ["user_id"], unique=False)

    op.create_table(
        "user_chat_states",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("verified", sa.Boolean(), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("llm_messages_seen", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id", "user_id"),
    )

    op.create_table(
        "quiz_questions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("choices_json", sa.JSON(), nullable=False),
        sa.Column("correct_index", sa.Integer(), nullable=False),
        sa.Column("points", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["known_chats.chat_id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_quiz_questions_chat_id", "quiz_questions", ["chat_id"], unique=False)

    op.create_table(
        "verify_quiz_sessions",
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("question_ids_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["token"], ["verify_tokens.token"]),
        sa.PrimaryKeyConstraint("token"),
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_ts", "audit_events", ["ts"], unique=False)
    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"], unique=False)
    op.create_index("ix_audit_events_chat_id", "audit_events", ["chat_id"], unique=False)
    op.create_index("ix_audit_events_user_id", "audit_events", ["user_id"], unique=False)

    op.create_table(
        "llm_translation_cache",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("namespace", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("source_locale", sa.String(length=32), nullable=False),
        sa.Column("target_locale", sa.String(length=32), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("translated_text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "namespace",
            "chat_id",
            "entity_id",
            "source_locale",
            "target_locale",
            "source_sha256",
            "prompt_version",
            name="uq_llm_translation_cache_key",
        ),
    )
    op.create_index("ix_llm_translation_cache_chat_id", "llm_translation_cache", ["chat_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_llm_translation_cache_chat_id", table_name="llm_translation_cache")
    op.drop_table("llm_translation_cache")
    op.drop_index("ix_audit_events_user_id", table_name="audit_events")
    op.drop_index("ix_audit_events_chat_id", table_name="audit_events")
    op.drop_index("ix_audit_events_event_type", table_name="audit_events")
    op.drop_index("ix_audit_events_ts", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("verify_quiz_sessions")
    op.drop_index("ix_quiz_questions_chat_id", table_name="quiz_questions")
    op.drop_table("quiz_questions")
    op.drop_table("user_chat_states")
    op.drop_index("ix_verify_tokens_user_id", table_name="verify_tokens")
    op.drop_index("ix_verify_tokens_chat_id", table_name="verify_tokens")
    op.drop_table("verify_tokens")
    op.drop_table("known_chats")
    op.drop_table("group_settings")
