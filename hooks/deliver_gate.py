#!/usr/bin/env python3
"""Stop hook: if this turn produced video (or the session is inside a registered reel project) and the reply claims it is finished / delivered but no matching final receipt can be shown, it blocks.

- "Video output": a producer such as ffmpeg / melt / reel render|preview / build_*.py in command position, with a video file in the command or its output,
  not in a temp/test directory; or this turn called reel deliver / register; or the session cwd is inside the project directory of a registered task.
- "Claims done" is judged sentence by sentence: a sentence with completion wording that is about the video (final / video / sample / delivery / mp4 / vN), where neither this sentence nor the next has
an honest status (sample pending review / not finished / awaiting audition / not delivered ...). Sentences that only say tests / code / docs are done do not count. If the text says "no video delivered this turn", nothing counts. The patterns recognise both Chinese and English wording.
- Which receipts count: status=final, the delivered file still exists with an unchanged sha256, and
  * if this turn produced output: the receipt path appears in this turn's command output (i.e. this turn delivered it itself) and is later than the last producing command of this turn;
  * if only inside a registered project: that project's receipts (any time, to answer "where is the video").
  Every version number (vN) named in the claim needs a matching final receipt.
- At most MAX_BLOCKS blocks per turn (to avoid an endless loop from a hook misjudgement); passes are logged in stopgate.log; on error it passes silently.
It is a wording and voucher check and does not prove the video is good; the receipt itself is guaranteed by the checks in reel deliver.
"""
import datetime, hashlib, json, os, re, shlex, sys

STATE = os.environ.get("REEL_STATE_DIR") or os.path.expanduser("~/.local/state/reel")
MAX_BLOCKS = 5
VIDEO_EXT = re.compile(r"\.(mp4|mov|m4v|webm|mkv)\b", re.I)
# only producer programs in command position (segment start) count; words in `which ffmpeg` or grep do not
PROD = re.compile(r"^(\S*/)?(ffmpeg|melt|remotion|hyperframes)\b|^(\S*/)?reel(\.py)?\s+(render|preview)\b"
                  r"|^(\S*/)?python3?\s+(-\S+\s+)*\S*(build|render|make|export|compose|cut|edit|video|reel|clip|montage)"
                  r"\w*\.py\b|^(\S*/)?python3?\s+\S*reel\.py\s+(render|preview)\b|^npx\s+(remotion|hyperframes)\b", re.I)
SEG = re.compile(r"\s*(?:;|&&|\|\||\||\n)\s*")
ENV_PREFIX = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*=\S*\s+)+")
SCRATCH = re.compile(r"/tmp/|/private/tmp/|/var/folders/|scratchpad|/tests?/|reel_gate_|reel_test_")
REEL_DELIVERY = re.compile(r"^(\S*/)?(python3?\s+\S*reel\.py|reel)\s+(deliver|register)\b")
_DONE = r"(完成|搞定|做好|剪好|做完|剪完|出好|导出|交付|交给你)"
CLAIM = re.compile(rf"(已经?|都|全部|均)\s*{_DONE}|{_DONE}了|成片(在|已|放)|可以(看|发|用)了"
                   r"|(视频|成片|片子|剪辑|样片)\S{0,8}(完成|好了|搞定)"
                   r"|已(放|存|保存|复制|上传|导出)(到|进|在|好)|\bdone\b|\bdelivered\b|\bfinished\b"
                   r"|\bcompleted?\b|\bexported\b|\bready to (post|publish|share|upload|watch)\b"
                   r"|\b(is|are) (now )?ready\b", re.I)
DELIV = re.compile(r"成片|片子|样片|视频|剪辑|交付|导出|\.mp4|\.mov|\bmp4\b|\bmov\b|\bv\d+(\.\d+)*\b"
                   r"|\bvideos?\b|\bfinal cut\b|\bsample\b|\bdeliver(y|ed)\b|\bexport(ed)?\b", re.I)
HARD_DELIV = re.compile(r"成片|片子|样片|交付|导出|\.mp4|\.mov|\bv\d+(\.\d+)*\b|\bfinal cut\b|\bdelivered\b", re.I)
DEV = re.compile(r"测试|代码|文档|检查器|脚本|hook|单测|用例|commit|提交|重构|修复|lint|README|PROGRESS"
                 r"|\btests?\b|\bcode\b|\bdocs?\b|\bdocumentation\b|\bscripts?\b|\brefactor\w*|\bchecker\b", re.I)
HONEST = re.compile(r"待审样片|待审|未完成|没完成|尚未完成|还没(有)?完成|待试听|未试听|还没试听|未交付|没有交付|没交付"
                    r"|不能交付|无法交付|DELIVERY_NOT_FINAL|DELIVERY_BLOCKED"
                    r"|pending review|awaiting (your )?review|needs (your )?review|not (yet )?(final|finished|done|complete"
                    r"|delivered|reviewed|approved)|unfinished|not been (delivered|reviewed)|no final receipt"
                    r"|cannot be delivered|can't be delivered", re.I)
GLOBAL_HONEST = re.compile(r"本轮无视频交付|不涉及视频交付|没有视频交付|no video (was )?delivered this turn"
                           r"|no video delivery this turn", re.I)
SENT = re.compile(r"[^。！？!?\n]+")
SENT_END = re.compile(r"(?<=[.;])\s+")          # English sentence ends; "final.mp4" has no space after the dot
LABEL = re.compile(r"\bv\d+(?:\.\d+)*\b", re.I)


def claim_sentences(msg):
    if not msg or GLOBAL_HONEST.search(msg):
        return []
    ss = [x.strip() for x in SENT.findall(SENT_END.sub("\n", msg)) if x.strip()]
    out = []
    for i, x in enumerate(ss):
        if not CLAIM.search(x) or not DELIV.search(x):
            continue
        if DEV.search(x) and not HARD_DELIV.search(x):
            continue                        # "all tests are done" / "the code is fixed" is not a delivery claim
        if HONEST.search(x) or (i + 1 < len(ss) and HONEST.search(ss[i + 1])):
            continue
        out.append(x)
    return out


def _ts(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_human(e):
    if e.get("type") == "user" and e.get("isMeta"):
        return False
    m = e.get("message") or {}
    if m.get("role") != "user":
        return False
    c = m.get("content")
    if isinstance(c, str):
        return True
    return isinstance(c, list) and not any(isinstance(x, dict) and x.get("type") == "tool_result" for x in c)


def load(path):
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    pass
    start = 0
    for i in range(len(entries) - 1, -1, -1):
        if _is_human(entries[i]):
            start = i
            break
    session_start = next((_ts(e.get("timestamp")) for e in entries if e.get("timestamp")), None)
    turn = [e for e in entries[start:] if not e.get("isSidechain")]
    return turn, session_start, (entries[start].get("uuid") if entries else None)


def _text(c):
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(_text(x.get("content") if x.get("type") == "tool_result" else x.get("text", ""))
                         for x in c if isinstance(x, dict))
    return ""


def analyse(turn):
    cmds, results, last = [], {}, ""
    for e in turn:
        m = e.get("message") or {}
        c = m.get("content")
        if not isinstance(c, list):
            continue
        if m.get("role") == "assistant":
            texts = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use" and b.get("name") == "Bash":
                    cmds.append((b.get("id"), (b.get("input") or {}).get("command") or "", _ts(e.get("timestamp"))))
                elif b.get("type") == "text":
                    texts.append(b.get("text", ""))
            if any(t.strip() for t in texts):
                last = "\n".join(texts)
        else:
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results[b.get("tool_use_id")] = _text(b.get("content"))
    return cmds, results, last


def _segments(cmd):
    """Split into simple commands in a quote-aware way (newlines/semicolons inside quotes are not split); falls back to a rough split on symbols when parsing fails."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars="();<>|&\n")
        lex.whitespace = " \t\r"
        lex.whitespace_split = True
        out, cur = [], []
        for t in lex:
            if t in (";", "&&", "||", "|", "&", "\n", "(", ")", "{", "}", "()", "|&", ";;"):
                if cur:
                    out.append(" ".join(cur))
                cur = []
            else:
                cur.append(t)
        if cur:
            out.append(" ".join(cur))
        return out
    except ValueError:
        return SEG.split(cmd)


def video_work(cmds, results):
    """Returns (reason, time of this turn's last producing command). A heredoc body (python - <<EOF editing docs/tests) does not count as a command."""
    why, last = None, None
    for tid, full, ts in cmds:
        cmd = full.split("<<", 1)[0]
        segs = [ENV_PREFIX.sub("", x.strip().lstrip("({ ")) for x in _segments(cmd)]
        hit = next((x for x in segs if REEL_DELIVERY.search(x)), None)
        if hit:
            why = why or f"called reel: {hit[:80]}"
            continue
        if SCRATCH.search(cmd):
            continue
        for seg in segs:
            if PROD.search(seg) and (VIDEO_EXT.search(seg) or VIDEO_EXT.search(results.get(tid, ""))):
                why = f"video production command: {seg[:80]}"
                last = ts or last
                break
    return why, last


def registered_project(cwd):
    try:
        with open(os.path.join(STATE, "registry.json"), encoding="utf-8") as fh:
            tasks = json.load(fh).get("tasks", [])
    except (OSError, ValueError, AttributeError):
        return None
    cwd = os.path.realpath(cwd or "")
    for t in tasks:
        p = os.path.realpath(t.get("project_dir") or "/nonexistent")
        if cwd == p or cwd.startswith(p + os.sep):
            return p
    return None


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def receipts(turn_text, after=None, project=None):
    """Valid receipts: the delivered file still exists and its hash is unchanged. If project is given, that project's (any time); otherwise those that appeared in this turn's output
    and are later than this turn's last output. Returns [(status, label)]."""
    out = []
    try:
        lines = open(os.path.join(STATE, "deliveries.jsonl"), encoding="utf-8").read().splitlines()
    except OSError:
        return out
    for ln in lines:
        try:
            r = json.loads(ln)
        except ValueError:
            continue
        brief = (r.get("paths") or {}).get("brief") or ""
        if project:
            if os.path.realpath(os.path.dirname(brief)) != project:
                continue
        else:
            if not (r.get("receipt") and r["receipt"] in turn_text) and not (r.get("delivered") and r["delivered"] in turn_text):
                continue
            ts = _ts(r.get("created_at"))
            if after and ts and ts + datetime.timedelta(seconds=1) < after:
                continue
        d = r.get("delivered")
        if not d or not os.path.isfile(d) or _sha(d) != r.get("delivered_sha256"):
            continue
        out.append((r.get("status"), r.get("label")))
    return out


def _bump(key):
    path = os.path.join(STATE, "stopgate.json")
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d = {k: v for k, v in d.items() if k == key}          # keep only the current turn
    d[key] = d.get(key, 0) + 1
    os.makedirs(STATE, exist_ok=True)
    json.dump(d, open(path, "w", encoding="utf-8"))
    if d[key] > MAX_BLOCKS:
        with open(os.path.join(STATE, "stopgate.log"), "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} released after {MAX_BLOCKS} blocks: {key}\n")
    return d[key]


def verdict(payload):
    path = payload.get("transcript_path")
    if not path:
        return None
    turn, _, turn_id = load(path)
    cmds, results, last = analyse(turn)
    last = payload.get("last_assistant_message") or last
    claims = claim_sentences(last)
    if not claims:
        return None
    why, last_prod = video_work(cmds, results)
    proj = registered_project(payload.get("cwd"))
    if not why and not proj:
        return None
    turn_text = "\n".join(c for _, c, _ in cmds) + "\n" + "\n".join(results.values())
    got = receipts(turn_text, after=last_prod) if why else receipts(turn_text, project=proj)
    finals = {lab for st, lab in got if st == "final"}
    wanted = {m.group(0).lower() for x in claims for m in LABEL.finditer(x)}
    missing = sorted(wanted - {str(x).lower() for x in finals})
    if finals and not missing:
        return None
    if _bump(f"{payload.get('session_id')}:{turn_id}") > MAX_BLOCKS:
        return None
    ctx = why or f"session is inside a registered reel project: {proj}"
    if finals and missing:
        problem = f"the claimed version(s) {missing} have no final receipt (receipts exist for {sorted(finals)})"
    elif any(st != "final" for st, _ in got):
        problem = "there is only a sample receipt, so say it is a sample pending review (待审样片)"
    else:
        problem = "there is no matching final receipt" + (
            " (the receipt must be written by `reel deliver` in this turn, after the last render)" if why else "")
    return (f"[deliver-gate] This sentence claims the video is finished or delivered: \"{claims[0][:60]}\", but {problem}"
            f" (context: {ctx}). Either run `reel check` -> `reel deliver` and get a status=final receipt, or state the"
            " real status in the same sentence: \"sample, pending review\", \"not finished\", \"not delivered\""
            " (待审样片 / 未完成 / 待试听 / 未交付). If this turn only discussed or changed code and delivered no video,"
            " say \"no video delivered this turn\" (本轮无视频交付).")


def main():
    try:
        payload = json.load(sys.stdin)
        reason = verdict(payload)
    except Exception:  # noqa: BLE001
        return
    if reason:
        json.dump({"decision": "block", "reason": reason}, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
    sys.exit(0)
