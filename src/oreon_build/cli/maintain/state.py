from __future__ import annotations

from typing import Any

from oreon_build.cli.orbscli import CONFIG_DIR, CONFIG_PATH, _load_json, _save_json

STATE_PATH = CONFIG_DIR / "maintain.json"


def load_state() -> dict[str, Any]:
    return _load_json(STATE_PATH)


def save_state(data: dict[str, Any]) -> None:
    _save_json(STATE_PATH, data)


def clear_state() -> None:
    if STATE_PATH.is_file():
        STATE_PATH.unlink()


def default_repo_base() -> str:
    cfg = _load_json(CONFIG_PATH)
    return str(cfg.get("repo_base") or "https://repo-us.oreonhq.com").rstrip("/")
