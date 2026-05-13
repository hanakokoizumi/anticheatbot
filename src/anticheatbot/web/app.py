from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web
from aiogram import Bot

from anticheatbot.config import Settings
from anticheatbot.web import admin_api, verify_api


def webapp_root() -> Path:
    """Resolve the ``webapp/`` static directory.

    When the package runs from ``site-packages`` (e.g. ``uv run`` in Docker), ``__file__`` is not under
    the repo, so we prefer ``WEBAPP_ROOT``, then ``<cwd>/webapp``, then walk parents of ``__file__`` for
    a sibling ``webapp`` that contains ``shared/``.
    """
    override = os.environ.get("WEBAPP_ROOT", "").strip()
    if override:
        p = Path(override).expanduser().resolve()
        if not p.is_dir():
            msg = f"WEBAPP_ROOT is not a directory: {p}"
            raise RuntimeError(msg)
        return p

    cwd = Path.cwd()
    cand = cwd / "webapp"
    if cand.is_dir() and (cand / "shared").is_dir():
        return cand.resolve()

    here = Path(__file__).resolve()
    for parent in here.parents:
        d = parent / "webapp"
        if d.is_dir() and (d / "shared").is_dir():
            return d.resolve()

    msg = (
        "Cannot find webapp/ (need webapp/shared/). "
        "Run from project root, set WEBAPP_ROOT, or copy webapp next to the working directory."
    )
    raise RuntimeError(msg)


def create_http_app(*, bot: Bot, settings: Settings) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app["settings"] = settings

    app.router.add_get("/api/verify/session", verify_api.verify_session)
    app.router.add_post("/api/verify/rules-complete", verify_api.verify_rules_complete)
    app.router.add_post("/api/verify/quiz-submit", verify_api.verify_quiz_submit)

    app.router.add_get("/api/admin/session", admin_api.admin_session)
    app.router.add_get("/api/admin/chats", admin_api.admin_chats)
    app.router.add_get("/api/admin/settings", admin_api.admin_settings_get)
    app.router.add_patch("/api/admin/settings", admin_api.admin_settings_patch)
    app.router.add_get("/api/admin/stats", admin_api.admin_stats)
    app.router.add_post("/api/admin/translation-cache/clear", admin_api.admin_translation_cache_clear)
    app.router.add_get("/api/admin/quiz", admin_api.admin_quiz_list)
    app.router.add_post("/api/admin/quiz", admin_api.admin_quiz_upsert)
    app.router.add_delete("/api/admin/quiz", admin_api.admin_quiz_delete)

    app.router.add_get("/healthz", lambda r: web.json_response({"ok": True}))

    root = webapp_root()
    app.router.add_static("/shared/", root / "shared", follow_symlinks=True, show_index=False)
    app.router.add_static("/verify/", root / "verify", follow_symlinks=True, show_index=False)
    app.router.add_static("/admin/", root / "admin", follow_symlinks=True, show_index=False)

    return app
