from __future__ import annotations

from pathlib import Path
import asyncio
from unittest.mock import MagicMock

import pytest

from anticheatbot.web import app as app_module
from anticheatbot.config import Settings
from aiohttp.test_utils import TestClient, TestServer


def test_webapp_root_uses_cwd_webapp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WEBAPP_ROOT", raising=False)
    (tmp_path / "webapp" / "shared").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    assert app_module.webapp_root() == (tmp_path / "webapp").resolve()


def test_webapp_root_webapp_root_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "static_webapp"
    (root / "shared").mkdir(parents=True)
    monkeypatch.setenv("WEBAPP_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    assert app_module.webapp_root() == root.resolve()
    monkeypatch.delenv("WEBAPP_ROOT", raising=False)


def test_webapp_root_env_not_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    missing = tmp_path / "nope"
    monkeypatch.setenv("WEBAPP_ROOT", str(missing))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="WEBAPP_ROOT"):
        app_module.webapp_root()
    monkeypatch.delenv("WEBAPP_ROOT", raising=False)


def test_sensitive_error_responses_are_not_cacheable() -> None:
    async def run():
        settings = Settings(BOT_TOKEN="1:x", WEBAPP_PUBLIC_URL="https://example.com")
        app = app_module.create_http_app(bot=MagicMock(), settings=settings)
        async with TestClient(TestServer(app)) as client:
            response = await client.get("/api/admin/session")
            assert response.status == 400
            assert "no-store" in response.headers["Cache-Control"]
            assert response.headers["Referrer-Policy"] == "no-referrer"
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            html = await client.get("/verify/index.html")
            assert html.status == 200
            assert html.headers["Referrer-Policy"] == "no-referrer"
    asyncio.run(run())
