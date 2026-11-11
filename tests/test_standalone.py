"""The mod run WITHOUT Odysseus, as a user would: `python -m elevenlabs_mod` in its own process, with no
ODYSSEUS_* variables. It must start, ask for MOD_PASSWORD, ignore forged Odysseus identity headers,
and serve its pages and API to the signed-in user."""
import http.cookiejar
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE = "elevenlabs_mod"
PAGES = ["/"]                 # HTML pages, 200 once signed in
API = ["/api/status"]         # JSON routes, 200 once signed in
PASSWORD = "standalone-test-pw"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def standalone():
    port = _free_port()
    data = tempfile.mkdtemp(prefix="standalone-")
    env = {k: v for k, v in os.environ.items() if not k.startswith("ODYSSEUS_")}
    env.update(MOD_PORT=str(port), MOD_HOST="127.0.0.1", MOD_PASSWORD=PASSWORD, MOD_DATA_DIR=data,
               PYTHONUNBUFFERED="1")
    proc = subprocess.Popen([sys.executable, "-m", MODULE], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("the mod exited: " + proc.stdout.read()[-2000:])
        try:
            with urllib.request.urlopen(base + "/health", timeout=2) as r:
                if r.status == 200:
                    break
        except OSError:
            time.sleep(0.3)
    else:
        proc.kill()
        pytest.fail("the mod did not answer /health within 30 s")
    yield base, data
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _client():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)), jar


def _get(opener, url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with opener.open(req, timeout=15) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read()


def _signed_in(base):
    opener, jar = _client()
    status, _, _ = _get(opener, base + "/login", urllib.parse.urlencode({"password": PASSWORD}).encode())
    assert status == 200 and any(c.name == "mod_session" for c in jar), "sign-in did not set a session"
    return opener


def test_health_is_open(standalone):
    base, _ = standalone
    opener, _ = _client()
    assert _get(opener, base + "/health")[0] == 200


def test_pages_need_the_password(standalone):
    base, _ = standalone
    opener, jar = _client()
    for path in PAGES + API:
        status, _, body = _get(opener, base + path)
        assert status == 401, path
        assert b"password" in body.lower()
    status, _, _ = _get(opener, base + "/login", urllib.parse.urlencode({"password": "wrong"}).encode())
    assert not any(c.name == "mod_session" for c in jar)


def test_forged_odysseus_headers_are_ignored(standalone):
    base, _ = standalone
    opener, _ = _client()
    forged = {"X-Odysseus-User": "admin", "X-Odysseus-Mod": MODULE, "X-Odysseus-Ts": str(int(time.time())),
              "X-Odysseus-Sig": "0" * 64}
    assert _get(opener, base + PAGES[0], headers=forged)[0] == 401


def test_signed_in_user_gets_pages_and_api(standalone):
    base, _ = standalone
    opener = _signed_in(base)
    for path in PAGES:
        status, ctype, body = _get(opener, base + path)
        assert status == 200 and "text/html" in ctype, path
        assert b"<html" in body.lower() or b"<!doctype" in body.lower()
    for path in API:
        status, ctype, _ = _get(opener, base + path)
        assert status == 200 and "json" in ctype, path
    status, _, body = _get(opener, base + "/mod/tools")
    assert status == 200 and b'"tools"' in body
