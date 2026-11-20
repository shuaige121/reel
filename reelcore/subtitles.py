"""Subtitle events: parse VTT/SRT, split into short lines, write SRT (burned in by mpv) or ASS (burned in by libass). Both backends share one timing calculation."""
import re
from .errors import ReelError

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE = re.compile(_TS + r"\s*-->\s*" + _TS)
_TAG = re.compile(r"<[^>]+>")
TRAIL = "。，、！？；：…—　 ,.!?;:"


def _secs(h, m, s, frac):
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(frac.ljust(3, "0")) / 1000.0


def parse(path, allow_zero=False):
    """Parse VTT/SRT block by block → [(start, end, text)]。
    The file header and NOTE/STYLE/REGION blocks are skipped; every other block must have a parseable time line, otherwise it is an error (subtitles are never dropped silently)."""
    with open(path, encoding="utf-8-sig") as fh:
        blocks = [b for b in re.split(r"\r?\n\s*\r?\n", fh.read().strip()) if b.strip()]
    events, bad = [], []
    for i, blk in enumerate(blocks):
        lines = [ln.rstrip() for ln in blk.splitlines()]
        head = lines[0].strip()
        if i == 0 and head.startswith("WEBVTT"):
            continue
        if head.split()[0] in ("NOTE", "STYLE", "REGION"):
            continue
        tis = [k for k, ln in enumerate(lines) if "-->" in ln]
        if not tis or tis[0] > 1:
            bad.append(head[:60]); continue
        if len(tis) > 1:                  # blank line missing between two cues: the index and time must not end up in the text
            bad.append(f"missing blank line: {lines[tis[1]][:50]}"); continue
        ti = tis[0]
        m = _CUE.search(lines[ti])
        if not m:
            bad.append(lines[ti][:60]); continue
        g = m.groups()
        a, b = _secs(*g[:4]), _secs(*g[4:])
        if b < a or (b == a and not allow_zero):     # word timings may have zero duration (common with whisper); subtitles may not
            bad.append(f"time reversed: {lines[ti][:50]}"); continue
        text = " ".join(_TAG.sub("", ln).strip() for ln in lines[ti + 1:] if ln.strip())
        if text:
            events.append((a, b, text))
    if bad:
        raise ReelError("SUBTITLES_INVALID", f"{len(bad)} subtitle blocks could not be parsed (they must not be dropped silently)", path=path, blocks=bad[:5])
    if not events:
        raise ReelError("SUBTITLES_EMPTY", "no subtitles were parsed from the subtitle file", path=path)
    return events


def chunk_text(t, maxlen=14):
    """Split at punctuation into short lines of at most maxlen characters. Sentences that are mostly Latin letters (English etc.) use 42 characters and break only at spaces,
    so words are never split (a hard 14-character cut would give "Did Claude Cod" / "e just...")."""
    letters = [ch for ch in t if ch.isalpha()]
    latin = bool(letters) and sum(ch.isascii() for ch in letters) > len(letters) * 0.6
    if latin:
        maxlen = max(maxlen, 42)
    parts = [x.strip() for x in re.split(r"(?<=[，。、！？；—,.!?;])", t) if x.strip()]
    chunks, cur = [], ""
    for p in parts:
        joined = f"{cur} {p}" if (latin and cur) else cur + p
        if not cur or len(joined) <= maxlen:
            cur = joined
        else:
            chunks.append(cur); cur = p
    if cur:
        chunks.append(cur)
    res = []
    for c in chunks:
        while len(c) > maxlen + 4:
            cut = c.rfind(" ", 0, maxlen + 1) if latin else -1
            cut = cut if cut > 0 else maxlen
            res.append(c[:cut].rstrip()); c = c[cut:].lstrip()
        res.append(c)
    return res


def cues(events):
    """Event to short cues; duration is shared in proportion to character count; never goes past the original event end and never accumulates delay."""
    out = []
    for st, en, t in events:
        chunks = chunk_text(t)
        total = sum(len(c) for c in chunks) or 1
        a = st
        for c in chunks:
            b = min(en, a + (en - st) * len(c) / total)
            disp = c.rstrip(TRAIL)
            if disp:
                out.append((a, b, disp))
            a = b
    return out


def _fmt(t, unit):
    """unit=1000 gives SRT milliseconds; unit=100 gives ASS centiseconds. Round to whole units first, then split, to avoid carry errors like ",1000"."""
    n = int(round(t * unit))
    s, r = divmod(n, unit)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (h, m, s, r)


def write_srt(events, path, keywords=()):
    lines = []
    for i, (a, b, t) in enumerate(cues(events), 1):
        for k in keywords:
            t = t.replace(k, f'<font color="#FFD700">{k}</font>')
        fa, fb = _fmt(a, 1000), _fmt(b, 1000)
        lines.append(f"{i}\n{fa[0]:02d}:{fa[1]:02d}:{fa[2]:02d},{fa[3]:03d} --> "
                     f"{fb[0]:02d}:{fb[1]:02d}:{fb[2]:02d},{fb[3]:03d}\n{t}\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def write_ass(events, path, width, height, font="Noto Sans CJK SC", keywords=()):
    gold, white = r"{\c&H00D7FF&}", r"{\c&H00FFFFFF&}"
    size = round(58 * max(width, height) / 1920)   # 58 px on a 1920-long side, landscape or portrait
    mv = round(250 * height / 1920)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(f"[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\n\n")
        fh.write("[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                 "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, "
                 "Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n")
        fh.write(f"Style: Default,{font},{size},&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
                 f"1,0,0,0,100,100,0,0,1,4,1,2,90,90,{mv},1\n\n")
        fh.write("[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
        for a, b, t in cues(events):
            for k in keywords:
                t = t.replace(k, gold + k + white)
            fa, fb = _fmt(a, 100), _fmt(b, 100)
            fh.write(f"Dialogue: 0,{fa[0]}:{fa[1]:02d}:{fa[2]:02d}.{fa[3]:02d},"
                     f"{fb[0]}:{fb[1]:02d}:{fb[2]:02d}.{fb[3]:02d},Default,,0,0,0,,{t}\n")
    return path
