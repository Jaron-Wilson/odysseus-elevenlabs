"""The voice the core calls (capability tts.voice): the credit guard, the
per-reply cap, the audio cache and the account status.

The core decides what speaks instead when this voice is paused (its own
Kokoro when that is ready, else the browser voice); this only says that it
is paused and why.
"""
from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional

from elevenlabs_mod import client as el
from elevenlabs_mod import store
from elevenlabs_mod.store import TTSError

logger = logging.getLogger(__name__)

MAX_TEXT = 5000
CACHE_MAX_BYTES = int(os.environ.get("ELEVENLABS_CACHE_MAX_BYTES") or 200 * 1024 * 1024)
QUOTA_PAUSE_S = 600


class Paused(TTSError):
    """ElevenLabs may not be used right now (credit reserve or quota)."""

    def __init__(self, reason: str):
        super().__init__(reason, 409)


def _num(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return v if v >= 0 else default


def cap_text(text: str, limit: int) -> str:
    """At most `limit` characters, cut at the last sentence (or word) end."""
    if not limit or len(text) <= limit:
        return text
    cut = text[:limit]
    for mark in (". ", "! ", "? ", "\n"):
        i = cut.rfind(mark)
        if i > limit * 0.5:
            return cut[: i + 1].strip()
    i = cut.rfind(" ")
    return (cut[:i] if i > limit * 0.5 else cut).strip()


class Voice:
    def __init__(self, settings_getter: Callable[[], Dict[str, Any]], client: Optional[el.ElevenLabsClient] = None,
                 cache_dir: Optional[str] = None):
        self._settings_getter = settings_getter
        self.client = client or el.ElevenLabsClient()
        self.cache_dir = cache_dir or os.path.join(store.data_dir(), "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._quota_hit: Optional[Dict[str, Any]] = None
        self.last_pause: Optional[Dict[str, Any]] = None
        self.last: Optional[Dict[str, Any]] = None

    def settings(self) -> Dict[str, Any]:
        try:
            raw = self._settings_getter() or {}
        except Exception as e:
            logger.warning("Could not read the settings: %s", type(e).__name__)
            raw = {}
        return store.clean_settings(raw)

    # ── The credit guard ──

    def guard(self, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Whether ElevenLabs may be used now. Trips when the remaining credits
        are below the reserve (a percent of the plan and/or a fixed number),
        or for ten minutes after ElevenLabs said the quota is used up."""
        s = settings or self.settings()
        pct = _num(s.get("min_credits_pct"), 5.0)
        floor = _num(s.get("min_credits"), 0.0)
        credits = self.client.credits() if self.client.key() else {"known": False, "error": ""}
        tripped, reason = False, ""
        if credits.get("known"):
            remaining, limit = credits["remaining"], credits["limit"]
            if limit and remaining * 100.0 / limit < pct:
                tripped, reason = True, f"ElevenLabs credits are below {pct:g}% ({remaining:,} of {limit:,} left)"
            elif floor and remaining < floor:
                tripped, reason = True, f"ElevenLabs credits are below {floor:,.0f} ({remaining:,} left)"
        quota = self._quota_hit
        if not tripped and quota and time.time() - quota["at"] < QUOTA_PAUSE_S:
            tripped, reason = True, quota["reason"]
        return {"tripped": tripped, "reason": reason, "min_credits_pct": pct, "min_credits": floor,
                "max_reply_chars": int(_num(s.get("max_reply_chars"), 1500)), "credits": credits}

    def _pause(self, reason: str) -> Paused:
        if not self.last_pause or self.last_pause.get("reason") != reason:
            logger.warning("ElevenLabs paused: %s", reason)
        self.last_pause = {"reason": reason, "at": time.time()}
        return Paused(reason)

    # ── What the core asks ──

    def status(self, refresh: bool = False) -> Dict[str, Any]:
        """Everything the core and the settings page show; never the key."""
        s = self.settings()
        key = self.client.key()
        if refresh and key:
            self.client.subscription(refresh=True)
        g = self.guard(s)
        credits = g.pop("credits")
        return {
            "label": "ElevenLabs",
            "ready": bool(key),
            "reason": "" if key else "No ElevenLabs API key yet. Paste one on the ElevenLabs add-on page.",
            "paused": g["tripped"],
            "pause_reason": g["reason"],
            "max_reply_chars": g["max_reply_chars"],
            "speed_applied": True,
            "model": el.resolve_model(s.get("model")),
            "default_voice": el.DEFAULT_VOICE,
            "default_voice_label": "George",
            "key_set": bool(key),
            "key_hint": el.mask_key(key),
            "models": el.MODELS,
            "default_model": el.DEFAULT_MODEL,
            "settings": s,
            "guard": g,
            "credits": credits,
            "tally": self.client.tally.snapshot(),
            "last_pause": self.last_pause,
            "last": self.last,
        }

    def voices(self, refresh: bool = False) -> List[Dict[str, Any]]:
        return self.client.voices(refresh)

    def _cache_path(self, text: str, model: str, voice: str, speed: float, fmt: str) -> str:
        raw = f"elevenlabs|{model}|{voice}|{speed}|{fmt}|{text}"
        return os.path.join(self.cache_dir, hashlib.sha256(raw.encode()).hexdigest() + (".wav" if fmt == "wav" else ".mp3"))

    def _cache_put(self, path: str, data: bytes) -> None:
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        try:
            files = [os.path.join(self.cache_dir, n) for n in os.listdir(self.cache_dir)
                     if n.endswith((".mp3", ".wav"))]
            total = sum(os.path.getsize(p) for p in files)
            if total <= CACHE_MAX_BYTES:
                return
            files.sort(key=lambda p: os.stat(p).st_atime)
            while files and total > CACHE_MAX_BYTES * 0.8:
                p = files.pop(0)
                total -= os.path.getsize(p)
                os.unlink(p)
        except OSError as e:
            logger.warning("Cache cleanup failed: %s", e)

    def synthesize(self, text: str, voice: str = "", speed: float = 1.0, fmt: str = "mp3",
                   use_cache: bool = True) -> bytes:
        """MP3 (or WAV for a call) for `text`. Raises Paused when the guard
        says no, TTSError for everything else."""
        s = self.settings()
        if not self.client.key():
            raise TTSError("No ElevenLabs API key yet. Paste one on the ElevenLabs add-on page.", 400)
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise TTSError("Nothing to say.", 400)
        fmt = "wav" if fmt == "wav" else "mp3"
        model = el.resolve_model(s.get("model"))
        voice = el.resolve_voice(voice)
        speed = el.clamp_speed(_num(speed, 1.0) or 1.0)
        # Credits are per character: one request never sends more than the cap.
        text = cap_text(text, int(_num(s.get("max_reply_chars"), 1500)))
        path = self._cache_path(text, model, voice, speed, fmt)
        if use_cache and os.path.exists(path):
            try:
                st = os.stat(path)
                os.utime(path, (time.time(), st.st_mtime))
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                pass
        # Only a cache miss costs credits, so only then check the reserve.
        g = self.guard(s)
        if g["tripped"]:
            raise self._pause(g["reason"])
        t0 = time.monotonic()
        try:
            audio = self.client.synthesize(text, voice, model, speed, wav=fmt == "wav")
        except el.QuotaExceeded as e:
            # Out of credits: stop asking for ten minutes.
            self._quota_hit = {"reason": e.message.rstrip("."), "at": time.time()}
            raise self._pause(self._quota_hit["reason"])
        self.last = {"latency_ms": round((time.monotonic() - t0) * 1000), "chars": len(text), "at": time.time()}
        if use_cache:
            try:
                self._cache_put(path, audio)
            except OSError as e:
                logger.warning("Could not cache audio: %s", e)
        return audio

    def set_key(self, key: str) -> Dict[str, Any]:
        """Check the key against the account, then store it. A key ElevenLabs
        rejects is not stored; a key that works but cannot read the account
        (a restricted key) is stored with a warning."""
        key = (key or "").strip()
        if not key:
            raise TTSError("Paste an ElevenLabs API key first.", 400)
        warning = ""
        try:
            self.client.subscription(key=key)
        except TTSError as e:
            if isinstance(e, el.InvalidKey):
                raise
            warning = e.message + " The key was saved, but the credit numbers may not show."
        el.save_api_key(key)
        self.client.forget()
        self._quota_hit = None
        logger.info("ElevenLabs API key saved (%s)", el.mask_key(key))
        return {"saved": True, "warning": warning, "key_hint": el.mask_key(key)}

    def delete_key(self) -> None:
        el.delete_api_key()
        self.client.forget()
        logger.info("ElevenLabs API key removed")

    def clear_cache(self) -> int:
        n = 0
        for name in os.listdir(self.cache_dir):
            if name.endswith((".mp3", ".wav")):
                os.unlink(os.path.join(self.cache_dir, name))
                n += 1
        return n
