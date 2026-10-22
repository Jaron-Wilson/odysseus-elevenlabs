"""The ElevenLabs client and the voice the core calls (elevenlabs_mod/client.py,
service.py). Every HTTP call is faked: no real key and no network.

Covers the request shape, WAV wrapping for calls, the cache (a repeat costs no
credits), error mapping, the credit guard pausing (the core then speaks with
Kokoro or the browser), the key never showing up in an answer or a log line,
and parsing of voices and the subscription."""
import io
import json
import logging
import os
import sys
import wave

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from elevenlabs_mod import client as el  # noqa: E402
from elevenlabs_mod import store  # noqa: E402
from elevenlabs_mod.service import Paused, Voice  # noqa: E402
from elevenlabs_mod.store import TTSError  # noqa: E402

KEY = "sk_test_0123456789abcdefSECRET"
MP3 = b"ID3" + b"\x00" * 64
PCM = b"\x01\x00\x02\x00" * 400  # 800 samples


class FakeHTTP:
    """Stands in for httpx.post / httpx.get; records each request."""

    def __init__(self):
        self.calls = []
        self.synth_status = 200
        self.synth_body = None
        self.sub = {"tier": "creator", "status": "active", "character_count": 10_000,
                    "character_limit": 131_000, "next_character_count_reset_unix": 1_790_000_000,
                    "voice_limit": 30}
        self.sub_status = 200
        self.voice_pages = [
            {"voices": [
                {"voice_id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "category": "premade",
                 "labels": {"accent": "british"}, "preview_url": "https://x/p.mp3"},
                {"voice_id": "myclone1", "name": "Jaron", "category": "cloned", "labels": {}},
            ], "has_more": True, "next_page_token": "t2"},
            {"voices": [{"voice_id": "gen1", "name": "Aria", "category": "generated"}],
             "has_more": False, "next_page_token": None},
        ]

    def _resp(self, status, url, content=b"", js=None, headers=None):
        req = httpx.Request("GET", url)
        if js is not None:
            return httpx.Response(status, json=js, request=req, headers=headers or {})
        return httpx.Response(status, content=content, request=req, headers=headers or {})

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, params, json, headers, timeout))
        if self.synth_status != 200:
            return self._resp(self.synth_status, url, js=self.synth_body or {})
        fmt = (params or {}).get("output_format", "")
        return self._resp(200, url, content=PCM if fmt.startswith("pcm_") else MP3)

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, None, headers, timeout))
        if url.endswith("/v1/user/subscription"):
            if self.sub_status != 200:
                return self._resp(self.sub_status, url, js={"detail": {"code": "invalid_api_key"}})
            return self._resp(200, url, js=self.sub)
        if url.endswith("/v2/voices"):
            page = 1 if (params or {}).get("next_page_token") == "t2" else 0
            return self._resp(200, url, js=self.voice_pages[page])
        return self._resp(404, url, js={})

    def synth_calls(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture(autouse=True)
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("MOD_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


@pytest.fixture
def http(monkeypatch):
    fake = FakeHTTP()
    monkeypatch.setattr(el.httpx, "post", fake.post)
    monkeypatch.setattr(el.httpx, "get", fake.get)
    monkeypatch.setenv(el.BASE_URL_ENV, "http://fake-eleven")
    return fake


def _voice(tmp_path, settings=None, key=KEY):
    s = dict(store.DEFAULTS)
    s.update(settings or {})
    return Voice(lambda: dict(s),
                 client=el.ElevenLabsClient(key_getter=lambda: key, tally=el.UsageTally(str(tmp_path / "usage.json"))),
                 cache_dir=str(tmp_path / "cache"))


# ── Request shape ──

def test_synth_request_shape(http, tmp_path):
    v = _voice(tmp_path)
    assert v.synthesize("Hello there.", "myclone1", 1.1) == MP3
    method, url, params, body, headers, timeout = http.synth_calls()[0]
    assert url == "http://fake-eleven/v1/text-to-speech/myclone1"
    assert params == {"output_format": "mp3_44100_128"}
    assert body == {"text": "Hello there.", "model_id": "eleven_flash_v2_5", "voice_settings": {"speed": 1.1}}
    assert headers["xi-api-key"] == KEY and timeout is not None


def test_default_voice_and_no_speed_at_1x(http, tmp_path):
    _voice(tmp_path).synthesize("Hi.", "alloy")  # another engine's default
    _, url, _, body, _, _ = http.synth_calls()[0]
    assert url.endswith("/" + el.DEFAULT_VOICE) and "voice_settings" not in body


def test_speed_clamped_and_skipped_for_v4(http, tmp_path):
    _voice(tmp_path).synthesize("Fast talk.", "", 2)
    assert http.synth_calls()[0][3]["voice_settings"]["speed"] == el.SPEED_MAX
    _voice(tmp_path / "b", {"model": "eleven_v4"}).synthesize("Fast talk.", "", 1.2)
    assert "voice_settings" not in http.synth_calls()[1][3]


def test_wav_request_is_pcm_wrapped(http, tmp_path):
    audio = _voice(tmp_path).synthesize("Call audio.", fmt="wav")
    assert http.synth_calls()[0][2] == {"output_format": "pcm_16000"}
    with wave.open(io.BytesIO(audio)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 800)


def test_per_reply_cap(http, tmp_path):
    _voice(tmp_path, {"max_reply_chars": 40}).synthesize(
        "First sentence is here. Second sentence goes past the cap for sure.")
    assert http.synth_calls()[0][3]["text"] == "First sentence is here."


# ── Cache ──

def test_cache_hit_costs_nothing(http, tmp_path):
    v = _voice(tmp_path)
    assert v.synthesize("Same phrase.") == v.synthesize("Same phrase.")
    assert len(http.synth_calls()) == 1
    tally = v.client.tally.snapshot()
    assert tally["chars"] == len("Same phrase.") and tally["requests"] == 1
    assert tally["credits"] == pytest.approx(len("Same phrase.") * 0.5)
    v.synthesize("Same phrase.", fmt="wav")
    v.synthesize("Same phrase.", fmt="wav")
    assert len(http.synth_calls()) == 2  # WAV and MP3 are cached apart
    assert v.clear_cache() == 2


def test_cache_hit_even_when_guard_tripped(http, tmp_path):
    v = _voice(tmp_path)
    v.synthesize("Cached line.")
    http.sub["character_count"] = 130_000
    v.client.forget()
    assert v.synthesize("Cached line.") == MP3 and len(http.synth_calls()) == 1


# ── Errors ──

@pytest.mark.parametrize("status,body,want_status,want_text", [
    (401, {"detail": {"type": "authentication_error", "code": "invalid_api_key", "message": "bad"}}, 400, "rejected the API key"),
    (429, {"detail": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}}, 429, "busy"),
    (404, {"detail": {"type": "not_found", "code": "voice_not_found"}}, 404, "no voice with id"),
    (400, {"detail": {"type": "validation_error", "code": "invalid_voice_id"}}, 404, "no voice with id"),
    (422, {"detail": [{"loc": ["body", "text"], "msg": "field required", "type": "missing"}]}, 400, "field required"),
    (500, {"detail": {"message": "boom"}}, 502, "answered 500"),
])
def test_error_mapping(http, tmp_path, status, body, want_status, want_text):
    http.synth_status, http.synth_body = status, body
    with pytest.raises(TTSError) as ei:
        _voice(tmp_path).synthesize("Hello.")
    assert ei.value.status == want_status and want_text in ei.value.message and KEY not in ei.value.message


def test_timeout_maps_to_tts_error(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise httpx.ReadTimeout("slow")
    monkeypatch.setattr(el.httpx, "post", boom)
    monkeypatch.setattr(el.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("x")))
    with pytest.raises(TTSError) as ei:
        _voice(tmp_path).synthesize("Hello.")
    assert ei.value.status == 504


def test_no_key_is_a_clear_error(http, tmp_path):
    v = _voice(tmp_path, key="")
    with pytest.raises(TTSError) as ei:
        v.synthesize("Hello.")
    assert "API key" in ei.value.message and not http.synth_calls()
    assert v.status()["ready"] is False


# ── Credit guard ──

def test_guard_pauses_below_the_reserve(http, tmp_path):
    http.sub["character_count"] = 126_000  # 5,000 of 131,000 left = 3.8%
    v = _voice(tmp_path)
    with pytest.raises(Paused) as ei:
        v.synthesize("Low on credits.")
    assert ei.value.status == 409 and "below 5%" in ei.value.message and not http.synth_calls()
    st = v.status()
    assert st["paused"] and "below 5%" in st["pause_reason"] and st["last_pause"]["reason"] == ei.value.message


def test_guard_absolute_floor(http, tmp_path):
    http.sub["character_count"] = 100_000  # 31,000 left, above 5%
    with pytest.raises(Paused):
        _voice(tmp_path, {"min_credits": 50_000}).synthesize("Floor.")


def test_guard_off_when_plenty(http, tmp_path):
    v = _voice(tmp_path)
    v.synthesize("Plenty.")
    assert len(http.synth_calls()) == 1 and not v.status()["paused"]


def test_local_spend_moves_cached_credits(http, tmp_path):
    http.sub["character_count"] = 124_000  # 7,000 left = 5.3%
    v = _voice(tmp_path)
    v.synthesize("x" * 1400 + ".")  # 0.5 credits/char -> 700 credits
    c = v.client.credits()
    assert c["estimated"] and c["remaining"] == 7000 - 700
    with pytest.raises(Paused):
        v.synthesize("Next one.")


def test_quota_exceeded_pauses_for_a_while(http, tmp_path):
    v = _voice(tmp_path)
    http.synth_status = 401
    http.synth_body = {"detail": {"status": "quota_exceeded", "message": "This request exceeds your quota."}}
    with pytest.raises(Paused):
        v.synthesize("Out of credits.")
    http.synth_status = 200
    with pytest.raises(Paused):
        v.synthesize("Still paused.")
    assert len(http.synth_calls()) == 1  # no second paid try within the pause


# ── Key handling ──

def test_key_never_in_answers_or_logs(http, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    v = _voice(tmp_path)
    saved = {}
    monkeypatch.setattr(el, "save_api_key", lambda k: saved.setdefault("k", k))
    out = v.set_key("  " + KEY + "  ")
    assert saved["k"] == KEY
    v.synthesize("Log check.")
    status = v.status(refresh=True)
    blob = json.dumps([out, status], default=str)
    assert KEY not in blob and KEY[-12:] not in blob and status["key_hint"] == "..." + KEY[-4:]
    assert KEY not in caplog.text


def test_bad_key_is_not_saved(http, tmp_path, monkeypatch):
    http.sub_status = 401
    monkeypatch.setattr(el, "save_api_key", lambda k: pytest.fail("saved a rejected key"))
    with pytest.raises(TTSError) as ei:
        _voice(tmp_path).set_key("sk_wrong_key_123456")
    assert ei.value.status == 400 and isinstance(ei.value, el.InvalidKey)


def test_key_file_is_private(data):
    el.save_api_key(KEY)
    path = data / "secrets.json"
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert el.get_api_key() == KEY
    el.delete_api_key()
    assert el.get_api_key() == ""


def test_settings_are_clamped():
    s = store.clean_settings({"model": " eleven_v3 ", "min_credits_pct": 500, "max_reply_chars": "x", "junk": 1})
    assert s == {"model": "eleven_v3", "min_credits_pct": 100, "min_credits": 0, "max_reply_chars": 1500}


# ── Parsing ──

def test_voices_parsed_paged_and_sorted(http, tmp_path):
    v = _voice(tmp_path)
    voices = v.voices()
    assert [x["name"] for x in voices] == ["Jaron", "Aria", "George"]  # cloned, generated, premade
    assert voices[2] == {"voice_id": "JBFqnCBsd6RMkjVDRZzb", "name": "George", "category": "premade",
                         "labels": {"accent": "british"}, "preview_url": "https://x/p.mp3"}
    gets = [c for c in http.calls if c[1].endswith("/v2/voices")]
    assert gets[0][2]["page_size"] == 100 and gets[1][2]["next_page_token"] == "t2"
    v.voices()
    assert len([c for c in http.calls if c[1].endswith("/v2/voices")]) == 2  # cached


def test_subscription_parsed(http, tmp_path):
    status = _voice(tmp_path).status(refresh=True)
    c = status["credits"]
    assert c["known"] and c["used"] == 10_000 and c["limit"] == 131_000 and c["remaining"] == 121_000
    assert c["remaining_pct"] == pytest.approx(92.4) and c["reset_unix"] == 1_790_000_000
    assert c["tier"] == "creator" and not c["estimated"]
    assert status["model"] == "eleven_flash_v2_5" and status["max_reply_chars"] == 1500
    assert any(m["id"] == "eleven_flash_v2_5" and m["credits_per_char"] == 0.5 for m in status["models"])


def test_subscription_unreadable_is_not_fatal(http, tmp_path):
    http.sub_status = 401
    v = _voice(tmp_path)
    assert v.client.credits()["known"] is False
    assert v.synthesize("Still speaks.") == MP3


def test_pcm_to_wav_odd_length():
    w = el.pcm_to_wav(b"\x01\x02\x03", 24000)
    with wave.open(io.BytesIO(w)) as f:
        assert f.getframerate() == 24000 and f.getnframes() == 1


def test_resolve_voice_keeps_ids_with_underscores():
    assert el.resolve_voice("clone_jaron") == "clone_jaron"
    assert el.resolve_voice("af_heart") == el.DEFAULT_VOICE
    assert el.resolve_voice("bm_george+af_bella") == el.DEFAULT_VOICE
    assert el.resolve_voice("alloy") == el.DEFAULT_VOICE
    assert el.resolve_voice("") == el.DEFAULT_VOICE
