# reel

[中文说明](README.zh-CN.md)

**reel is a video production workflow for [Claude Code](https://code.claude.com/docs/en/overview)**: a
skill, a command-line engine built on ffmpeg, two delivery hooks and a director agent. It is not related to Instagram
Reels.

> /reel is a Claude Code skill. I write the brief, the agent plans, edits and checks the video, and delivers a file
> that is ready to post. I review it and ask for changes like any client would.

Short version: **Made with /reel: brief in, checked video file out.**

## What it is not

A one-click generator. The agent needs a clear brief and your own material. Songs, AI images and AI video clips come
from other services you bring yourself. Expect to give feedback and get new versions, the way you would with a human
editor.

- It does not write songs or generate pictures or AI video. Those come from you or from other services; reel arranges,
  edits, checks and delivers.
- "Final" means: the technical checks passed, the agent watched and listened to the whole cut and signed for it, and
  the file was copied and hash-verified. It does not mean you have seen it or like it.
- On-screen text is not OCR-checked. The banned-word scan covers the brief, the plan, subtitles and caption files only,
  and the check report lists everything it did not check under `not_covered`.
- It is not free to run: Claude Code needs a Claude subscription or API access. reel itself calls no paid service.

## What you get

| Part | Where | What it does |
|---|---|---|
| Skill | `skills/reel/SKILL.md`, `skills/reel/SOP.md` | Tells Claude the order of work: intake, source pack, asset inventory, direction and storyboard, voice audition, edit, review, delivery, feedback |
| Engine | `reel.py`, `reelcore/`, `bin/reel` | One JSON-in/JSON-out command per step: inspect, frame grid, observe/locate a subject, validate, compile, preview, render, timeline, scrub, edit/undo, beats/snap, check, deliver, receipt |
| Hooks | `hooks/` | A Bash guard that only lets `reel deliver` write into a registered delivery folder, and a Stop hook that blocks "it's done" when there is no final receipt |
| Director agent | `agents/director.md` | A separate agent that writes the brief and storyboard and reviews the cut against them |
| Example | `examples/photo-reel/` | A no-cost project that runs end to end on your machine and produces a delivered file plus a receipt |
| Engine reference | `docs/ENGINE.md` | Commands, EditPlan, brief, review records, receipts, profiles |

Engine features, each backed by code and tests: sub-pixel push-ins on a registered subject box, following a moving
subject across keyframes, pans and holds, xfade transitions, burned-in subtitles, background music, grading and LUTs,
a frame-exact timeline anchored to the audio track, narrating a web page with highlights driven by the DOM, beat
detection and cut snapping, a technical gate (decoding, format, black and frozen frames, long stills, clipping,
loudness, silences, banned words) with per-project profiles, review records bound to the video's sha256, and
hash-verified delivery with receipts.

## Requirements

- **macOS.** That is the only platform tested. Linux and Windows are not verified.
- **Claude Code** (needs a Claude subscription or API access).
- **Python 3.10 or newer** (tested with 3.12), used through a private virtualenv that `install.sh` creates.
- **ffmpeg** (`brew install ffmpeg`).
- **mpv** (`brew install mpv`) to burn in subtitles when your ffmpeg has no libass. The test suite uses subtitles.

Optional, per feature (all free and local unless noted):

| Feature | Install |
|---|---|
| Beat detection and snapping (`reel beats`, `reel snap`) | `./install.sh --beats` (librosa); `--beats-ml` adds beat_this and torch, and downloads its model on first use |
| Web-page narration (`kind: "web"` shots) | `./install.sh --web` (Playwright + Chromium) |
| Local subject search (`reel locate`, OWLv2) | `./install.sh --vision` (torch, transformers; model weights download on first use) |
| Per-word timing and voice audition (`reel words`, `reel voice check`) | `brew install whisper-cpp`, a whisper.cpp model file, `REEL_WHISPER_MODEL=/path/to/model.bin`; Chinese also needs `./install.sh --zh-voice` |
| Remote GPU rendering (`--where remote`) | a Linux host with an NVIDIA GPU reachable over ssh, `REEL_REMOTE=user@host`; `reel remote setup-vision` installs its own venv there |

## Install

Clone this repository, then from its folder:

```bash
./install.sh            # creates ~/.local/share/reel/venv with numpy and Pillow; add flags from the table above
```

### As a Claude Code plugin (recommended)

The repository is a plugin and a one-plugin marketplace. In Claude Code:

```
/plugin marketplace add /absolute/path/to/this/repository
/plugin install reel@reel-tools
```

For a single session without installing: `claude --plugin-dir /absolute/path/to/this/repository`.

While the plugin is enabled, `reel` is on the Bash tool's PATH, both hooks are active, the skill runs as `/reel:reel`
(Claude also picks it up when you ask for a video), and the agent is `reel:director`.
The hooks are active only while the plugin is enabled; they do nothing until a brief is registered with `reel register`, and the Stop hook acts only on video work.

### Manual install

```bash
./install.sh --link                                   # venv + a reel symlink in ~/.local/bin (must be on PATH)
mkdir -p ~/.claude/skills ~/.claude/agents
cp -R skills/reel ~/.claude/skills/reel               # the skill then runs as /reel
cp agents/director.md ~/.claude/agents/director.md
```

Then add the hooks to `~/.claude/settings.json`, replacing `REPO` with the folder that `reel --root` prints:

```json
{
  "hooks": {
    "PreToolUse": [
      {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 REPO/hooks/guard_delivery.py", "timeout": 5}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python3 REPO/hooks/deliver_gate.py", "timeout": 30}]}
    ]
  }
}
```

If your `settings.json` already has `PreToolUse` or `Stop` entries, add these to the existing arrays. The hooks use
only the Python standard library.

## Try the example (no paid service, no network)

```bash
examples/photo-reel/run.sh
```

It generates five stand-in photo cards and a synthesized music track, registers a subject on two cards, validates and
renders a 14.4 s vertical video, runs `reel check`, delivers it with `reel deliver` and verifies the receipt. The
media are placeholders: replace them with your own photos and music and run it again. Details in
[`examples/photo-reel/README.md`](examples/photo-reel/README.md).

With the skill installed, the same flow from Claude Code is: "Make a 15-second vertical video from the photos in this
folder with this music", followed by a brief, a plan, a render, a check and a delivery receipt.

## Which routes cost money

- **Always:** Claude Code itself (subscription or API).
- **Never inside reel:** the engine, hooks, example and tests make no paid calls and need no API keys.
- **Only if you bring them:** AI-generated songs, AI images, AI video clips and cloud text-to-speech come from
  third-party services with their own pricing and terms. reel does not integrate any of them; you generate the files
  there and hand them to reel as material.
- **Free route:** your photos or footage plus your own recording or music → framing, push-ins, transitions, subtitles,
  beat cuts → check → delivery; and narrating a web page with highlights. This route needs only Claude Code and the
  free local tools above.

## Optional: Hypit

[Hypit](https://hypit.ai) is a separate third-party product for composing videos with AI agents. You can compose or
lay out a video there, export the file, and still run `reel check` and `reel deliver` on it. This repository contains
no Hypit code and is not affiliated with it.

## Tests

```bash
tests/run_all.sh
```

Runs every suite with the reel venv: geometry and tracking, timeline tools, the delivery-gate fault-injection matrix,
the hooks, music-video contracts and Shotcut parsing. Tests for optional extras that are not installed are reported as
SKIPPED. All test media are synthesized locally.

## Roadmap

- Find repeated shots by picture similarity (today `reel mlt --dupes` only finds a reused source range of the same
  file).
- Check that subtitles and on-screen text match the brief's delivery language.
- Code gates for steps the SOP enforces only in writing: source-pack hashes in the brief, the user's verdict bound to
  a receipt, and a required director review record before a final delivery.
- English for all engine messages (error codes are already English); a music-video handbook; notes on using reel with
  Hypit.

## Status

A personal project, shared as is. Issues and questions are answered as time allows.

Author: Leonard Chow.

License: [MIT](LICENSE). Third-party components and their licences: [THIRD_PARTY.md](THIRD_PARTY.md).
