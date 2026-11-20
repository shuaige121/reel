"""歌词时间源：reel lyrics import / verify。

时间源顺序：① 歌曲文件内嵌的歌词流（Suno 下载的 .m4a 带 mov_text）；② --srt 外部歌词时间轴；都没有 → LYRICS_NO_TIMING。
不回退到裸 whisper（whisper 句首系统性偏早、会幻觉）；whisper 逐字结果只用来交叉校验（--words）。

比对按「顺序 + 重复次数」做全局对齐（Needleman-Wunsch，按行），不是集合比较：漏唱第二遍副歌、两段顺序颠倒都会报出来。
- missing：歌词原件有、没唱（时间轴里没有）→ fail
- extra：唱了、歌词原件里没有，也不像任何一行 → fail
- sung_differs：同一位置的行文字不同（例如在 Suno 里改了词）→ needs_review，默认画面显示「实际演唱」的版本
  （画面歌词以实际唱的为准，差异列出来确认一次）；--display lyrics 可改回原件
- 时间：单调、不重叠（允许 ≤50 ms）、在歌曲范围内、每行 0.3–15 s → 否则 fail
- 交叉校验（给 --words）：每行起点与 whisper 锚点的差；差 >0.5 s 的行列为 needs_review，不改时间、不删行
精度：内嵌/外部时间轴只到「行」。precision 一律记 "line"，下游不得宣称逐字对齐（字级动画是插值）。
"""
import json, os, re, subprocess, unicodedata
from .errors import ReelError
from . import check

TOL_OVERLAP = 0.05
LINE_MIN, LINE_MAX = 0.3, 15.0
XCHECK_LINE_S, XCHECK_MEDIAN_S = 0.5, 0.25


def norm(s):
    """比对用：保留任何语言的字母、数字和组合符号（泰文元音、声调是组合符号），去掉标点和空白。"""
    text = unicodedata.normalize("NFC", s or "")
    return "".join(c for c in text if unicodedata.category(c)[0] in "LMN").lower()


def _ts(s):
    h, m, rest = s.strip().split(":")
    sec, ms = rest.replace(".", ",").split(",")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def parse_srt(text):
    """→ [(t0, t1, text)]，含 [Verse] 这类段落标记行（text 以 [ 开头）。"""
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
    """歌词原件：.md 取 `## Lyrics` 一节；其他文本按行。返回 [(section, text)]，跳过 [End] 和空行。"""
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
        raise ReelError("LYRICS_STREAM_UNREADABLE", "内嵌歌词流读不出来", song=song, stream=k, stderr=p.stderr[-300:])
    return p.stdout


def _sim(a, b):
    """两行文字的相似度（0–1），按字符的最长公共子序列。"""
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
    """Needleman-Wunsch：expected=[text]（原件顺序，含重复），sung=[text]（时间轴顺序）。
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
    """whisper 逐字 → 每行起点（拼音无关的字面锚点：行首两字在该行时间窗 ±3 s 内第一次出现）。"""
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
    """行尾更正：歌词流的行尾常常跨过后面的器乐（Suno 把下一句起点当行尾）。只许提前、必须附证据、按 cue 号+原文对上。"""
    try:
        with open(path, encoding="utf-8") as fh:
            fixes = json.load(fh)
    except (OSError, ValueError) as e:
        raise ReelError("ARG_INVALID", f"读不了行尾更正文件：{e}", path=path)
    if not isinstance(fixes, list):
        raise ReelError("ARG_INVALID", "行尾更正文件必须是数组 [{cue, text, t1, evidence}]", path=path)
    by_cue, applied = {ln["cue_id"]: ln for ln in lines}, []
    for f in fixes:
        ln = by_cue.get(f.get("cue")) if isinstance(f, dict) else None
        if ln is None or norm(f.get("text", "")) != norm(ln["text_sung"]):
            raise ReelError("LYRICS_END_FIX_INVALID", "行尾更正对不上歌词流里的行（cue 号和唱的原文都要对）", fix=f)
        t1 = f.get("t1")
        if not isinstance(t1, (int, float)) or not (ln["t0"] + LINE_MIN <= t1 < ln["t1"]):
            raise ReelError("LYRICS_END_FIX_INVALID", "行尾只能提前，且不能短于最短行时长", fix=f,
                            t0=ln["t0"], t1_stream=ln["t1"], line_min=LINE_MIN)
        if not isinstance(f.get("evidence"), str) or len(f["evidence"].strip()) < 8:
            raise ReelError("LYRICS_END_FIX_INVALID", "行尾更正必须写证据（逐字时间、起音、曲式等）", fix=f)
        ln["t1_stream"], ln["t1"], ln["end_fix"] = ln["t1"], round(float(t1), 3), f["evidence"]
        applied.append({"cue": ln["cue_id"], "text": ln["text_sung"], "t1_stream": ln["t1_stream"], "t1": ln["t1"]})
    return applied


def build(song, lyrics_path, srt_path=None, words_path=None, display="sung", stream=0, end_fixes=None):
    if not os.path.exists(song):
        raise ReelError("ASSET_MISSING", "歌曲文件不存在", path=song)
    info, dur = check._probe(song)
    if srt_path:
        with open(srt_path, encoding="utf-8") as fh:
            raw, source = fh.read(), f"srt:{os.path.abspath(srt_path)}"
    else:
        if not subtitle_streams(song):
            raise ReelError("LYRICS_NO_TIMING", "歌曲里没有内嵌歌词流，也没给 --srt；不回退到 whisper",
                            song=song, hint="Suno 下载的 .m4a 一般带 mov_text；别的来源用 --srt 提供逐行时间轴")
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
            tbad.append({"cue": b["cue_id"], "why": "起点倒序", "t": b["t0"]})
        elif b["t0"] < a["t1"] - TOL_OVERLAP:
            tbad.append({"cue": b["cue_id"], "why": f"与上一行重叠 {a['t1'] - b['t0']:.2f}s", "t": b["t0"]})
    for ln in lines:
        d = ln["t1"] - ln["t0"]
        if not (LINE_MIN <= d <= LINE_MAX):
            tbad.append({"cue": ln["cue_id"], "why": f"行时长 {d:.2f}s 不在 {LINE_MIN}–{LINE_MAX}s", "t": ln["t0"]})
        if ln["t0"] < 0 or ln["t1"] > dur + 0.05:
            tbad.append({"cue": ln["cue_id"], "why": "超出歌曲范围", "t": ln["t0"]})
    xc = None
    if words_path:
        st = _words_starts(words_path, lines)
        diffs = [(ln["cue_id"], round(ln["t0"] - s, 3)) for ln, s in zip(lines, st) if s is not None]
        far = [{"cue": c, "srt_minus_whisper_s": d} for c, d in diffs if abs(d) > XCHECK_LINE_S]
        med = sorted(d for _, d in diffs)[len(diffs) // 2] if diffs else None
        xc = {"anchored_lines": len(diffs), "of": len(lines), "median_s": med, "far_lines": far,
              "status": "needs_review" if (far or (med is not None and abs(med) > XCHECK_MEDIAN_S)) else "pass",
              "note": "whisper 只作交叉证据；差异大的行要人听，不自动改时间"}
    checks = {
        "coverage": {"status": "fail" if (missing or extra) else "pass", "missing": missing, "extra": extra},
        "sung_differs": {"status": "needs_review" if differs else "pass", "lines": differs, "display": display},
        "repeats": {"status": "needs_review" if repeats else "pass", "lines": repeats,
                    "note": "唱了两遍、歌词原件只写一遍的行：画面照唱的再显示一次，确认一次是不是有意的"},
        "timing": {"status": "fail" if tbad else "pass", "problems": tbad},
        "cross_check": xc or {"status": "not_checked", "note": "没给 --words"},
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
        raise ReelError("LYRICS_CHECK_FAILED", "歌词时间轴不合格，没有写出 lyrics.json",
                        coverage=r["checks"]["coverage"], timing=r["checks"]["timing"])
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False, indent=1)
    return {"out": out, "status": r["status"], "lines": len(r["lines"]), "source": r["source"],
            "checks": {k: v["status"] for k, v in r["checks"].items()},
            "sung_differs": r["checks"]["sung_differs"]["lines"], "repeats": r["checks"]["repeats"]["lines"],
            "end_fixes": (r["end_fixes"] or {}).get("applied", [])}


def verify(path, song=None, lyrics_path=None):
    """lyrics.json 仍对应当前的歌和歌词原件（hash 没变），并重跑结构与时间检查。"""
    with open(path, encoding="utf-8") as fh:
        r = json.load(fh)
    if r.get("version") != "reel.lyrics.v1":
        raise ReelError("LYRICS_INVALID", "不是 reel.lyrics.v1", path=path)
    stale = []
    fx = (r.get("end_fixes") or {}).get("path")
    for key, p in (("song", song or r["song"]["path"]), ("lyrics_file", lyrics_path or r["lyrics_file"]["path"]))\
            + ((("end_fixes", fx),) if fx else ()):
        if not os.path.exists(p) or check.sha256(p) != r[key]["sha256"]:
            stale.append({"which": key, "path": p})
    if stale:
        raise ReelError("LYRICS_STALE", "歌或歌词原件变了（或找不到），lyrics.json 作废，重新 import", stale=stale)
    src = r["source"]
    fresh = build(r["song"]["path"], r["lyrics_file"]["path"], src[4:] if src.startswith("srt:") else None,
                  None, r.get("display", "sung"), int(src.rsplit(":", 1)[1]) if src.startswith("embedded:") else 0, fx)
    same = [(a["t0"], a["t1"], a["text"]) for a in fresh["lines"]] == [(a["t0"], a["t1"], a["text"]) for a in r["lines"]]
    if not same:
        raise ReelError("LYRICS_TAMPERED", "lyrics.json 的行与重新导入的结果不一致（被手改过？）", path=path)
    if fresh["status"] == "fail":
        raise ReelError("LYRICS_CHECK_FAILED", "歌词时间轴不合格", checks=fresh["checks"])
    return {"path": path, "status": fresh["status"], "lines": len(fresh["lines"]),
            "checks": {k: v["status"] for k, v in fresh["checks"].items()}}
