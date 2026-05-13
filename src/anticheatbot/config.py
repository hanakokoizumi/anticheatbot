from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = Field(..., alias="BOT_TOKEN")

    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/bot.db",
        alias="DATABASE_URL",
    )

    webapp_public_url: str = Field(..., alias="WEBAPP_PUBLIC_URL")
    http_host: str = Field(default="0.0.0.0", alias="HTTP_HOST")
    http_port: int = Field(default=8080, alias="HTTP_PORT")

    init_data_max_age_seconds: int = Field(default=86400, alias="INIT_DATA_MAX_AGE_SECONDS")

    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str = Field(default="https://api.openai.com/v1", alias="OPENAI_BASE_URL")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    translate_model: str | None = Field(default=None, alias="TRANSLATE_MODEL")
    moderation_model: str | None = Field(default=None, alias="MODERATION_MODEL")

    translation_prompt_version: int = Field(default=1, alias="TRANSLATION_PROMPT_VERSION")

    turnstile_site_key: str | None = Field(default=None, alias="TURNSTILE_SITE_KEY")
    turnstile_secret_key: str | None = Field(default=None, alias="TURNSTILE_SECRET_KEY")

    global_admin_user_ids: str = Field(default="", alias="GLOBAL_ADMIN_USER_IDS")
    admin_user_ids: str = Field(default="", alias="ADMIN_USER_IDS")

    @field_validator("webapp_public_url")
    @classmethod
    def strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @staticmethod
    def _parse_id_list(raw: str) -> set[int]:
        out: set[int] = set()
        for part in (raw or "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                out.add(int(part))
            except ValueError:
                continue
        return out

    @property
    def global_admin_id_set(self) -> set[int]:
        """全局管理员：环境变量 GLOBAL_ADMIN_USER_IDS 与（兼容）ADMIN_USER_IDS 合并，拥有最高管理权限。"""
        return self._parse_id_list(self.global_admin_user_ids) | self._parse_id_list(self.admin_user_ids)

    @property
    def admin_id_set(self) -> set[int]:
        """兼容旧代码：等同于 global_admin_id_set。"""
        return self.global_admin_id_set

    @property
    def effective_translate_model(self) -> str:
        return self.translate_model or self.openai_model

    @property
    def effective_moderation_model(self) -> str:
        return self.moderation_model or self.openai_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
