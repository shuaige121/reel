"""文本禁用词扫描：brief、plan 里的可见文字、字幕、显式附加文本。

只证明「这些输入文本」不含禁用词；PNG、录屏、成片画面里的字没有 OCR，不在覆盖范围内。
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
    """返回 [(kind, 文件, [(位置, 文本)]) 或 (kind, 文件, None=缺文件)]。"""
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
            except Exception:  # noqa: BLE001  读不了等同缺失，不能当作扫过
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
    rule = (f"禁用词 {len(banned)} 个（profile text.banned + extra_banned），text.allow 覆盖的整词放行；"
            "缺失或读不了的输入文件 → fail；只覆盖输入文本，画面里的字没有 OCR")
    if not sources and not require:
        return {"status": "not_checked", "rule": rule, "measured": {"sources": 0},
                "evidence": [], "note": "没有提供可扫描的文本（--brief / --plan 字幕 / --subs / --text）"}
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
