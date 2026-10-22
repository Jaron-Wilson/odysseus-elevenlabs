"""ElevenLabs voice: an Odysseus add-on.

The core calls it as the `tts.voice` capability:

    GET  /mod/cap/tts.voice/status       ready, paused (and why), model, max per reply, credits
    GET  /mod/cap/tts.voice/voices       the account's voices
    POST /mod/cap/tts.voice/synthesize   {text, voice, speed, format: mp3|wav} -> audio
                                         409 {paused, reason} when the credit guard says no

and its page (the ElevenLabs tab) holds the key, the model, the credits and
the reserve.

Run it without Odysseus:   python -m odysseus_mod.devcore
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from odysseus_mod import CoreClient, ModApp, Response, html, json_response

from elevenlabs_mod import client as el
from elevenlabs_mod import store
from elevenlabs_mod.service import Paused, Voice
from elevenlabs_mod.store import TTSError

logger = logging.getLogger("elevenlabs_mod")
CAP = "/mod/cap/tts.voice"
TEST_TEXT = "Hi, this is your ElevenLabs voice."

app = ModApp(mod_id="elevenlabs")

_settings_cache = {"at": 0.0, "value": None}
_settings_lock = threading.Lock()


def _settings():
    """The mod settings, read from the core at most every few seconds (each
    sentence of a voice call is one synthesis)."""
    with _settings_lock:
        if _settings_cache["value"] is not None and time.time() - _settings_cache["at"] < 5:
            return _settings_cache["value"]
    try:
        value = app.core.settings()
    except Exception as e:
        logger.warning("Could not read settings from the core: %s", type(e).__name__)
        value = _settings_cache["value"] or {}
    with _settings_lock:
        _settings_cache.update(at=time.time(), value=value)
    return value


def _forget_settings():
    with _settings_lock:
        _settings_cache.update(at=0.0, value=None)


voice = Voice(_settings)
_notified = {"reason": ""}


def _error(e: TTSError) -> Response:
    # Never 401: the app reads a 401 from Odysseus as its own session ending.
    status = 400 if e.status == 401 else e.status
    return json_response({"error": e.message}, status)


def _audio(data: bytes) -> Response:
    wav = data[:4] == b"RIFF"
    return Response(data, 200, "audio/wav" if wav else "audio/mpeg")


def _tell_admins_paused(reason: str) -> None:
    """One notification each time the guard pauses ElevenLabs for a new reason."""
    if not reason or _notified["reason"] == reason or app.core.standalone:
        return
    _notified["reason"] = reason
    try:
        app.core.notify("ElevenLabs paused", reason + ". Odysseus speaks with Kokoro (or the browser voice) "
                        "until the credits reset.", level="warning", url=CoreClient.link("elevenlabs", "main"))
    except Exception as e:
        logger.info("Could not send the pause notification: %s", type(e).__name__)


# ── The capability the core calls ──

@app.get(CAP + "/status")
def cap_status(req):
    try:
        return json_response(voice.status(refresh=req.query.get("refresh") == "1"))
    except TTSError as e:
        return _error(e)


@app.get(CAP + "/voices")
def cap_voices(req):
    try:
        return json_response({"voices": voice.voices(req.query.get("refresh") == "1")})
    except TTSError as e:
        return _error(e)


@app.post(CAP + "/synthesize")
def cap_synthesize(req):
    body = req.json() if req.body else {}
    if not isinstance(body, dict):
        return json_response({"error": "send a JSON object"}, 400)
    try:
        data = voice.synthesize(str(body.get("text") or ""), str(body.get("voice") or ""),
                                body.get("speed", 1.0), str(body.get("format") or "mp3"),
                                use_cache=body.get("use_cache", True) is not False)
    except Paused as e:
        threading.Thread(target=_tell_admins_paused, args=(e.message,), daemon=True).start()
        return json_response({"paused": True, "reason": e.message}, 409)
    except TTSError as e:
        return _error(e)
    return _audio(data)


# ── The page ──

@app.get("/api/status")
def page_status(req):
    _forget_settings()
    return cap_status(req)


@app.get("/api/voices")
def page_voices(req):
    return cap_voices(req)


@app.post("/api/key")
def page_set_key(req):
    body = req.json() if req.body else {}
    try:
        return json_response(voice.set_key(str((body or {}).get("api_key") or "")))
    except TTSError as e:
        return _error(e)


@app.post("/api/key/delete")
def page_delete_key(req):
    voice.delete_key()
    return json_response({"deleted": True})


@app.post("/api/settings")
def page_save_settings(req):
    body = req.json() if req.body else {}
    current = dict(_settings() or {})
    current.update({k: v for k, v in (body or {}).items() if k in store.DEFAULTS})
    clean = store.clean_settings(current)
    try:
        app.core.save_settings(clean)
    except Exception as e:
        return json_response({"error": f"Could not save ({type(e).__name__})."}, 502)
    _forget_settings()
    return json_response({"saved": True, "settings": clean})


@app.post("/api/test")
def page_test(req):
    try:
        return _audio(voice.synthesize(TEST_TEXT, "", 1.0, "mp3"))
    except Paused as e:
        return json_response({"error": e.message + ". Odysseus would speak with Kokoro now."}, 409)
    except TTSError as e:
        return _error(e)


@app.post("/api/cache/clear")
def page_clear_cache(req):
    return json_response({"cleared": voice.clear_cache()})


@app.get("/")
def page(req):
    with open(os.path.join(os.path.dirname(__file__), "page.html"), "r", encoding="utf-8") as f:
        return html(f.read())


def main():
    logging.basicConfig(level=os.environ.get("ELEVENLABS_LOG", "INFO").upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    app.run()
