# Example: photo reel (no paid service, no network)

A 14.4-second vertical video (1080x1920, 30 fps) from five photo cards and a music track, taken through the whole
reel pipeline on your own machine:

brief → stand-in media → register subjects → plan → validate / compile → render → `reel check` → `reel deliver` →
receipt.

```bash
./install.sh                      # once, from the repository root
examples/photo-reel/run.sh        # work folder: ~/reel-example/project; deliveries: ~/reel-example/delivered
```

It needs only ffmpeg and the core install (numpy, Pillow). Nothing is downloaded and no API is called. The script
never writes into the repository.

## The media are placeholders

`make_media.py` generates five 1600x1200 cards labelled "PLACEHOLDER n/5" (a gradient, a grid and a round "subject"
to zoom in on) and a synthesized 100 BPM chord track loudness-normalised to about -14 LUFS. They exist so the example
runs anywhere. To make it yours:

1. Replace `media/card-1.png` … `card-5.png` in the work folder with your own photos (or change the `asset` names in
   `plan.json`), and `media/music.wav` with your own music or voice-over.
2. Re-register the subjects: run `reel frame media/card-1.png --out preview/card-1-grid.png`, read the box of what you
   want to push in on, and register it with `reel observe` (or let Claude do this with the skill). Remove the old
   `observations.json` first so the ids stay `obs-1` and `obs-2`, or update the ids in `plan.json`.
3. If your track is not 14.4 s long, `fit_last_shot` stretches or shortens the last shot; keep its camera move inside
   the new duration, or change the shot durations.

## Files

| File | What it is |
|---|---|
| `brief.json` | The brief: name, language, final or sample, delivery folder (`~/reel-example/delivered`), what must not change, voice mode, profile |
| `plan.json` | EditPlan v1: five shots showing `zoom_to` on a registered subject, a `move_to` pan, a still shot (`camera: []`) and a pull-back end card, with fade transitions |
| `caption.txt` | A post caption, scanned for banned words by `reel check --text` |
| `make_media.py` | Generates the placeholder cards, the music track and `targets.json` (the subject boxes the script registers) |
| `run.sh` | Runs every step and prints a one-line summary per step |

## What you get

- `~/reel-example/project/out/reel-example.mp4`: the render; `out/check.json`: the full check report;
  `preview/final.png`: a contact sheet.
- `~/reel-example/delivered/reel-example-v1-sample-pending-review.mp4`: the delivered copy, hash-verified.
- `~/reel-example/project/.reel/receipts/reel-example-v1.json`: the receipt (hashes of the video, brief, profile,
  plan and report).

Each run delivers the next free version number (v1, v2, ...); old versions are never overwritten.

## Why it is delivered as a sample

`run.sh` delivers with `--sample` because nobody has watched or listened to the cut yet: a final delivery needs a
`final_watch` and a `final_listen` review record bound to the video's sha256. After you have watched and listened to
`out/reel-example.mp4`, write `reviews.json` in the work folder, using the `video_sha256` value from `out/check.json`:

```json
{"version": "reel.reviews.v1", "records": [
 {"video_sha256": "<video_sha256 from out/check.json>", "check": "final_watch", "decision": "accept", "by": "me",
  "note": "watched the whole cut", "observations": [{"t": 0, "note": "first card visible"}, {"t": [11, 14.4], "note": "end card readable"}]},
 {"video_sha256": "<video_sha256 from out/check.json>", "check": "final_listen", "decision": "accept", "by": "me",
  "note": "listened from start to end", "quote": "Good to go"}]}
```

Then promote the same version to final (only the identical file is accepted):

```bash
cd ~/reel-example/project
reel deliver out/reel-example.mp4 --brief brief.json --version v1 --plan plan.json --text caption.txt --reviews reviews.json
```

With the skill installed, Claude does this itself: it watches and listens, signs the records with timecoded
observations, and the receipt then says `approved_by: agent`.

## Clean up

`reel register` put the example's delivery folder under the Bash guard. To release it:

```bash
cd ~/reel-example/project && reel register brief.json --remove
```

Then delete `~/reel-example` if you no longer need it.
