# Voice

Bagley can speak and listen without the cloud. There are three layers, and each one is optional:

| Layer | What it does | Runs on |
|---|---|---|
| Browser voice (default) | Reads replies with your system voices; the mic button uses the browser's speech recognition (online in Chrome and Edge). | The browser |
| Server engines | **Piper** (offline) or **ElevenLabs** (online) speak replies; **whisper.cpp** (offline) transcribes what you say. | The computer running `bagley` |
| `bagley listen` | A wake word daemon for the desktop: say "Bagley, ..." and he answers out loud. | Your desktop session |

Pick the engines in **Settings, Voice**: speech output `browser`, `piper` or `elevenlabs`; speech input `browser` or `whisper`; the wake word (default `bagley`). Check what Bagley found with:

```sh
bagley voices
```

## Speech out with Piper (offline)

[Piper](https://github.com/rhasspy/piper) is a fast neural voice that runs on a laptop CPU.

**Install.** `pipx install piper-tts` (`sudo apt install pipx` first if you don't have it), or unpack a release from the Piper releases page and put `piper` on your `PATH`. Don't use Kali's own `piper` package: it is a gaming mouse configurator with the same command name, and Bagley skips it.

**Download a British voice.** Bagley is a dry, measured Englishman, so start with `en_GB-alan-medium`; `en_GB-northern_english_male-medium` is a warmer alternative. Each voice is an `.onnx` file plus its `.onnx.json`:

```sh
mkdir -p ~/.local/share/piper && cd ~/.local/share/piper
base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_GB
curl -LO $base/alan/medium/en_GB-alan-medium.onnx
curl -LO $base/alan/medium/en_GB-alan-medium.onnx.json
# The alternative:
curl -LO $base/northern_english_male/medium/en_GB-northern_english_male-medium.onnx
curl -LO $base/northern_english_male/medium/en_GB-northern_english_male-medium.onnx.json
```

Bagley looks for voices in `~/.local/share/piper/`, `~/.bagley/voices/` (the data folder) and `/usr/share/piper-voices/`, recommended British voices first.

**Choose it.** Set speech output to `piper`. Leave the voice empty to use the best voice found, or give a name (`en_GB-alan-medium`, or just `alan`) or a full path to an `.onnx` file. Then:

```sh
bagley speak "Right then. Shall we?"
echo "Piped text works too." | bagley speak
bagley speak --out hello.wav "Saved instead of played."
```

Bagley reads at a slightly brisk pace (`--length_scale 0.95`) with a quarter-second pause between sentences.

## Speech out with ElevenLabs (online)

ElevenLabs sounds the most like a person, at the cost of sending text to a third party.

1. Create an API key at elevenlabs.io (Profile, API keys).
2. Pick a refined British male voice in the Voice Library and copy its voice ID. Without one Bagley uses **George** (`JBFqnCBsd6RMkjVDRZzb`), a warm, measured British narrator; **Daniel** (`onwK4e9ZLuTAKqWW03F9`) is a crisper newsreader.
3. In Settings, Voice: speech output `elevenlabs`, paste the key and the voice ID.

Bagley uses the `eleven_multilingual_v2` model, MP3 at 44.1 kHz and 128 kbps, with stability 0.45, similarity 0.8 and style 0.2. The key is stored on the server and never shown to the browser in full.

**Privacy.** Every reply Bagley speaks goes to ElevenLabs as text: whatever is in it (file names, notes, calendar items) leaves your computer. Code blocks are left out. Use Piper when that matters. ElevenLabs counts characters against a monthly quota; when it runs out, or the key is wrong, Bagley says so instead of speaking.

## Speech in with whisper.cpp (offline)

**Install.** Build it from github.com/ggml-org/whisper.cpp and copy `build/bin/whisper-cli` to `~/.local/bin`:

```sh
sudo apt install build-essential cmake git
git clone https://github.com/ggml-org/whisper.cpp && cd whisper.cpp
cmake -B build && cmake --build build -j --config Release   # add -DGGML_VULKAN=ON for a GPU
cp build/bin/whisper-cli ~/.local/bin/
```

Bagley tries `whisper-cli`, `whisper-cpp`, `whisper` and `main`, in that order.

**Download a model:**

```sh
mkdir -p ~/.local/share/whisper && cd ~/.local/share/whisper
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin   # English, 142 MB, fast
curl -LO https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin     # Multilingual, 466 MB
```

`base.en` is plenty for commands. Use `small` if you also speak French to Bagley; the language is detected automatically. Bagley looks in `~/.bagley/models/`, `~/.local/share/whisper/`, `~/.local/share/whisper.cpp/` and `/usr/share/whisper.cpp*`, and prefers `base.en`.

**Install ffmpeg** (`sudo apt install ffmpeg`). The browser records WebM or Ogg Opus, which ffmpeg converts to the 16 kHz WAV whisper.cpp reads. The desktop daemon records WAV directly and doesn't need it.

**Choose it.** Set speech input to `whisper`. Leave the model empty for the best one found, or give a name (`base.en`) or a path. Try it on a file:

```sh
bagley transcribe note.webm
```

## The wake word: `bagley listen`

`bagley listen` runs on your desktop, listens to the microphone and answers out loud:

- "Bagley, what's on today?" runs the command straight away.
- "Bagley." on its own plays a short ready tone; say the command next (within 8 seconds).

Common mis-hearings count: "Bagly", "Badgley", "Baguely", "Bag Lee", "Hey Bagley", "Bailey". "Bagel" does not, and the wake word has to come first: "the Bagley report is due" is ignored. Fillers such as "hey", "okay" or "right" may come before it.

It needs:

- a recorder: `pw-record` (PipeWire), `parecord` (PulseAudio) or `arecord` (ALSA);
- whisper.cpp and a model on this computer, or on the Bagley server (it then sends each utterance there);
- a player for the reply: `pw-play`, `paplay`, `aplay` or `ffplay`.

Replies are spoken with the server's engine when it is Piper or ElevenLabs, else with Piper on this computer, else only printed. While Bagley speaks, the avatar shows VOICE and the microphone is ignored, so he never answers himself. Commands within five minutes of each other continue the same chat. Anything that needs approval waits for you in the web UI or overlay (or `bagley approve`).

```sh
bagley listen --test-mic      # Microphone levels and detected speech. Ctrl+C to stop.
bagley listen -v              # Listen, and show everything heard.
bagley listen --once          # One command, then exit.
```

| Option | Meaning |
|---|---|
| `--wake-word WORD` | Wake word (default: the preference, `bagley`) |
| `--no-wake` | Every utterance is a command |
| `--once` | Exit after one command |
| `--device NAME` | Recording device (PipeWire target, PulseAudio source or ALSA device) |
| `--model NAME` | whisper.cpp model, e.g. `base.en` or a path |
| `--voice NAME` | Piper voice for local speech |
| `-v`, `--verbose` | Show what was heard but ignored |
| `--test-mic` | Show levels and detected utterances |

Speech detection adapts to the room: it learns the background level, starts after about 200 ms of speech, ends after 700 ms of silence and never records more than 15 seconds at a time. If `--test-mic` never shows `SPEECH`, raise the microphone gain or pick another `--device` (`pw-record --list-targets` lists them).

**Push to talk.** Bind a key to a one-shot command without the wake word, e.g. in Hyprland:

```
bind = SUPER, B, exec, bagley listen --once --no-wake
```

**Another machine.** With `BAGLEY_URL` and `BAGLEY_TOKEN` set, the daemon talks to Bagley elsewhere (an always-on runner over Tailscale). It still transcribes on this computer when whisper.cpp is installed here.

### Start it with your session

A systemd user unit ships in `desktop/systemd/bagley-listen.service`:

```sh
mkdir -p ~/.config/systemd/user
cp desktop/systemd/bagley-listen.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now bagley-listen
journalctl --user -u bagley-listen -f
```

```ini
[Unit]
Description=Bagley wake word listener
After=pipewire.service pipewire-pulse.service bagley.service

[Service]
Type=simple
ExecStart=%h/.local/bin/bagley listen
Restart=on-failure
RestartSec=10
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin
Environment=PYTHONUNBUFFERED=1
Environment=NO_COLOR=1
EnvironmentFile=-%h/.config/bagley/listen.env

[Install]
WantedBy=default.target
```

Change `ExecStart` if `which bagley` points elsewhere. Put `BAGLEY_URL=...` and `BAGLEY_TOKEN=...` in `~/.config/bagley/listen.env` to reach another machine. The unit restarts the daemon if it fails, for example when the server isn't up yet.

## In the web UI

With speech output set to `piper` or `elevenlabs`, the web UI asks the server for the audio of each reply and plays it instead of a browser voice. With speech input set to `whisper`, the microphone button records in the browser and the server transcribes it, so nothing goes to Google. The browser only allows the microphone on a secure page: `http://localhost` works; from another device use HTTPS (for example `tailscale serve`).

## HTTP API

| Endpoint | Body | Answer |
|---|---|---|
| `POST /api/tts` | `{"text": "..."}` (Markdown is fine; up to 2500 spoken characters) | Audio: `audio/wav` (Piper) or `audio/mpeg` (ElevenLabs). 409 `{detail}` when speech output is `browser` or the engine isn't set up; 502 when the engine fails. |
| `POST /api/stt` | The recording as the body, with its `Content-Type` (`audio/webm`, `audio/ogg`, `audio/wav`, `audio/mp4`...), up to 25 MB; recordings are cut at 5 minutes | `{"text": "..."}`. 409 when whisper.cpp, a model or ffmpeg is missing; 415 for a non-audio type; 422 when the audio can't be read. |
| `GET /api/voice/status` | | Engines, programs, voices and models found, and any problems. |
| `POST /api/voice/speaking` | `{"speaking": true, "seconds": 120}` | `{"speaking": true}`. Shows VOICE on the avatar while a client plays audio; resets itself after `seconds` (default 120, at most 900). |

## Troubleshooting

- `bagley voices` shows what was found and lists problems.
- **Piper is silent or garbled:** the `.onnx.json` must sit next to the `.onnx`; Bagley reads the sample rate from it.
- **The wrong `piper` runs:** install `piper-tts-bin`, whose command is `piper-tts`.
- **whisper.cpp hears "Thank you." in silence:** that is whisper on noise. Raise the microphone gain or use `--device` for a closer microphone.
- **Bagley answers himself:** use headphones, or lower the speaker volume; the microphone is ignored while he speaks and for 0.4 seconds after.
