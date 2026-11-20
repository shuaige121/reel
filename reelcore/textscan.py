"""Banned-word scan: brief, visible text in the plan, subtitles, explicitly supplied text.

It only proves that "these input texts" contain no banned words; text inside PNGs, screen recordings or the final picture is not OCR-ed and is out of scope.
"""
import json, os, re
from . import subtitles

PLAN_TEXT_KEYS = {"text", "title", "caption", "label"}


def _walk_strings(obj, path=""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk_strings(v, f"{path}[{i}]")


def _plan_strings(plan):
    def walk(obj, path):
        if isinstance(obj, dict):
            for k, v in obj.items():
                p = f"{path}.{k}"
                if k in PLAN_TEXT_KEYS and isinstance(v, str):
                    yield p, v
                else:
                    yield from walk(v, p)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from walk(v, f"{path}[{i}]")
    yield from walk(plan.get("shots") or [], "shots")


def collect(brief=None, plan=None, plan_subs=None, subs=(), texts=()):
    """Returns [(kind, file, [(position, text)]) or (kind, file, None = missing file)]."""
    out = []
    if brief:
        try:
            with open(brief, encoding="utf-8") as fh:
                b = json.load(fh)
            out.append(("brief", brief, list(_walk_strings(b))))
        except (OSError, ValueError):
            out.append(("brief", brief, None))
    if plan is not None:
        out.append(("plan", "plan.shots", list(_plan_strings(plan))))
    for kind, files in (("subtitles", ([plan_subs] if plan_subs else []) + list(subs)), ("text", texts)):
        for f in files:
            if not os.path.isfile(f):
                out.append((kind, f, None))
                continue
            try:
                if kind == "subtitles":
                    lines = [(f"{a:.2f}s", t) for a, _, t in subtitles.cues(subtitles.parse(f))]
                else:
                    with open(f, encoding="utf-8") as fh:
                        lines = [(f"L{i}", s) for i, s in enumerate(fh.read().splitlines(), 1)]
            except Exception:  # noqa: BLE001  an unreadable file counts as missing, never as scanned
                lines = None
            out.append((kind, f, lines))
    return out


def _hits(text, banned, allow):
    low = text.lower()
    covered = set()
    for a in allow:
        for m in re.finditer(re.escape(a.lower()), low):
            covered.update(range(m.start(), m.end()))
    hits = []
    for w in banned:
        for m in re.finditer(re.escape(w.lower()), low):
            if not all(i in covered for i in range(m.start(), m.end())):
                hits.append((w, m.start()))
    return hits


def scan(sources, profile):
    t = profile.get("text") or {}
    banned = list(dict.fromkeys((t.get("banned") or []) + (t.get("extra_banned") or [])))
    allow = t.get("allow") or []
    require = set(t.get("require") or [])
    rule = (f"{len(banned)} banned words (profile text.banned + extra_banned); whole words covered by text.allow pass; "
            "a missing or unreadable input file means fail; only input text is covered, text in the picture is not OCR-ed")
    if not sources and not require:
        return {"status": "not_checked", "rule": rule, "measured": {"sources": 0},
                "evidence": [], "note": "no scannable text was supplied (--brief / --plan subtitles / --subs / --text)"}
    ev, n_lines = [], 0
    kinds = {k for k, _, lines in sources if lines is not None}
    for kind, f, lines in sources:
        if lines is None:
            ev.append({"kind": kind, "file": f, "missing": True})
            continue
        for where, s in lines:
            n_lines += 1
            for w, pos in _hits(s, banned, allow):
                ev.append({"kind": kind, "file": f, "at": where, "word": w,
                           "context": s[max(0, pos - 12): pos + len(w) + 12]})
    for k in sorted(require - kinds):
        ev.append({"kind": k, "missing": True, "required_by_profile": True})
    return {"status": "fail" if ev else "pass", "rule": rule,
            "measured": {"sources": len(sources), "lines": n_lines, "banned": len(banned),
                         "kinds": sorted(kinds)}, "evidence": ev}
