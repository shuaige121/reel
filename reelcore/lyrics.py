"""Lyrics timing source: reel lyrics import / verify.

Timing source order: (1) the lyrics stream embedded in the song file (a Suno .m4a download carries mov_text); (2) an external lyrics timeline via --srt; if neither exists, LYRICS_NO_TIMING.
There is no fallback to bare whisper (whisper systematically starts sentences early and can hallucinate); whisper per-word results are used only for cross-checking (--words).

Comparison is a global alignment by "order + repeat count" (Needleman-Wunsch, per line), not a set comparison: a skipped second chorus or two swapped sections are both reported.
- missing: in the lyrics source but not sung (absent from the timeline): fail
- extra: sung but not in the lyrics source and not similar to any line: fail
- sung_differs: the line text at the same position differs (e.g. words changed in Suno): needs_review; by default the picture shows the version actually sung
  (on-screen lyrics follow what is actually sung and the differences are listed for one confirmation); --display lyrics switches back to the source
- timing: monotonic, non-overlapping (up to 50 ms allowed), inside the song, 0.3-15 s per line; otherwise fail
- cross-check (with --words): the difference between each line start and the whisper anchor; lines off by more than 0.5 s are listed as needs_review, with no timing change and no line removed
Precision: embedded/external timelines only go down to the line. precision is always recorded as "line"; downstream must not claim per-word alignment (word-level animation is interpolation).
"""
import json, os, re, subprocess, unicodedata
from .errors import ReelError
from . import check

TOL_OVERLAP = 0.05
LINE_MIN, LINE_MAX = 0.3, 15.0
XCHECK_LINE_S, XCHECK_MEDIAN_S = 0.5, 0.25


def norm(s):
    """For comparison: keep letters, digits and combining marks of any language (Thai vowels and tone marks are combining marks) and remove punctuation and whitespace."""
    text = unicodedata.normalize("NFC", s or "")
    return "".join(c for c in text if unicodedata.category(c)[0] in "LMN").lower()


def _ts(s):
    h, m, rest = s.strip().split(":")
    sec, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def parse_srt(text):
    """→ [(t0, t1, text)]，including section marker lines such as [Verse] (text starting with "[")."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        rows = [r for r in block.strip().splitlines() if r.strip()]
        k = next((i for i, r in enumerate(rows) if "-->" in r), None)
        if k is None:
            continue
        a, b = rows[k].split("-->")
        out.append((_ts(a), _ts(b), " ".join(rows[k + 1:]).strip()))
    return out


def lyric_lines(path):
    """Lyrics source: for .md take the `## Lyrics` section; other text is read per line. Returns [(section, text)], skipping [End] and blank lines."""
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    m = re.search(r"^## Lyrics\s*$(.*?)(?=^## |\Z)", txt, re.M | re.S)
    body = m.group(1) if m else txt
    out, sec = [], None
    for raw in body.splitlines():
        l = raw.strip()
        if not l:
            continue
        mm = re.match(r"^\[(.+?)\]$", l)
        if mm:
            sec = mm.group(1)
            continue
        out.append((sec, l))
    return [x for x in out if (x[0] or "").lower() != "end"]


def subtitle_streams(song):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index,codec_name",
                        "-of", "json", song], capture_output=True, text=True)
    try:
        return json.loads(p.stdout).get("streams", [])
    except ValueError:
        return []


def embedded_srt(song, k=0):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", song, "-map", f"0:s:{k}", "-c:s", "srt", "-f", "srt", "-"],
                       capture_output=True, text=True)
    if p.returncode != 0 or "-->" not in p.stdout:
        raise ReelError("LYRICS_STREAM_UNREADABLE", "the embedded lyrics stream cannot be read", song=song, stream=k, stderr=p.stderr[-300:])
    return p.stdout


def _sim(a, b):
    """Similarity of two lines of text (0-1), by longest common subsequence of characters."""
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b):
            cur.append(prev[j] + 1 if ca == cb else max(prev[j + 1], cur[j]))
        prev = cur
    return 2 * prev[-1] / (len(a) + len(b))


def align(expected, sung):
    """Needleman-Wunsch：expected=[text] (source order, with repeats), sung=[text] (timeline order).
    → [(i or None, j or None, kind)]，kind ∈ same / differs / missing / extra。"""
    n, m = len(expected), len(sung)
    GAP = -1.0
    S = [[0.0] * (m + 1) for _ in range(n + 1)]
    T = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        S[i][0], T[i][0] = i * GAP, "u"
    for j in range(1, m + 1):
        S[0][j], T[0][j] = j * GAP, "l"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            s = _sim(expected[i - 1], sung[j - 1])
            match = 2.0 if s == 1.0 else (2 * s - 0.6 if s >= 0.5 else -2.0)
            best = max((S[i - 1][j - 1] + match, "d"), (S[i - 1][j] + GAP, "u"), (S[i][j - 1] + GAP, "l"))
            S[i][j], T[i][j] = best
    out, i, j = [], n, m
    while i > 0 or j > 0:
        t = T[i][j]
        if t == "d":
            s = _sim(expected[i - 1], sung[j - 1])
            out.append((i - 1, j - 1, "same" if s == 1.0 else ("differs" if s >= 0.5 else "mismatch")))
            i, j = i - 1, j - 1
        elif t == "u":
            out.append((i - 1, None, "missing")); i -= 1
        else:
            out.append((None, j - 1, "extra")); j -= 1
    out.reverse()
    # a low-similarity diagonal pair is really one missing + one extra
    fixed = []
    for i, j, k in out:
        if k == "mismatch":
            fixed += [(i, None, "missing"), (None, j, "extra")]
        else:
            fixed.append((i, j, k))
    return fixed


def _words_starts(words_path, lines):
    """whisper per-word results give a start per line (a literal anchor independent of pinyin: the first two characters of the line, first occurring within +-3 s of the line's time window)."""
    with open(words_path, encoding="utf-8") as fh:
        w = json.load(fh)
    w = w.get("words", w) if isinstance(w, dict) else w
    chars = [(x.get("t0", x.get("start")), c) for x in w for c in (x.get("text") or x.get("word") or "") if norm(c)]
    out = []
    for ln in lines:
        head = norm(ln["text"])[:2]
        cand = [t for k, (t, c) in enumerate(chars) if t is not None and abs(t - ln["t0"]) <= 3
                and "".join(cc for _, cc in chars[k:k + 2]).lower() == head]
        out.append(min(cand, key=lambda t: abs(t - ln["t0"])) if cand else None)
    return out


def _apply_end_fixes(lines, path):
    """Line-end corrections: the end of a lyrics-stream line often runs over the instrumental that follows (Suno uses the next line's start as the end). Corrections may only move the end earlier, must carry evidence, and must match by cue number plus original text."""
    try:
        with open(path, encoding="utf-8") as fh:
            fixes = json.load(fh)
    except (OSError, ValueError) as e:
        raise ReelError("ARG_INVALID", f"cannot read the line-end correction file: {e}", path=path)
    if not isinstance(fixes, list):
        raise ReelError("ARG_INVALID", "the line-end correction file must be an array [{cue, text, t1, evidence}]", path=path)
    by_cue, applied = {ln["cue_id"]: ln for ln in lines}, []
    for f in fixes:
        ln = by_cue.get(f.get("cue")) if isinstance(f, dict) else None
        if ln is None or norm(f.get("text", "")) != norm(ln["text_sung"]):
            raise ReelError("LYRICS_END_FIX_INVALID", "the line-end correction does not match a line in the lyrics stream (both the cue number and the sung text must match)", fix=f)
        t1 = f.get("t1")
        if not isinstance(t1, (int, float)) or not (ln["t0"] + LINE_MIN <= t1 < ln["t1"]):
            raise ReelError("LYRICS_END_FIX_INVALID", "a line end can only move earlier and must not make the line shorter than the minimum line duration", fix=f,
                            t0=ln["t0"], t1_stream=ln["t1"], line_min=LINE_MIN)
        if not isinstance(f.get("evidence"), str) or len(f["evidence"].strip()) < 8:
            raise ReelError("LYRICS_END_FIX_INVALID", "a line-end correction must give evidence (per-word timing, onsets, song form, etc.)", fix=f)
        ln["t1_stream"], ln["t1"], ln["end_fix"] = ln["t1"], round(float(t1), 3), f["evidence"]
        applied.append({"cue": ln["cue_id"], "text": ln["text_sung"], "t1_stream": ln["t1_stream"], "t1": ln["t1"]})
    return applied


def build(song, lyrics_path, srt_path=None, words_path=None, display="sung", stream=0, end_fixes=None):
    if not os.path.exists(song):
        raise ReelError("ASSET_MISSING", "the song file does not exist", path=song)
    info, dur = check._probe(song)
    if srt_path:
        with open(srt_path, encoding="utf-8") as fh:
            raw, source = fh.read(), f"srt:{os.path.abspath(srt_path)}"
    else:
        if not subtitle_streams(song):
            raise ReelError("LYRICS_NO_TIMING", "the song has no embedded lyrics stream and no --srt was given; not falling back to whisper",
                            song=song, hint="a .m4a downloaded from Suno normally carries mov_text; for other sources supply a per-line timeline with --srt")
        raw, source = embedded_srt(song, stream), f"embedded:0:s:{stream}"
    cues, sec = [], None
    for t0, t1, text in parse_srt(raw):
        mm = re.match(r"^\[(.+?)\]$", text)
        if mm:
            sec = mm.group(1)
            continue
        if norm(text):
            cues.append({"section": sec, "text": text, "t0": round(t0, 3), "t1": round(t1, 3)})
    exp = lyric_lines(lyrics_path)
    pairs = align([t for _, t in exp], [c["text"] for c in cues])
    lines, missing, extra, differs, repeats = [], [], [], [], []
    for i, j, k in pairs:
        if k == "missing":
            missing.append({"line": i + 1, "text": exp[i][1]})
            continue
        if k == "extra":
            twin = next((ii for ii, (_, t) in enumerate(exp) if norm(t) == norm(cues[j]["text"])), None)
            if twin is None:
                extra.append({"cue": j + 1, "text": cues[j]["text"], "t": [cues[j]["t0"], cues[j]["t1"]]})
                continue
            # the singer repeated a line that is in the lyrics: show it again, but a human confirms it once
            repeats.append({"cue": j + 1, "lyrics_line": twin + 1, "text": cues[j]["text"], "t": [cues[j]["t0"], cues[j]["t1"]]})
            i, k = twin, "same"
        c = cues[j]
        rec = {"cue_id": j + 1, "lyrics_line": i + 1, "section": exp[i][0] or c["section"], "t0": c["t0"], "t1": c["t1"],
               "text_sung": c["text"], "text_lyrics": exp[i][1], "precision": "line"}
        rec["text"] = rec["text_sung"] if display == "sung" else rec["text_lyrics"]
        if k == "differs":
            differs.append({"cue": j + 1, "lyrics_line": i + 1, "lyrics": exp[i][1], "sung": c["text"]})
        lines.append(rec)
    applied = _apply_end_fixes(lines, end_fixes) if end_fixes else []
    occ = {}
    for ln in lines:
        key = norm(ln["text"]); occ[key] = occ.get(key, 0) + 1; ln["occurrence"] = occ[key]
    # timing checks
    tbad = []
    for a, b in zip(lines, lines[1:]):
        if b["t0"] < a["t0"]:
            tbad.append({"cue": b["cue_id"], "why": "start time out of order", "t": b["t0"]})
        elif b["t0"] < a["t1"] - TOL_OVERLAP:
            tbad.append({"cue": b["cue_id"], "why": f"overlaps the previous line by {a['t1'] - b['t0']:.2f}s", "t": b["t0"]})
    for ln in lines:
        d = ln["t1"] - ln["t0"]
        if not (LINE_MIN <= d <= LINE_MAX):
            tbad.append({"cue": ln["cue_id"], "why": f"line duration {d:.2f}s is not within {LINE_MIN}–{LINE_MAX}s", "t": ln["t0"]})
        if ln["t0"] < 0 or ln["t1"] > dur + 0.05:
            tbad.append({"cue": ln["cue_id"], "why": "outside the song", "t": ln["t0"]})
    xc = None
    if words_path:
        st = _words_starts(words_path, lines)
        diffs = [(ln["cue_id"], round(ln["t0"] - s, 3)) for ln, s in zip(lines, st) if s is not None]
        far = [{"cue": c, "srt_minus_whisper_s": d} for c, d in diffs if abs(d) > XCHECK_LINE_S]
        med = sorted(d for _, d in diffs)[len(diffs) // 2] if diffs else None
        xc = {"anchored_lines": len(diffs), "of": len(lines), "median_s": med, "far_lines": far,
              "status": "needs_review" if (far or (med is not None and abs(med) > XCHECK_MEDIAN_S)) else "pass",
              "note": "whisper is only cross-evidence; lines with a large difference need a human to listen, and timing is not changed automatically"}
    checks = {
        "coverage": {"status": "fail" if (missing or extra) else "pass", "missing": missing, "extra": extra},
        "sung_differs": {"status": "needs_review" if differs else "pass", "lines": differs, "display": display},
        "repeats": {"status": "needs_review" if repeats else "pass", "lines": repeats,
                    "note": "a line sung twice but written once in the lyrics source: the picture shows it again as sung; confirm once whether this is intended"},
        "timing": {"status": "fail" if tbad else "pass", "problems": tbad},
        "cross_check": xc or {"status": "not_checked", "note": "--words was not given"},
    }
    sts = [v["status"] for v in checks.values()]
    status = "fail" if "fail" in sts else ("needs_review" if "needs_review" in sts else "pass")
    return {"version": "reel.lyrics.v1", "song": {"path": os.path.abspath(song), "sha256": check.sha256(song),
            "duration_s": round(dur, 3)}, "lyrics_file": {"path": os.path.abspath(lyrics_path),
            "sha256": check.sha256(lyrics_path)}, "source": source, "precision": "line", "display": display,
            "end_fixes": {"path": os.path.abspath(end_fixes), "sha256": check.sha256(end_fixes), "applied": applied}
            if end_fixes else None, "status": status, "checks": checks, "lines": lines}


def import_(song, lyrics_path, out, srt=None, words=None, display="sung", stream=0, end_fixes=None):
    r = build(song, lyrics_path, srt, words, display, stream, end_fixes)
    if r["status"] == "fail":  # never leave a failed timing file around for the next step to pick up
        if os.path.exists(out):
            os.remove(out)
        raise ReelError("LYRICS_CHECK_FAILED", "the lyrics timeline is not acceptable; lyrics.json was not written",
                        coverage=r["checks"]["coverage"], timing=r["checks"]["timing"])
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False, indent=1)
    return {"out": out, "status": r["status"], "lines": len(r["lines"]), "source": r["source"],
            "checks": {k: v["status"] for k, v in r["checks"].items()},
            "sung_differs": r["checks"]["sung_differs"]["lines"], "repeats": r["checks"]["repeats"]["lines"],
            "end_fixes": (r["end_fixes"] or {}).get("applied", [])}


def verify(path, song=None, lyrics_path=None):
    """lyrics.json still matches the current song and lyrics source (hash unchanged), and the structure and timing checks are re-run."""
    with open(path, encoding="utf-8") as fh:
        r = json.load(fh)
    if r.get("version") != "reel.lyrics.v1":
        raise ReelError("LYRICS_INVALID", "not reel.lyrics.v1", path=path)
    stale = []
    fx = (r.get("end_fixes") or {}).get("path")
    for key, p in (("song", song or r["song"]["path"]), ("lyrics_file", lyrics_path or r["lyrics_file"]["path"]))\
            + ((("end_fixes", fx),) if fx else ()):
        if not os.path.exists(p) or check.sha256(p) != r[key]["sha256"]:
            stale.append({"which": key, "path": p})
    if stale:
        raise ReelError("LYRICS_STALE", "the song or lyrics source changed (or is missing); lyrics.json is void, import again", stale=stale)
    src = r["source"]
    fresh = build(r["song"]["path"], r["lyrics_file"]["path"], src[4:] if src.startswith("srt:") else None,
                  None, r.get("display", "sung"), int(src.rsplit(":", 1)[1]) if src.startswith("embedded:") else 0, fx)
    same = [(a["t0"], a["t1"], a["text"]) for a in fresh["lines"]] == [(a["t0"], a["t1"], a["text"]) for a in r["lines"]]
    if not same:
        raise ReelError("LYRICS_TAMPERED", "the lines in lyrics.json differ from a fresh import (edited by hand?)", path=path)
    if fresh["status"] == "fail":
        raise ReelError("LYRICS_CHECK_FAILED", "the lyrics timeline is not acceptable", checks=fresh["checks"])
    return {"path": path, "status": fresh["status"], "lines": len(fresh["lines"]),
            "checks": {k: v["status"] for k, v in fresh["checks"].items()}}
