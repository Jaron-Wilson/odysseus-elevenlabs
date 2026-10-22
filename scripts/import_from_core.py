"""Move the ElevenLabs setup from an Odysseus that had it built in into this add-on.

Odysseus before the add-on kept the key encrypted in <data dir>/api_keys.json
(entry "elevenlabs", Fernet with <data dir>/.key), the settings in
<data dir>/settings.json (tts_elevenlabs_*) and the usage tally in
<data dir>/elevenlabs_usage.json. This copies all three into the add-on:

    <data dir>/mods/elevenlabs/data/secrets.json   the key (0600)
    <data dir>/mods/elevenlabs/settings.json       model, reserve, max per reply
    <data dir>/mods/elevenlabs/data/usage.json     the tally (only if the add-on has none)

Install the add-on first (Settings > Mods), then run this with the Python
Odysseus runs with (it needs the `cryptography` package):

    python scripts/import_from_core.py --data-dir ~/odysseus-data

It never prints the key, only its last four characters. Nothing is removed
from the core's files unless you pass --remove-from-core. Restart the add-on
afterwards (Settings > Mods > ElevenLabs voice > Restart).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from typing import Any, Dict

SETTINGS_MAP = {
    "tts_elevenlabs_model": "model",
    "tts_elevenlabs_min_credits_pct": "min_credits_pct",
    "tts_elevenlabs_min_credits": "min_credits",
    "tts_elevenlabs_max_reply_chars": "max_reply_chars",
}


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: Dict[str, Any], mode: int = 0o600) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def mask(key: str) -> str:
    return "..." + key[-4:] if len(key) >= 8 else ("set" if key else "")


def core_key(data_dir: str) -> str:
    """The ElevenLabs key the core stored, decrypted; '' when there is none."""
    enc = _read_json(os.path.join(data_dir, "api_keys.json")).get("elevenlabs") or ""
    if not enc:
        return ""
    key_file = os.path.join(data_dir, ".key")
    if not os.path.exists(key_file):
        raise SystemExit(f"{key_file} is missing, so the stored key cannot be read.")
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError:
        raise SystemExit("Run this with the Python Odysseus runs with: it needs the `cryptography` package.")
    with open(key_file, "rb") as f:
        fernet = Fernet(f.read().strip())
    try:
        return fernet.decrypt(enc.encode()).decode()
    except InvalidToken:
        raise SystemExit("The stored ElevenLabs key could not be decrypted with this data folder's .key.")


def run(data_dir: str, mods_dir: str = "", remove_from_core: bool = False, force: bool = False) -> Dict[str, Any]:
    data_dir = os.path.abspath(os.path.expanduser(data_dir))
    mods_dir = os.path.abspath(os.path.expanduser(mods_dir or os.path.join(data_dir, "mods")))
    mod_dir = os.path.join(mods_dir, "elevenlabs")
    if not os.path.isdir(mod_dir):
        raise SystemExit(f"The add-on is not installed here ({mod_dir} does not exist). Install it in "
                         "Settings > Mods first.")
    report: Dict[str, Any] = {"key": "", "settings": {}, "usage": False}

    key = core_key(data_dir)
    secrets_path = os.path.join(mod_dir, "data", "secrets.json")
    secrets = _read_json(secrets_path)
    if key:
        if secrets.get("api_key") and secrets["api_key"] != key and not force:
            raise SystemExit("The add-on already has a different key. Pass --force to replace it.")
        secrets["api_key"] = key
        _write_json(secrets_path, secrets)
        report["key"] = mask(key)

    core_settings = _read_json(os.path.join(data_dir, "settings.json"))
    moved = {new: core_settings[old] for old, new in SETTINGS_MAP.items() if old in core_settings}
    if moved:
        settings_path = os.path.join(mod_dir, "settings.json")
        current = _read_json(settings_path)
        current.update(moved)
        _write_json(settings_path, current, 0o600)
        report["settings"] = moved

    usage_src = os.path.join(data_dir, "elevenlabs_usage.json")
    usage_dst = os.path.join(mod_dir, "data", "usage.json")
    if os.path.exists(usage_src) and (force or not os.path.exists(usage_dst)):
        os.makedirs(os.path.dirname(usage_dst), exist_ok=True)
        shutil.copyfile(usage_src, usage_dst)
        report["usage"] = True

    if remove_from_core and key:
        keys = _read_json(os.path.join(data_dir, "api_keys.json"))
        keys.pop("elevenlabs", None)
        _write_json(os.path.join(data_dir, "api_keys.json"), keys)
        report["removed_from_core"] = True
    return report


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data-dir", default=os.environ.get("ODYSSEUS_DATA_DIR") or "~/odysseus-data",
                    help="the Odysseus data folder (default: $ODYSSEUS_DATA_DIR or ~/odysseus-data)")
    ap.add_argument("--mods-dir", default=os.environ.get("ODYSSEUS_MODS_DIR") or "",
                    help="the mods folder, when it is not <data dir>/mods")
    ap.add_argument("--remove-from-core", action="store_true", help="delete the key from the core's api_keys.json")
    ap.add_argument("--force", action="store_true", help="replace a different key or tally already in the add-on")
    a = ap.parse_args(argv)
    r = run(a.data_dir, a.mods_dir, a.remove_from_core, a.force)
    print("API key: " + (f"imported ({r['key']})" if r["key"] else "none found in the core"))
    print("Settings: " + (", ".join(f"{k}={v}" for k, v in r["settings"].items()) if r["settings"] else "none found"))
    print("Usage tally: " + ("copied" if r["usage"] else "kept as is"))
    if r.get("removed_from_core"):
        print("Removed the key from the core's api_keys.json.")
    print("Now restart the add-on: Settings > Mods > ElevenLabs voice > Restart.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
