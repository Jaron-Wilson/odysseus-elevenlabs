# ElevenLabs voice for Odysseus

An Odysseus add-on (mod) that adds **ElevenLabs** text to speech: replies read aloud and voice
calls in your account's voices, your own cloned voices included. Odysseus's local Kokoro voice
stays built in; this add-on is a voice next to it.

- **Credit reserve.** ElevenLabs bills per character. Below a share of your plan (or a number of
  credits) the add-on pauses, and Odysseus speaks with Kokoro (or the browser voice when Kokoro is
  not downloaded) until the credits reset. It also pauses for ten minutes when ElevenLabs says the
  quota is used up. Admins get a notification each time it pauses.
- **Max per reply.** At most this many characters of one reply are read aloud.
- **Cache.** A phrase already spoken is kept (in the add-on's data folder, 200 MB at most) and
  costs nothing the next time.
- **The key stays on the server**, in `<data dir>/mods/elevenlabs/data/secrets.json` (0600). The
  page only ever shows its last four characters.

## Install

1. Odysseus: **Settings > Mods > Install a mod**, paste `git@github.com:Jaron-Wilson/odysseus-elevenlabs.git`
   (or this folder), **Fetch and review**, then **Approve and install**. It asks for one
   permission, `notify` (the pause notification).
2. Open the **ElevenLabs** tab (System group) and paste an API key (elevenlabs.io, *Developers >
   API keys*). A restricted key works if it can read voices and make speech; the credit numbers
   need *User: read*.
3. **Settings > AI Defaults > Voice call > Speaks with**: pick *ElevenLabs voice (add-on)*. The voice
   list shows the account's voices.

Requires an Odysseus with capability providers (`provides`, mod API 0.1). It runs with Odysseus's
own Python (it needs `httpx`, which Odysseus already has).

## Moving your key from Odysseus

Odysseus before this add-on kept the ElevenLabs key itself (encrypted in `api_keys.json`), with its
settings in `settings.json` and a usage tally. After installing the add-on, run this once on the
server, with any Python that has the `cryptography` package (the system `python3` on Ubuntu has it, as
does the one Odysseus runs with), and the data folder Odysseus uses:

```
! python3 ~/odysseus-data/mods/elevenlabs/src/scripts/import_from_core.py --data-dir ~/odysseus-data
```

It copies the key (printing only its last four characters), the model, the reserve, the max per
reply and the tally into the add-on, and leaves the core's copy alone (add `--remove-from-core` to
delete it there). Then **Settings > Mods > ElevenLabs voice > Restart**. A saved *Speaks with:
ElevenLabs* keeps working: Odysseus reads it as this add-on.

## How Odysseus calls it

The manifest says `"provides": ["tts.voice"]`, and the core calls:

| Call | Answers |
|---|---|
| `GET /mod/cap/tts.voice/status` | `ready`, `reason`, `paused`, `pause_reason`, `max_reply_chars`, `model`, credits, the tally |
| `GET /mod/cap/tts.voice/voices` | `{voices: [{voice_id, name, category, labels}]}` |
| `POST /mod/cap/tts.voice/synthesize` | `{text, voice, speed, format: "mp3" or "wav"}`: the audio, or `409 {paused, reason}` |

Every call carries the core's signed identity; anything else is refused.

## Develop

```
python -m odysseus_mod.devcore        # the add-on under a stand-in core
python -m pytest -q                   # tests (ElevenLabs is faked; nothing leaves the machine)
```

`ELEVENLABS_BASE_URL` points the client at another server (the tests use a fake one).

## License

AGPL-3.0, like Odysseus.
