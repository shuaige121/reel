#!/usr/bin/env bash
# Install reel's Python environment (a private venv; system Python is not touched).
#
#   ./install.sh                 core: numpy + Pillow (enough for render / check / deliver and the example)
#   ./install.sh --beats         + librosa                      (reel beats / reel snap, librosa backend)
#   ./install.sh --beats-ml      + beat-this, torch, torchaudio (reel beats, beat_this backend; downloads its model on first use)
#   ./install.sh --web           + playwright + Chromium        (web shots: narrate and highlight a web page)
#   ./install.sh --vision        + torch, transformers, scipy   (reel locate: OWLv2 open-vocabulary detection, local CPU)
#   ./install.sh --zh-voice      + pypinyin                     (reel voice check for Chinese voice-overs)
#   ./install.sh --all           all of the above
#   ./install.sh --link          also symlink bin/reel into $REEL_BIN_DIR (default ~/.local/bin) for manual installs
#
# Environment: REEL_VENV (default ~/.local/share/reel/venv), REEL_BOOTSTRAP_PYTHON (interpreter used to create it).
# Everything here downloads free, open-source packages from PyPI; nothing calls a paid service.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
VENV="${REEL_VENV:-$HOME/.local/share/reel/venv}"
BEATS=0 BEATS_ML=0 WEB=0 VISION=0 ZH=0 LINK=0
for a in "$@"; do
  case "$a" in
    --beats) BEATS=1 ;;
    --beats-ml) BEATS=1; BEATS_ML=1 ;;
    --web) WEB=1 ;;
    --vision) VISION=1 ;;
    --zh-voice) ZH=1 ;;
    --all) BEATS=1; BEATS_ML=1; WEB=1; VISION=1; ZH=1 ;;
    --link) LINK=1 ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $a (see ./install.sh --help)" >&2; exit 2 ;;
  esac
done

pick_python() {
  if [ -n "${REEL_BOOTSTRAP_PYTHON:-}" ]; then echo "$REEL_BOOTSTRAP_PYTHON"; return; fi
  for c in python3.12 python3.13 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      echo "$c"; return
    fi
  done
  echo "no Python >= 3.10 found (tested with 3.12; on macOS: brew install python@3.12)" >&2
  exit 1
}

for tool in ffmpeg ffprobe; do
  command -v "$tool" >/dev/null 2>&1 || { echo "missing $tool: install it first (macOS: brew install ffmpeg)" >&2; exit 1; }
done
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -qw ass && ! command -v mpv >/dev/null 2>&1; then
  echo "note: subtitles need ffmpeg with libass or mpv (macOS: brew install mpv); the test suite uses them" >&2
fi

PY="$(pick_python)"
if [ ! -x "$VENV/bin/python" ]; then
  echo "creating venv $VENV with $("$PY" --version 2>&1)"
  mkdir -p "$(dirname "$VENV")"
  "$PY" -m venv "$VENV"
fi
VP="$VENV/bin/python"
"$VP" -m pip install --quiet --upgrade pip
"$VP" -m pip install --quiet -r "$ROOT/requirements.txt"
[ "$BEATS" = 1 ] && "$VP" -m pip install --quiet "librosa>=0.10"
[ "$BEATS_ML" = 1 ] && "$VP" -m pip install --quiet "beat-this>=1.0" torch torchaudio
[ "$VISION" = 1 ] && "$VP" -m pip install --quiet torch "transformers>=4.45" scipy
[ "$ZH" = 1 ] && "$VP" -m pip install --quiet pypinyin
if [ "$WEB" = 1 ]; then
  "$VP" -m pip install --quiet playwright
  "$VP" -m playwright install chromium
fi

if [ "$LINK" = 1 ]; then
  BIN="${REEL_BIN_DIR:-$HOME/.local/bin}"
  mkdir -p "$BIN"
  ln -sfn "$ROOT/bin/reel" "$BIN/reel"
  echo "linked $BIN/reel -> $ROOT/bin/reel"
  case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN is not on PATH; add it to your shell profile" >&2 ;; esac
fi

"$ROOT/bin/reel" --help >/dev/null
echo "reel is ready: venv $VENV ($("$VP" --version 2>&1)); try: $ROOT/examples/photo-reel/run.sh"
