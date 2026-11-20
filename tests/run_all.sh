#!/usr/bin/env bash
# Run the whole test suite with the reel venv (no pytest needed). Needs ffmpeg and, for subtitle burn-in, mpv
# (or an ffmpeg built with libass). Tests for optional extras are skipped, and reported as skipped, when the extra
# is not installed.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
PY="${REEL_PYTHON:-}"
if [ -z "$PY" ]; then
  V="${REEL_VENV:-$HOME/.local/share/reel/venv}"
  if [ -x "$V/bin/python" ]; then PY="$V/bin/python"; else PY=python3; fi
fi
echo "python: $("$PY" --version 2>&1) ($PY)"
fail=0
for t in test_lyrics_norm test_mlt test_reelcore test_timeline test_mv test_gate test_hooks; do
  echo; echo "=== $t"
  "$PY" "$ROOT/tests/$t.py" || { echo "!!! $t FAILED"; fail=1; }
done
echo; echo "=== test_beats"
if "$PY" -c 'import librosa' 2>/dev/null; then
  "$PY" "$ROOT/tests/test_beats.py" || { echo "!!! test_beats FAILED"; fail=1; }
else
  echo "SKIPPED: librosa is not installed in the reel venv (./install.sh --beats)"
fi
echo
[ "$fail" = 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit "$fail"
