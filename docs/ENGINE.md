# reel engine reference (`reelcore`)

The engine is a command-line tool meant to be driven by an agent: the model makes decisions, deterministic code does
perception, geometry, rendering and checks. A remote GPU host is optional (see "Remote GPU").

Run it as `reel <command>` (the plugin puts `bin/reel` on the Bash tool's PATH; a manual install links it with
`./install.sh --link`). `reel --root` prints the repository folder. Every command prints exactly one JSON object on
stdout, `{"ok": true, "result": ...}` or `{"ok": false, "error": {"code": "...", "message": "...", "detail": {...}}}`,
and exits 1 on failure. Error codes are stable English identifiers; most human-readable messages are in Chinese.

## Layers

| Layer | Where | Job | Output |
|---|---|---|---|
| Tools | `reel.py` → `reelcore/cli.py` | one subcommand per tool, JSON in and out, structured errors | `{"ok":…,"result"/"error":…}` |
| Decisions | the model | reads the asset list, grid frames and observations; writes `plan.json` | EditPlan v1 |
| Perception | `perceive.py` / `web.py` | frames with a coordinate grid; registers boxes the model read off; OWLv2 text-prompted detection; per-character boxes from the DOM | `observations.json` |
| Plan | `plan.py` | EditPlan v1 validation (all errors reported at once); legacy text-plan adapter | normalised plan |
| Geometry | `camera.py` | box → crop window at the output aspect → easing → sub-pixel `perspective` corner expressions | per-frame framing |
| Render | `render.py` | frame-exact timeline (transition overlaps counted), segments, joins, mix, grade, subtitles | the cut |
| Verify | `verify.py` | size, frame rate, frame count, audio/video length, decodability; any failure fails the render | RenderResult |
| Gate | `check.py`, `gate.py`, `deliver.py` | technical checks, review records, brief, hash-verified delivery and receipts | report, receipt |
| Remote | `remote.py` | optional GPU host: probe, VRAM gate, GPU lock, rsync, remote run | - |

The backend only chooses the encoder and how subtitles are burned in: local = libx264, plus libass if ffmpeg has it,
otherwise mpv; remote = NVENC + libass. There is one copy of the timeline maths.

## Workflow (the order the agent calls the tools)

```bash
cd "$P" && mkdir -p preview                      # run inside the project folder: plan.json, observations.json, previews
reel inspect media/a.jpg media/b.mp4             # list files one by one; videos also report scene cuts
reel frame media/a.jpg --out preview/a_grid.png  # frame with a 0-1 grid → Read the image to read coordinates
reel observe --obs observations.json --src media --asset a.jpg --bbox 0.62,0.18,0.80,0.41 --label "product logo"
reel locate  --obs observations.json --src media --asset b.mp4 --t 4.2,4.8,5.4,6.0 --query "a coffee machine" --check-png preview/chk.png
#   OWLv2; runs locally when torch is installed (./install.sh --vision), otherwise on REEL_REMOTE.
#   --check-png draws the detections back on the frame: look at it before using them.
# write plan.json (below); zoom_to refers to an observation_id
reel validate plan.json                          # without --obs, every command reads observations.json next to the plan
reel compile  plan.json                          # frame-level timeline, no rendering
reel preview  plan.json s2 --out preview/s2.mp4 --sheet preview/s2.png   # one shot, no sound, plus a contact sheet
reel render   plan.json --out final.mp4 --sheet preview/final.png --where local
```

`render --where auto` (the default) uses the remote host only when `REEL_REMOTE` is set, the host is online, has
NVENC and libass, and passes the GPU gate; otherwise it renders locally and says why in `note`. With no remote host
configured it renders locally without trying ssh. Write `--where local` unless the user asked for the remote GPU.

An old text plan (`file|in|duration|move` per line) converts with `reel from-legacy plan.txt --src media --voice voice.mp3 --out plan.json`.

## EditPlan v1

```json
{"version": "reel.plan.v1", "src_dir": "media", "output": {"width": 1080, "height": 1920, "fps": 30},
 "duration_policy": "fit_last_shot",
 "shots": [
  {"id": "s1", "asset": "a.jpg", "duration": 3.2,
   "camera": [{"op": "zoom_to", "observation_id": "obs-1", "t0": 0.4, "t1": 3.0,
               "easing": "ease_in_out", "padding": 0.15, "max_zoom": 2.5}],
   "transition_out": {"type": "fade", "duration": 0.4}},
  {"id": "s2", "asset": "b.mp4", "source_in": 12.0, "duration": 2.5, "camera": []}],
 "audio": {"voice": "voice.mp3", "subtitles": "voice.vtt", "bgm": null, "bgm_gain": 0.16, "keywords": ["keyword"]},
 "look": {"grade": false, "lut": null}}
```

- `camera: []` means truly still. A `zoom_to` box is always in normalised source-image coordinates and must come from
  an observation (IoU ≥ 0.5 is enforced; do not edit detection boxes by hand).
- **Video shots:** the observation's time must lie inside the shot's source range; a range that crosses a scene cut
  fails with `SCENE_CUT_IN_SHOT`. If the source picture itself moves (first/last frame SSIM < 0.85) but only one box is
  given, it fails with `SOURCE_CAMERA_MOVING`: use `"track": ["obs-5", "obs-8", …]` (the same subject at several times,
  in increasing order). The crop size covers every keyframe and the centre follows the keyframes linearly; a
  `zoom_to` with `track` must be the shot's last camera move.
- `move_to` gives an exact crop window; with `space: "cover"` coordinates are relative to the centred cover crop.
- A subject that cannot fit the output aspect (a wide subject in a vertical frame) fails with `SUBJECT_TOO_LARGE`
  instead of being cropped silently; when only the padding does not fit, the padding shrinks.
- The audio track is the timeline anchor: visible frames = sum of shot frames − sum of transition frames.
  `require_match` fails with `DURATION_MISMATCH` beyond 0.1 s; `fit_last_shot` adjusts the last shot.
- A video shot whose in-point plus duration exceeds the source fails with `SOURCE_TOO_SHORT` (no silent freeze); a
  difference of 1.5 frames or less (rounding) is padded with the last frame.
- Subtitles parse strictly by block: an unparsable block fails with `SUBTITLES_INVALID`; verification checks that every
  cue lies inside the cut.
- `audio.voice` is required. It, the assets, subtitles and background music all resolve against `src_dir`, not the
  plan's folder.
- Transitions: `fade`, `slideleft`, `slideright`, `fadeblack`, `dissolve`, `circleopen`; a transition may be at most
  half the shorter neighbouring shot.

## Web shots: narrate, follow and highlight a page (no OCR)

Text positions come from the DOM (`Range.getClientRects`, per character), and the browser draws camera and highlight
frame by frame (Playwright): pushing in re-rasterises at the new scale, so text stays sharp. Needs `./install.sh --web`.

```bash
reel words voice.mp3 --out words.json --lang en   # voice-over → per-word timing (local whisper.cpp); falls back to even spacing within subtitle cues
```
```json
{"id": "w1", "kind": "web", "duration": 28.8,
 "web": {"url": "https://example.com/article or article.html", "css": "optional CSS that hides pop-ups and navigation"},
 "camera": [{"op": "follow"}],
 "highlight": [{"text": "the exact sentence on the page", "sync": "voice", "style": "marker"}]}
```

- Layout width: vertical output defaults to 390 (phone layout), horizontal to 1280; `web.layout_width` overrides.
- `follow` keeps a whole text column in frame and moves only when the spoken line leaves the comfort zone
  (`comfort`, default ±0.28 of the frame height), with a cosine ease and a speed cap (`max_speed`, default 0.7 frame
  heights per second). `zoom_to` with `text` pushes in once to a passage.
- `highlight` styles: `marker`, `spotlight`, `both`.
- `TEXT_NOT_FOUND`: the page does not contain that text. `TEXT_NOT_IN_VOICE`: the voice-over does not say it.
- Save pages that need a login as HTML first. With `--where remote`, web shots are pre-rendered locally.

## Timeline tools (timeline / scrub / edit / preview --range)

All share one `compile_plan` result; ranges are half-open `[start, end)` in output frames.

```bash
reel timeline plan.json --format text [--range 12:20] [--detail summary|events|words]
reel timeline plan.json --png tl.png [--range 12:20] [--render final.mp4] [--beats beats.json]
reel scrub plan.json --cuts --render final.mp4 --overlays safe,caption,bbox --out frames/
reel scrub plan.json --at 3.2,7.5 --out frames/
reel edit plan.json --base r3 --op '{"op":"trim","id":"k8","duration":6.0}'
reel edit plan.json --op '[{"op":"insert","shot":{...},"after":"k2"},{"op":"trim","id":"k3b","duration":5.0}]'
#   ops: trim · move · insert · replace · set-transition · set-camera; applied in memory, validated and compiled,
#   written atomically only on success
reel history plan.json
reel undo plan.json --to r1                      # commits a new revision equal to r1; history is never deleted
reel preview plan.json --range 12:20 [--size 540x960] [--fps 15] [--audio mix|voice|none] --out p.mp4
```

Revisions live in `.reel-history/` next to the plan (one plan per folder; another plan in the same folder fails with
`HISTORY_MISMATCH`). A hand-edited plan shows as `rN*`, and `--base rN` then fails with `REV_CONFLICT`.

## Delivery gate (check / deliver / register / receipt)

```bash
reel register brief.json                          # validate the brief and hand its delivery folder to the Bash guard
reel check final.mp4 [--plan plan.json] [--brief brief.json] [--profile default|vertical-strict|project.json] \
          [--reviews reviews.json] [--subs x.vtt]... [--text caption.txt]... [--report check.json]
reel deliver final.mp4 --brief brief.json --version v3 [same options as check] [--sample]
reel receipt <project>/.reel/receipts/<name>-v3.json   # the delivered file still exists and its hash is unchanged
```

```json
{"version": "reel.reviews.v1", "records": [
 {"video_sha256": "<video_sha256 from the check report>", "check": "low_motion", "decision": "accept", "by": "user",
  "note": "the slow title push is intended", "observations": [{"t": [0, 4.2], "note": "title card, slow push"}]},
 {"video_sha256": "<video_sha256 from the check report>", "check": "final_watch", "decision": "accept", "by": "claude",
  "note": "watched in full, compared with the storyboard",
  "observations": [{"t": 0, "note": "first frame has the title"}, {"t": [12, 18], "note": "evidence shots match the voice-over"}]},
 {"video_sha256": "<video_sha256 from the check report>", "check": "final_listen", "decision": "accept", "by": "user",
  "note": "listened from start to end", "quote": "Sounds good, post it"}]}
```

- **Failure semantics:** `reel check` exits 0 only when the gate passes. Otherwise `ok: false`, exit 1, `error.code` is
  `CHECK_FAILED` (`gate.blockers`) or `CHECK_NEEDS_REVIEW` (`gate.pending_review`), and `result` still holds the full
  report. Blocking: any fail; any detector error (missing ffmpeg, non-zero exit, event lines that do not parse, a run
  that never reached `progress=end`); a required check missing or `not_checked`; a review that rejects.
- **Checks:** `decode_clean`, `format` (codec, pixel format, size, frame rate, audio codec, sample rate, per profile),
  `first_frame`, `black_frames`, `freeze` (plan-aware), `long_freeze`, `low_motion` (4 s or more of near-stillness →
  needs_review), `audio_clipping`, `loudness`, `head_silence` / `tail_silence` / `mid_silence`, `subtitles_in_range`
  and `verify.*` (with `--plan`), `text_scan` (banned words in the brief, the plan's visible text, subtitles, `--subs`,
  `--text`). Not implemented and always `not_checked`, listed in `gate.not_covered`: `ocr_visible_text` (on-screen
  text), `caption_legibility`, `pin_occlusion`, `voice_visual_alignment`, `content_correspondence`.
- **Profiles** (`profiles/*.json`: `default`, `vertical-strict`; `extends` inherits, dictionaries merge, lists
  replace): `required`, thresholds and `action` (fail / needs_review) per check, `audio` (required / silent /
  optional), `text.banned / extra_banned / allow / require`, and `allow` (registered exceptions:
  `{check, t: [start, end], reason}`; when all offending evidence lies inside the range the item passes and is marked
  `excused`). `check` and `deliver` resolve the profile as `--profile` > `brief.profile` (relative to the brief) >
  `default`.
- **Review records** (`reel.reviews.v1`): each is bound to `video_sha256`; `decision` accept/reject; `by`; `note` of
  at least 4 characters. Ordinary records need `observations` (timecode + note). `final_listen` signed by a person
  needs `quote`; signed by an agent (`by` contains claude, agent, gpt, assistant, bot and so on) it needs
  `observations`. The receipt's `approved_by` says `agent` or `human`. Records bound to another hash are void
  (`stale_reviews`); incomplete ones do not count (`invalid_reviews`). One reject outweighs any number of accepts.
- **Brief** (`reel.brief.v1`): `name`, `language`, `deliverable`, `delivery_root`, `must_not_change`, `voice`
  (`tts` / `clone` need `approved`: the same voice and the audition sample's sha256; a human approval needs `quote`,
  an agent approval needs `stt` evidence written by `reel voice check sample --script text --brief brief.json`, and
  the gate recomputes the error rate (≤ 10%) from the stored transcript instead of trusting a reported number).
  Missing or too-broad delivery folder → nothing can be delivered; anything else missing → only `--sample`.
- **Deliver:** re-checks the actual file every time (there is no `--report` input) → technical failure: not even a
  sample (`DELIVERY_BLOCKED`) → final conditions unmet without `--sample`: `DELIVERY_NOT_FINAL` → copy to a temporary
  file in the delivery folder, verify sha256, hard-link into place without overwriting (`COPY_MISMATCH` /
  `VERSION_EXISTS`) → receipt `<project>/.reel/receipts/<name>-<version>.json`, log
  `$REEL_STATE_DIR/deliveries.jsonl` and registry `registry.json` (default `~/.local/state/reel/`). A sample delivered
  earlier can be promoted to final with the same version number, only for the identical file. The receipt states only
  "copied and hash-verified"; it never claims the user watched it.

## Hooks

- `hooks/guard_delivery.py` (PreToolUse, Bash): guards only the delivery folders registered with `reel register`.
  Writes into them (`cp`/`mv`/`rsync`/`ditto`/`ln`/`install`, ffmpeg outputs, redirections, `tee`, `rm`/`touch`/…,
  `reel … --out`) are denied; `reel deliver` is allowed; reading is allowed. Commands it cannot analyse are denied
  only when they mention a registered folder. With no registered task it does nothing; on its own error it allows.
- `hooks/deliver_gate.py` (Stop): when the turn produced video (ffmpeg / melt / `reel render` / render scripts with a
  video file) or the session is inside a registered project, a reply sentence that claims the video is finished or
  delivered needs a valid `status: final` receipt from this turn (or the project); otherwise the stop is blocked with
  an explanation. Honest status words in the same or next sentence ("sample pending review", "not delivered",
  "no video delivered this turn", or their Chinese equivalents) pass. It blocks at most 5 times per turn.

## Beats

```bash
reel beats music.mp3 --out beats.json            # beats / downbeats / BPM; auto: beat_this when installed, else librosa
reel snap plan.json --beats beats.json --mode downbeat --tolerance 0.3   # move cuts onto downbeats, falling back to beats
reel undo plan.json --to r0                      # snapping is written through edit, so it can be undone
```

Modes: `beat`, `downbeat`, `every:N`, `half`; `--offset` is where the music starts on the timeline. Snapping never
breaks a shot: durations must still hold camera moves, manual highlights, transitions, observation times and the
remaining source. `./install.sh --beats` installs librosa; `--beats-ml` adds beat_this (its model checkpoint, about
80 MB, downloads on first use). The beat worker runs in the reel venv (`$REEL_VENV`, default
`~/.local/share/reel/venv`, or `REEL_VENV_PY`).

## Music videos (advanced)

The engine also has a lyrics timing source (`reel lyrics import / verify`) and an acceptance contract for music
videos (`reel check / deliver --contract mv.json`, brief `"kind": "mv"`): song identity and level against the
original track, lyrics on screen and not early, output format. The contract format is documented at the top of
`reelcore/mvcheck.py` and `reelcore/lyrics.py`. A step-by-step music-video handbook is not part of this repository.

## Shotcut projects

```bash
reel mlt project.mlt --video-only --range 0:60    # text timeline: tracks, clips, in/out points, filters
reel mlt project.mlt --dupes                      # the same source range of a video used twice
```

Read-only. A 0-byte cloud placeholder file is reported explicitly.

## Remote GPU (`remote.py`)

- Host: `REEL_REMOTE` (one ssh destination) or `REEL_REMOTES` (comma-separated, tried in order). Nothing is
  hard-coded; with neither set, remote features report `REMOTE_OFFLINE` with a configuration hint and `auto` stays
  local.
- VRAM is capped at 80% by default; above that `NEED_USER_APPROVAL` (ask the user, then `--allow-over-80`). GPU
  utilisation ≥ 50% → `GPU_BUSY`.
- A remote `flock` on `~/reel-remote/gpu.lock` allows one reel GPU job at a time; VRAM is re-checked after the lock.
- Each job carries its own copy of `reelcore` (`job/code/`); job folders `~/reel-remote/jobs/<time>-<id>/` are not
  deleted automatically. The result is pulled back and verified again locally.
- `reel remote setup-vision` creates `~/reel-remote/venv-$(hostname -s)` with torch (CUDA 12.8 wheels), transformers
  and the OWLv2 weights; it touches no other environment. `reel remote probe` reports host and GPU state. reel never
  reboots or shuts down a host.

## Environment variables

| Variable | Default | Used for |
|---|---|---|
| `REEL_VENV` | `~/.local/share/reel/venv` | Python environment used by `bin/reel`, the beat worker and the example |
| `REEL_PYTHON` | - | explicit interpreter for `bin/reel` |
| `REEL_STATE_DIR` | `~/.local/state/reel` | registry, delivery log, Stop-hook counter |
| `REEL_REMOTE`, `REEL_REMOTES` | - | optional remote GPU host(s) |
| `REEL_WHISPER_MODEL` | `~/models/whisper/ggml-large-v3-turbo.bin` | whisper.cpp model for `reel words` / `reel voice check` |
| `REEL_SUB_FONT` | PingFang SC (mpv) / Noto Sans CJK SC (libass) | subtitle font; for published videos choose a font whose licence you have checked |

## Tests

```bash
tests/run_all.sh            # every suite with the reel venv; optional-extra tests are reported as SKIPPED
python3 tests/test_gate.py  # delivery gate fault-injection matrix (about 2 minutes)
python3 tests/test_hooks.py # Bash guard and Stop hook fault injection
```

`test_reelcore.py` (geometry, tracking expressions vs Python trajectories frame by frame, video in-points, special
characters in paths, end-to-end render), `test_timeline.py`, `test_mlt.py`, `test_mv.py`, `test_lyrics_norm.py`,
`test_beats.py` (needs `--beats`). Every media file is synthesized with ffmpeg or Pillow; no network.
