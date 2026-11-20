#!/usr/bin/env bash
# End-to-end example with local tools only (no network, no paid service):
#   brief → stand-in media → observe a subject → plan → validate/compile → render → reel check → reel deliver → receipt
#
# Usage: examples/photo-reel/run.sh [WORK_DIR]
#   WORK_DIR defaults to ~/reel-example/project. The delivery folder is ~/reel-example/delivered (set in brief.json).
#   The repository is never written to.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO="$(cd "$HERE/../.." && pwd -P)"
reel() { "$REPO/bin/reel" "$@"; }
PY="${REEL_PYTHON:-}"
if [ -z "$PY" ]; then
  V="${REEL_VENV:-$HOME/.local/share/reel/venv}"
  if [ -x "$V/bin/python" ]; then PY="$V/bin/python"; else PY=python3; fi
fi
WORK="${1:-$HOME/reel-example/project}"
DELIVERED="$HOME/reel-example/delivered"
step() { printf '\n== %s\n' "$*"; }
field() { "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1], {}, {"d": d}))' "$1"; }

command -v ffmpeg >/dev/null || { echo "ffmpeg not found (macOS: brew install ffmpeg)" >&2; exit 1; }
mkdir -p "$WORK/media" "$WORK/preview" "$WORK/out" "$DELIVERED"
cp "$HERE/brief.json" "$HERE/plan.json" "$HERE/caption.txt" "$WORK/"
rm -f "$WORK/observations.json"            # regenerated below, so ids stay obs-1 / obs-2
cd "$WORK"

step "1. brief: register the delivery folder (the Bash guard now protects it)"
reel register brief.json | field 'd["result"]["brief"]'

step "2. stand-in media (placeholders: swap in your own photos and music later)"
"$PY" "$HERE/make_media.py" media

step "3. inspect the assets"
reel inspect media/card-1.png media/card-2.png media/card-3.png media/card-4.png media/card-5.png media/music.wav \
  | field '"; ".join(" ".join(str(a.get(k)) for k in ("kind", "width", "height", "duration")) for a in d["result"]["assets"])'

step "4. look and register targets (Claude reads the grid image; here the box comes from make_media.py)"
reel frame media/card-1.png --out preview/card-1-grid.png | field 'd["result"]["png"]'
for card in card-1.png card-3.png; do
  box="$("$PY" -c 'import json,sys; print(",".join(map(str, json.load(open("media/targets.json"))[sys.argv[1]])))' "$card")"
  reel observe --obs observations.json --src media --asset "$card" --bbox "$box" --label subject \
    | field 'd["result"]["observation"]["id"] + " " + d["result"]["observation"]["asset"] + " " + str(d["result"]["observation"]["bbox"])'
done

step "5. validate and compile the plan (frame-exact timeline, nothing rendered yet)"
reel validate plan.json | field '"valid, shots: " + str(d["result"]["shots"])'
reel compile plan.json | field '"visual " + str(d["result"]["visual_s"]) + " s, audio " + str(d["result"]["voice_s"]) + " s"'

step "6. render (local CPU) and verify frames, duration and decoding"
reel render plan.json --out out/reel-example.mp4 --sheet preview/final.png --where local \
  | field '"ok=" + str(d["ok"]) + " " + d["result"]["where"] + " checks passed: " + str(sum(c["ok"] for c in d["result"]["checks"])) + "/" + str(len(d["result"]["checks"]))'

step "7. reel check: technical gate + banned-word scan of brief, plan and caption"
set +e
reel check out/reel-example.mp4 --plan plan.json --brief brief.json --text caption.txt --report out/check.json > out/check-stdout.json
rc=$?
set -e
field '"gate=" + d["result"]["gate"]["status"] + " counts=" + str(d["result"]["counts"]) + " not_covered=" + str(d["result"]["gate"]["not_covered"])' < out/check-stdout.json
if [ "$rc" != 0 ]; then
  echo "reel check did not pass; see out/check.json (gate.blockers / gate.pending_review)" >&2
  exit 1
fi

step "8. reel deliver: re-check, copy, verify the hash, write the receipt"
n=1
while [ -e "$WORK/.reel/receipts/reel-example-v$n.json" ]; do n=$((n + 1)); done
# --sample: nobody has watched or listened to this cut yet, so it is delivered as a sample pending review.
# To deliver it as final, write reviews.json after you watch and listen (see examples/photo-reel/README.md).
reel deliver out/reel-example.mp4 --brief brief.json --version "v$n" --plan plan.json --text caption.txt --sample > out/deliver.json
field '"status=" + d["result"]["status"] + "\ndelivered: " + d["result"]["delivered"] + "\nreceipt:   " + d["result"]["receipt"]' < out/deliver.json
receipt="$(field 'd["result"]["receipt"]' < out/deliver.json)"

step "9. reel receipt: the delivered file is still there and unchanged"
reel receipt "$receipt" | field '"ok=" + str(d["ok"]) + " status=" + d["result"]["status"]'

printf '\nDone. Contact sheet: %s\nTo stop guarding the example delivery folder: (cd %s && %s/bin/reel register brief.json --remove)\n' \
  "$WORK/preview/final.png" "$WORK" "$REPO"
