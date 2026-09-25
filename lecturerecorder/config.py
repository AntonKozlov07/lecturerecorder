"""Paths and user settings.

Everything the app stores (database, audio, settings, browser profile) lives in
one data folder so it is easy to back up or delete.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path


def data_dir() -> Path:
    override = os.environ.get("LECTURERECORDER_HOME")
    if override:
        path = Path(override)
    elif sys.platform == "win32":
        path = Path(os.environ.get("APPDATA", Path.home())) / "LectureRecorder"
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "LectureRecorder"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        path = Path(base) / "lecturerecorder"
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA_DIR = data_dir()
AUDIO_DIR = DATA_DIR / "audio"
AUDIO_DIR.mkdir(exist_ok=True)
MATERIALS_DIR = DATA_DIR / "materials"
MATERIALS_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "library.db"
SETTINGS_PATH = DATA_DIR / "settings.json"

MODELS = {
    "claude-opus-5": "Claude Opus 5 (best quality)",
    "claude-sonnet-5": "Claude Sonnet 5 (faster, cheaper)",
    "claude-haiku-4-5": "Claude Haiku 4.5 (fastest, cheapest)",
}
WHISPER_MODELS = ["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"]

DEFAULTS = {
    "api_key": "",
    "model": "claude-opus-5",
    "whisper_model": "small",
    "whisper_device": "auto",
    "language": "",  # empty = auto-detect
    "segment_seconds": 30,
    "auto_notes": True,
    "phone_enabled": False,
    "filter_side_talk": True,
}

_lock = threading.Lock()


def load_settings() -> dict:
    with _lock:
        settings = dict(DEFAULTS)
        if SETTINGS_PATH.exists():
            try:
                settings.update(json.loads(SETTINGS_PATH.read_text("utf-8")))
            except (OSError, ValueError):
                pass
        return settings


def save_settings(changes: dict) -> dict:
    current = load_settings()
    for key, value in changes.items():
        if key in DEFAULTS:
            current[key] = value
    with _lock:
        tmp = SETTINGS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(current, indent=2), "utf-8")
        tmp.replace(SETTINGS_PATH)
        try:
            os.chmod(SETTINGS_PATH, 0o600)
        except OSError:
            pass
    return current


def public_settings() -> dict:
    """Settings safe to send to the UI (the API key is never returned)."""
    s = load_settings()
    key = s.pop("api_key", "") or ""
    s["has_api_key"] = bool(key) or bool(os.environ.get("ANTHROPIC_API_KEY"))
    s["api_key_hint"] = key[-4:] if key else ("from environment" if os.environ.get("ANTHROPIC_API_KEY") else "")
    s["models"] = MODELS
    s["whisper_models"] = WHISPER_MODELS
    s["data_dir"] = str(DATA_DIR)
    return s
