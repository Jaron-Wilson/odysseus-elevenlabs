"""The add-on under the stand-in core (odysseus_mod.devcore), as Odysseus runs
it: the tts.voice capability with a signed identity, the page's API, and
the import of a key from an Odysseus that had ElevenLabs built in. A fake
ElevenLabs answers on loopback; nothing leaves the machine.

Run from the add-on folder: python -m pytest -q"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from odysseus_mod.devcore import DevCore  # noqa: E402

KEY = "sk_test_0123456789abcdefSECRET"
MP3 = b"ID3" + b"\x00" * 64


class FakeEleven:
    def __init__(self):
        self.synth = []
        self.used = 10_000
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.headers.get("xi-api-key") != KEY:
                    return self._send(401, {"detail": {"code": "invalid_api_key"}})
                if self.path.startswith("/v1/user/subscription"):
                    return self._send(200, {"tier": "creator", "character_count": outer.used,
                                            "character_limit": 131_000})
                if self.path.startswith("/v2/voices"):
                    return self._send(200, {"voices": [{"voice_id": "myclone1", "name": "Jaron",
                                                        "category": "cloned"}], "has_more": False})
                return self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                outer.synth.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
                if self.headers.get("xi-api-key") != KEY:
                    return self._send(401, {"detail": {"code": "invalid_api_key"}})
                return self._send(200, MP3, "audio/mpeg")

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def req(url, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, resp.headers.get("Content-Type", ""), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


@pytest.fixture(scope="module")
def setup(tmp_path_factory):
    fake = FakeEleven()
    os.environ["ELEVENLABS_BASE_URL"] = fake.url
    c = DevCore(ROOT, port=0, user="alice", state_dir=str(tmp_path_factory.mktemp("devcore")))
    c.serve()
    c.start_mod()
    yield c, fake
    c.close()
    fake.srv.shutdown()
    os.environ.pop("ELEVENLABS_BASE_URL", None)


def cap(c, method, path, body=None):
    conn, r = c.mod_request(method, "/mod/cap/tts.voice/" + path, json.dumps(body).encode() if body else None,
                            "odysseus-core", {"Content-Type": "application/json"})
    try:
        return r.status, r.getheader("Content-Type"), r.read()
    finally:
        conn.close()


def test_unsigned_cap_calls_are_refused(setup):
    c, _ = setup
    assert req(f"http://127.0.0.1:{c.mod_port}/mod/cap/tts.voice/status")[0] == 401


def test_without_a_key_it_is_not_ready(setup):
    c, _ = setup
    code, _, body = cap(c, "GET", "status")
    st = json.loads(body)
    assert code == 200 and st["ready"] is False and "API key" in st["reason"]
    code, _, body = cap(c, "POST", "synthesize", {"text": "Hi."})
    assert code == 400 and "API key" in json.loads(body)["error"]


def test_key_voices_and_speech(setup):
    c, fake = setup
    base = f"{c.url}/mods/{c.mod_id}/"
    code, _, body = req(base + "api/key", "POST", {"api_key": "sk_wrong_key_123456"})
    assert code == 400 and b"rejected" in body
    code, _, body = req(base + "api/key", "POST", {"api_key": KEY})
    assert code == 200 and KEY.encode() not in body
    code, _, body = cap(c, "GET", "status")
    st = json.loads(body)
    assert st["ready"] and st["key_hint"] == "..." + KEY[-4:] and KEY not in body.decode()
    assert st["max_reply_chars"] == 1500 and st["credits"]["remaining"] == 121_000
    code, _, body = cap(c, "GET", "voices")
    assert json.loads(body)["voices"][0]["voice_id"] == "myclone1"
    code, ctype, audio = cap(c, "POST", "synthesize", {"text": "Hello there.", "voice": "myclone1", "speed": 1})
    assert code == 200 and ctype == "audio/mpeg" and audio == MP3
    assert fake.synth[-1][0].startswith("/v1/text-to-speech/myclone1")


def test_settings_and_the_reserve_pause(setup):
    c, fake = setup
    base = f"{c.url}/mods/{c.mod_id}/"
    code, _, body = req(base + "api/settings", "POST", {"min_credits_pct": 50, "max_reply_chars": 99999})
    saved = json.loads(body)["settings"]
    assert code == 200 and saved["min_credits_pct"] == 50 and saved["max_reply_chars"] == 5000
    fake.used = 100_000  # 31,000 of 131,000 left: below 50%
    cap(c, "GET", "status?refresh=1")
    n = len(fake.synth)
    code, _, body = cap(c, "POST", "synthesize", {"text": "A new line."})
    assert code == 409 and json.loads(body)["paused"] is True and "below 50%" in json.loads(body)["reason"]
    assert len(fake.synth) == n
    import time
    for _ in range(50):  # sent from a thread
        if any(x.get("title") == "ElevenLabs paused" for x in c.notifications):
            break
        time.sleep(0.05)
    assert any(x.get("title") == "ElevenLabs paused" for x in c.notifications)


def test_page_loads(setup):
    c, _ = setup
    code, ctype, body = req(f"{c.url}/mods/{c.mod_id}/")
    assert code == 200 and b"ElevenLabs voice" in body


def test_import_from_core(tmp_path):
    from cryptography.fernet import Fernet
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import import_from_core as imp
    data = tmp_path / "odysseus-data"
    (data / "mods" / "elevenlabs" / "data").mkdir(parents=True)
    fkey = Fernet.generate_key()
    (data / ".key").write_bytes(fkey)
    (data / "api_keys.json").write_text(json.dumps({"elevenlabs": Fernet(fkey).encrypt(KEY.encode()).decode(),
                                                    "openai": "x"}))
    (data / "settings.json").write_text(json.dumps({"tts_provider": "elevenlabs", "tts_elevenlabs_model": "eleven_v3",
                                                    "tts_elevenlabs_min_credits_pct": 10}))
    (data / "elevenlabs_usage.json").write_text(json.dumps({"total_chars": 42}))
    r = imp.run(str(data))
    assert r["key"] == "..." + KEY[-4:] and r["usage"] is True
    secrets = data / "mods" / "elevenlabs" / "data" / "secrets.json"
    assert json.loads(secrets.read_text()) == {"api_key": KEY}
    assert oct(os.stat(secrets).st_mode & 0o777) == "0o600"
    assert json.loads((data / "mods" / "elevenlabs" / "settings.json").read_text()) == {
        "model": "eleven_v3", "min_credits_pct": 10}
    assert json.loads((data / "mods" / "elevenlabs" / "data" / "usage.json").read_text())["total_chars"] == 42
    # The core keeps its copy unless asked, and a different key is not overwritten silently.
    assert "elevenlabs" in json.loads((data / "api_keys.json").read_text())
    secrets.write_text(json.dumps({"api_key": "sk_other_key_999999"}))
    with pytest.raises(SystemExit):
        imp.run(str(data))
    r = imp.run(str(data), force=True, remove_from_core=True)
    assert r["removed_from_core"] and "elevenlabs" not in json.loads((data / "api_keys.json").read_text())


def test_import_needs_the_add_on_installed(tmp_path):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import import_from_core as imp
    with pytest.raises(SystemExit) as e:
        imp.run(str(tmp_path))
    assert "not installed" in str(e.value)
