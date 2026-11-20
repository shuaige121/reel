---
name: reel
description: >-
  Make or edit a video end to end: promo clips, photo-and-music reels, voice-over explainers, cutting footage into a
  finished piece, push-in close-ups on a subject, narrating a web page while highlighting it. Delivery always goes
  through `reel check` then `reel deliver`; only a final receipt counts as done. Use when asked to make, cut, edit,
  render or deliver a video.
---

# /reel: video production for Claude Code

A layered tool built on ffmpeg. You (Claude) look at the material, decide what to show and write a plan; code does
the framing geometry, rendering and checks. Output size is configurable (default vertical 1080x1920).

Commands and file formats are documented in `docs/ENGINE.md` of the reel repository. Read it before the first
command: `cat "$(reel --root)/docs/ENGINE.md"`. `reel <command> --help` lists every option. Every command prints one
JSON object (`{"ok": true, "result": ...}` or `{"ok": false, "error": {"code": ..., ...}}`) and exits 1 on failure.

## Order of work: direction first, then editing

For any video that someone other than the user will see, follow [`SOP.md`](./SOP.md): S0 intake, S1 source pack,
S2 asset inventory, S3-S4 a `director` agent writes `direction/BRIEF.md` and a storyboard, then edit straight away
(the approval step G1 does not block by default), then the delivery gate below. At S7 a separate `director` agent
reviews the cut against the BRIEF. Installed as a plugin, the agent is named `reel:director`.

- Give the director the paths of the original material, not your own conclusions about it.
- If an approved BRIEF exists and the purpose, audience and material scope have not changed, follow it.
- If the user says the video is boring or pointless, go back to S3 and rethink the piece; do not only re-edit.

## Delivery gate (mandatory)

Each step is enforced by code: skip one and you cannot deliver a final cut.

1. **Write the brief first** (`$P/brief.json`, template below) and register it: `reel register brief.json`. Delivery
   language, final or sample, delivery folder and what must not change: if you cannot fill these in, ask the user.
   Once registered, the Bash guard hook refuses `cp`/`mv`/`ffmpeg` writes into the delivery folder.
2. **Audition the voice.** There is no default voice. Pick one that fits the brief, render a 10-20 s sample and run
   `reel voice check sample.mp3 --script sample.txt --brief brief.json`. Local whisper transcribes the sample
   and compares it with the text (`--lang` defaults to `en`; for a Chinese voice-over pass `--lang zh`, which compares by toneless pinyin, so homophone spelling does
   not count as an error); only an error rate of 10% or less writes the approval into `brief.voice.approved`
   (`by: claude`, with the transcript as evidence). If the user listened, record their exact words instead.
   - STT proves the words are read correctly and clearly. It does not prove the voice suits the piece: say in your
     reply that you chose the voice, and switch voices in the next version if the user dislikes it.
   - A new voice needs a new check. The macOS `say` voice is for internal drafts only, never for a delivered video.
3. **Check the render:** `reel check final.mp4 --plan plan.json --brief brief.json [--profile vertical-strict|project-profile.json] [--text caption.txt]`.
   - Exit 1 means it cannot be delivered. Read `result.gate.blockers` (technical failures: even a sample cannot be
     delivered) and `result.gate.pending_review` (items a reviewer must look at).
   - Do not rely on `ok` alone. `result.gate.not_covered` lists what was not checked (for example on-screen text,
     which has no OCR); never say those items were checked.
4. **Review.** Look at every `needs_review` item (`reel scrub`, frame grabs) and write one review record per item with
   timecoded observations. Watch the whole cut yourself and sign `final_watch` (at least one frame per shot); listen
   from start to end and sign `final_listen` (an agent signature needs timecoded observations; if the user listened,
   record their exact words as `quote`). When you have signed both, deliver it as final: the receipt says
   `approved_by: agent`, and your reply says "final cut, reviewed by Claude, not yet seen by you". The user's
   feedback becomes the next version. The `reviews.json` format is in `docs/ENGINE.md`.
5. **Deliver:** `reel deliver final.mp4 --brief brief.json --version v3 --plan plan.json --reviews reviews.json`.
   - If the final conditions are not met it fails with `DELIVERY_NOT_FINAL` and lists why. Then only `--sample` is
     possible: the file name carries `sample-pending-review`, and you must call it a sample when you tell the user.
   - A version number can be delivered once; old versions are never deleted or overwritten. Only when the user
     explicitly asks to remove old versions: `reel register brief.json --remove`, delete, then register again.
6. **Saying "done"** requires a receipt with `status: final` (`reel receipt <receipt.json>` re-verifies it). Otherwise
   state the real status: "not finished", "sample pending review", "voice not auditioned". The Stop hook blocks a
   claim of completion that has no matching receipt.
7. Videos made by a project's own render script (`build_*.py` and the like), or composed in another tool and
   exported, go through steps 3-6 as well.

```json
{"version": "reel.brief.v1", "name": "spring-promo", "language": "en", "deliverable": "final",
 "delivery_root": "~/Videos/deliveries/spring-promo", "must_not_change": ["end card credit"],
 "voice": {"mode": "recorded"}, "profile": "default"}
```

- `voice.mode`: `none` (no voice), `recorded` (a real recording), `tts` or `clone` (these two also need `voice_id`
  and an `approved` block written by `reel voice check`, or a human approval with `quote`, `sample` and
  `sample_sha256`).
- `delivery_root` must be an existing absolute path (or start with `~`). It may not be the home folder, Desktop,
  Documents, Downloads, the iCloud Drive root, the project folder or a parent of it.
- A silent version uses a project profile: `{"extends": "default", "audio": "silent"}`.
- Profiles: `default` (general starting thresholds) and `vertical-strict` (1080x1920 30 fps, -16 LUFS, strict). Put
  project differences in a project profile with `extends`; register intended long holds in `allow` with a reason;
  add banned words (old product names, filler text) to `text.extra_banned`.

## At the top of every Bash call

Each Bash call starts a fresh shell, so begin every call with:

```bash
REEL_PROJECT=spring-promo                      # this project's name
command -v reel >/dev/null || { echo "reel is not on PATH (enable the plugin, or run ./install.sh --link)" >&2; return 1 2>/dev/null || exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; return 1 2>/dev/null || exit 1; }
P="${REEL_PROJECTS_DIR:-$HOME/reel-projects}/$REEL_PROJECT"; mkdir -p "$P/preview" && cd "$P"
```

All outputs live in `$P`, never in a temporary folder; relative paths below are relative to `$P`.

## Making the cut

You make the decisions (which shots, which subject, camera moves, pacing); perception, geometry, rendering and checks
are done by code. Full command syntax is in `docs/ENGINE.md`.

1. **Material** into `$P` (assets, `observations.json`, `plan.json`, previews, renders and check reports all live
   there; one plan per folder).
2. **Script and voice.** Write the voice-over script, pick a voice that fits the brief and pass the audition (gate
   step 2). Produce `voice.mp3` (or `.wav`) and, if you want subtitles, `voice.vtt`. For published work prefer a real
   recording or a voice whose licence covers the use; for a music-only piece point `audio.voice` at the music track
   (it is the timeline anchor).
3. **Look and pick targets:** `reel inspect` (list each file) → `reel frame` (Read the image with the 0-1 grid and read
   off the subject's box) → `reel observe` to register it, or `reel locate --query ... --check-png ...` (OWLv2; look
   at the check image before using a detection).
4. **Write `plan.json`** (EditPlan v1): `camera: []` holds the frame still; `zoom_to` with an `observation_id` pushes
   in on a registered subject; use `track` when the subject moves in a video; `transition_out` for transitions. Do not
   let a video shot cross a scene cut reported by `inspect`. `audio.voice` is required and resolves against `src_dir`.
5. **Validate and preview:** `reel validate` → `reel compile` → `reel preview plan.json s2 --out preview/s2.mp4 --sheet preview/s2.png`, then Read the contact sheet to judge the camera move.
6. **Render:** `reel render plan.json --out "$P/final.mp4" --sheet "$P/preview/final.png" --where local` (use
   `--where remote` only if the user configured a remote GPU and asked for it). Passing render checks only means the
   file is not broken; continue with gate steps 3-6.

Other entry points, each with a section in `docs/ENGINE.md`:

- **Inspect and change the timeline:** `reel timeline` (`--format text` or a `--png` multitrack image), `reel scrub`
  (frames around each cut), `reel edit` / `undo` / `history`, `reel preview --range` (low-res preview with sound).
- **Cut to the beat:** `reel beats music.mp3 --out beats.json` → `reel snap plan.json --beats beats.json --mode downbeat`;
  then check every cut with `reel timeline --png --beats`. Needs `./install.sh --beats`.
- **Narrate a web page with highlights:** a shot with `kind: "web"`; text positions come from the DOM (no OCR), so a
  highlighted sentence must match the page text exactly; `reel words voice.mp3` gives per-word timing. Needs
  `./install.sh --web`.
- **Shotcut projects:** `reel mlt project.mlt --video-only`; `--dupes` finds source footage used twice.

Available: plan assembly, sub-pixel push-ins on a subject box, following a moving subject, xfade transitions, burned-in
subtitles, background music, grading and LUTs, web shots, OWLv2 subject search, technical checks, gated delivery.
Not built in: eased transitions, frame interpolation, voice cloning.

## Remote GPU (optional)

The `--where remote` parts are enforced by code.

- Use it only when the user has configured `REEL_REMOTE` (an ssh destination) and asks for it; otherwise render with
  `--where local`.
- VRAM use is capped at 80% by default. When a job needs more, the tool returns `NEED_USER_APPROVAL`: tell the user
  first, and add `--allow-over-80` only with their consent. GPU utilisation of 50% or more means someone else is using
  it; do not take it.
- One reel GPU job at a time (remote file lock). Do not change power limits. Never reboot or shut down the host.
- `reel remote probe` reports the host; `reel remote setup-vision` installs a separate venv with torch and the OWLv2
  weights on the remote host.

## Companion files

- [`SOP.md`](./SOP.md): direction-first workflow, roles, steps and exit conditions.
- The `director` agent (`agents/director.md` in the repository): brief, storyboard and review.
- `docs/ENGINE.md` in the repository: commands, EditPlan, brief, reviews, receipts and profiles.
