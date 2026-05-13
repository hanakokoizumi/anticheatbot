from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON, TypeDecorator


class Base(DeclarativeBase):
    pass


class JSONCompat(TypeDecorator[dict[str, Any] | list[Any]]):
    """JSON on Postgres/SQLite; use Text+json on SQLite if needed — JSON type works on aiosqlite."""

    impl = JSON
    cache_ok = True


class GroupSettings(Base):
    __tablename__ = "group_settings"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    verification_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    verify_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="rules_ack")
    verify_timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=180)
    kick_on_verify_timeout: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    turnstile_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    rules_markdown: Mapped[str] = mapped_column(Text, nullable=False, default="")

    canonical_locale: Mapped[str] = mapped_column(String(32), nullable=False, default="zh-Hans")
    llm_translation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    translation_allowed_locales: Mapped[str | None] = mapped_column(Text, nullable=True)

    quiz_pass_score_threshold: Mapped[int] = mapped_column(Integer, nullable=False, default=80)
    quiz_draw_count: Mapped[int] = mapped_column(Integer, nullable=False, default=10)

    llm_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    llm_max_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    llm_min_confidence_action: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    spam_action: Mapped[str] = mapped_column(String(32), nullable=False, default="delete")

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class KnownChat(Base):
    __tablename__ = "known_chats"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VerifyToken(Base):
    __tablename__ = "verify_tokens"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("known_chats.chat_id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class UserChatState(Base):
    __tablename__ = "user_chat_states"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    llm_messages_seen: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class QuizQuestion(Base):
    __tablename__ = "quiz_questions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("known_chats.chat_id"), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    choices_json: Mapped[list[str]] = mapped_column(JSONCompat, nullable=False)
    correct_index: Mapped[int] = mapped_column(Integer, nullable=False)
    points: Mapped[int] = mapped_column(Integer, nullable=False)


class VerifyQuizSession(Base):
    __tablename__ = "verify_quiz_sessions"

    token: Mapped[str] = mapped_column(String(64), ForeignKey("verify_tokens.token"), primary_key=True)
    question_ids_json: Mapped[list[int]] = mapped_column(JSONCompat, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONCompat, nullable=True)


class LLMTranslationCache(Base):
    __tablename__ = "llm_translation_cache"
    __table_args__ = (
        UniqueConstraint(
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

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_locale: Mapped[str] = mapped_column(String(32), nullable=False)
    target_locale: Mapped[str] = mapped_column(String(32), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[int] = mapped_column(Integer, nullable=False)
    translated_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
