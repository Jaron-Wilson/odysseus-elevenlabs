"""Where the add-on keeps things, all in its data folder (MOD_DATA_DIR):

    secrets.json   the API key (0600; never sent to a browser or logged)
    usage.json     the local tally of characters and credits sent
    cache/         audio already made, so a repeated phrase costs nothing

Its settings (model, credit reserve, max per reply) are the mod settings the
core keeps for it (GET/PUT /api/mods/v1/settings), or a local file when it
runs on its own.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any, Dict, Optional

_lock = threading.Lock()

DEFAULTS: Dict[str, Any] = {
    "model": "eleven_flash_v2_5",
    # Below this share of the plan (or this many credits) ElevenLabs pauses
    # and the core's Kokoro (or the browser voice) speaks.
    "min_credits_pct": 5,
    "min_credits": 0,
    # Most characters one reply may send to ElevenLabs (0 = no cap).
    "max_reply_chars": 1500,
}
RANGES = {"min_credits_pct": (0, 100), "min_credits": (0, 10_000_000), "max_reply_chars": (0, 5000)}


class TTSError(Exception):
    """A problem a person can act on, with the HTTP status to answer."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.message = message
        self.status = status


def data_dir() -> str:
    d = os.environ.get("MOD_DATA_DIR") or os.path.join(os.getcwd(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def _secrets_file() -> str:
    return os.path.join(data_dir(), "secrets.json")


def _load_secrets() -> Dict[str, Any]:
    try:
        with open(_secrets_file(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def secret(name: str) -> str:
    v = _load_secrets().get(name)
    return v if isinstance(v, str) else ""


def save_secret(name: str, value: Optional[str]) -> None:
    """Set (or, with None, remove) one secret. The file is 0600 from the start."""
    with _lock:
        d = _load_secrets()
        if value:
            d[name] = value
        else:
            d.pop(name, None)
        path = _secrets_file()
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(d, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def clean_settings(raw: Any) -> Dict[str, Any]:
    """The settings with defaults filled in and numbers clamped."""
    raw = raw if isinstance(raw, dict) else {}
    out = dict(DEFAULTS)
    model = raw.get("model")
    if isinstance(model, str) and model.strip():
        out["model"] = model.strip()[:80]
    for k, (lo, hi) in RANGES.items():
        try:
            v = float(raw.get(k, out[k]))
        except (TypeError, ValueError):
            continue
        v = max(lo, min(hi, v))
        out[k] = int(v) if float(v).is_integer() else v
    return out
