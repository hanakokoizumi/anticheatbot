from __future__ import annotations

from pathlib import Path

import pytest

from anticheatbot.web import app as app_module


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
