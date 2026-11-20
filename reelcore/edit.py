"""小粒度编辑 + revision 历史。

流程：读当前 revision →（--base 冲突检测）→ 在内存副本上应用操作 → validate + compile_plan
→ 成功才把旧版快照存进 PLAN 同目录 .reel-history/<rev>.json、原子写回 plan.json、追加 log；
任何一步失败都不落盘，返回结构化错误（committed=false）。

revision：第一次编辑时把原文件登记为 r0；之后每次提交 r1, r2 …，每个 revision 都有一份完整快照。
plan.json 被 reel edit 以外的方式改过（与最后一个 revision 的 hash 不同）→ 当前 revision 记作 "rN*"；
下一次编辑先把这份外部修改登记成一个 external_edit revision，历史不丢。
undo 不回退指针，而是提交一个内容等于目标 revision 的新 revision（历史只增不删）。
"""
import copy, datetime, fcntl, hashlib, json, os, tempfile
from . import perceive, plan as planmod, render, timeline
from .errors import ReelError

HIST = ".reel-history"
OPS = ("trim", "move", "insert", "replace", "set-transition", "set-camera")


def _sha(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def hist_dir(plan_path):
    return os.path.join(os.path.dirname(os.path.abspath(plan_path)), HIST)


def _log_path(plan_path):
    return os.path.join(hist_dir(plan_path), "log.json")


def read_log(plan_path):
    p = _log_path(plan_path)
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        log = json.load(fh)
    if log.get("plan") != os.path.basename(plan_path):
        raise ReelError("HISTORY_MISMATCH", f"{HIST} 属于 {log.get('plan')}，不是 {os.path.basename(plan_path)}；"
                        "同一目录只支持一个 plan 的历史", history=hist_dir(plan_path))
    return log


def state(plan_path, raw=None):
    """当前 revision 标签：无历史 → r0；与最后 revision 一致 → rN；被外部改过 → rN*。"""
    raw = planmod.load(plan_path) if raw is None else raw
    log = read_log(plan_path)
    if not log:
        return {"revision": "r0", "recorded": False, "dirty": False, "sha": _sha(raw)}
    last = log["revisions"][-1]
    dirty = _sha(raw) != last["sha"]
    return {"revision": last["rev"] + ("*" if dirty else ""), "recorded": True, "dirty": dirty, "sha": _sha(raw),
            "last": last["rev"]}


def resolved(raw, plan_path):
    """与 cli._plan_and_obs 相同的 src_dir 解析，作用在副本上（原始 plan 保持原样写回）。"""
    p = copy.deepcopy(raw)
    base = os.path.dirname(os.path.abspath(plan_path))
    if not os.path.isabs(p.get("src_dir") or ""):
        p["src_dir"] = os.path.normpath(os.path.join(base, p.get("src_dir") or "."))
    return p


def default_obs(plan_path, obs_path=None):
    return perceive.load_obs(obs_path or os.path.join(os.path.dirname(os.path.abspath(plan_path)),
                                                      "observations.json"))


# ---------------- 操作 ----------------

def _idx(plan, sid):
    shots = plan.get("shots") or []
    for i, s in enumerate(shots):
        if isinstance(s, dict) and s.get("id") == sid:
            return i
    raise ReelError("SHOT_NOT_FOUND", f"没有镜头 {sid!r}", ids=[s.get("id") for s in shots if isinstance(s, dict)])


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _pos(plan, op, default_end=True):
    if "index" in op:
        if not (isinstance(op["index"], int) and 0 <= op["index"] <= len(plan["shots"])):
            raise ReelError("OP_INVALID", f"index 超出范围 0..{len(plan['shots'])}", op=op)
        return op["index"]
    if "after" in op:
        return _idx(plan, op["after"]) + 1
    if "before" in op:
        return _idx(plan, op["before"])
    if default_end:
        return len(plan["shots"])
    raise ReelError("OP_INVALID", "需要 index / before / after 之一", op=op)


def apply_op(plan, op):
    """在 plan（原始 JSON，未规范化）上原地应用一个操作，返回直接改动的镜头 id。"""
    if not isinstance(op, dict) or op.get("op") not in OPS:
        raise ReelError("OP_INVALID", f"op 必须是 {OPS}", op=op)
    kind = op["op"]
    if kind == "insert":
        shot = op.get("shot")
        if not isinstance(shot, dict) or not isinstance(shot.get("id"), str):
            raise ReelError("OP_INVALID", "insert 需要 shot 对象且带 id", op=op)
        at = _pos(plan, op)
        plan["shots"].insert(at, copy.deepcopy(shot))
        return [shot["id"]]
    i = _idx(plan, op.get("id"))
    s = plan["shots"][i]
    if kind == "trim":
        if "duration" not in op and "source_in" not in op:
            raise ReelError("OP_INVALID", "trim 需要 duration 和/或 source_in", op=op)
        for k in ("duration", "source_in"):
            if k in op:
                if not _num(op[k]):
                    raise ReelError("OP_INVALID", f"trim.{k} 必须是数字", op=op)
                s[k] = op[k]
        return [s["id"]]
    if kind == "move":
        if "swap" in op:
            j = _idx(plan, op["swap"])
            plan["shots"][i], plan["shots"][j] = plan["shots"][j], plan["shots"][i]
            return [s["id"], op["swap"]]
        shot = plan["shots"].pop(i)
        try:
            at = _pos(plan, op, default_end=False)
        except ReelError:
            plan["shots"].insert(i, shot)
            raise
        plan["shots"].insert(at, shot)
        return [s["id"]]
    if kind == "replace":
        if not isinstance(op.get("asset"), str) or not op["asset"]:
            raise ReelError("OP_INVALID", "replace 需要 asset", op=op)
        if s.get("kind") == "web":
            if "camera" not in op:
                raise ReelError("OP_INVALID", "网页镜头换成素材时运镜不通用，需同时给 camera", op=op)
            for k in ("kind", "web", "highlight"):
                s.pop(k, None)
        s["asset"] = op["asset"]
        if "source_in" in op:
            s["source_in"] = op["source_in"]
        if "camera" in op:
            s["camera"] = op["camera"]
        return [s["id"]]
    if kind == "set-transition":
        t = op.get("type")
        if t in (None, "none"):
            s.pop("transition_out", None)
        else:
            s["transition_out"] = {"type": t, "duration": op.get("duration")}
        return [s["id"]]
    if kind == "set-camera":
        if "camera" not in op:
            raise ReelError("OP_INVALID", "set-camera 需要 camera（数组；[] = 静止）", op=op)
        s["camera"] = op["camera"]
        for k in ("viewport", "space"):
            if k in op:
                s[k] = op[k]
        return [s["id"]]
    raise AssertionError(kind)


# ---------------- 差异 ----------------

def _shot_sigs(c):
    out = {}
    for i, s in enumerate(c["plan"]["shots"]):
        st = c["starts"][i]
        body = {k: v for k, v in s.items() if k != "duration"}
        out[s["id"]] = ((st, st + c["frames"][i], c["trans"][i]), _sha(body))
    return out


def _merge(iv):
    iv = sorted(x for x in iv if x[1] > x[0])
    out = []
    for a, b in iv:
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def affected(old_c, new_c):
    """受影响的帧区间（新时间线坐标 + 被删镜头在旧时间线的区间）和只平移的镜头。"""
    new = _shot_sigs(new_c)
    if old_c is None:
        return [[0, new_c["visual_f"]]], [], list(new)
    old = _shot_sigs(old_c)
    iv, shifted, content = [], [], []
    for sid in set(old) | set(new):
        o, n = old.get(sid), new.get(sid)
        if o == n:
            continue
        if n:
            iv.append([n[0][0], n[0][1]])
        if o and not n:
            iv.append([o[0][0], o[0][1]])
        if o and n and o[1] == n[1] and o[0][1] - o[0][0] == n[0][1] - n[0][0] and o[0][2] == n[0][2]:
            shifted.append(sid)
        else:
            content.append(sid)
    if old_c["visual_f"] != new_c["visual_f"]:
        iv.append([min(old_c["visual_f"], new_c["visual_f"]), max(old_c["visual_f"], new_c["visual_f"])])
    order = [s["id"] for s in new_c["plan"]["shots"]]
    key = lambda x: order.index(x) if x in order else len(order)
    return _merge(iv), sorted(shifted, key=key), sorted(content, key=key)


# ---------------- 提交 ----------------

def _atomic_json(path, obj):
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=d)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _now():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _compile(raw, plan_path, obs):
    return render.compile_plan(resolved(raw, plan_path), obs)


def _commit(plan_path, before_sha, new_raw, op):
    """持锁提交：确认读取后文件没被别人改过 → 快照 → 原子写 plan → 追加 log。"""
    hd = hist_dir(plan_path)
    os.makedirs(hd, exist_ok=True)
    with open(os.path.join(hd, "lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = planmod.load(plan_path)
        if _sha(cur) != before_sha:
            raise ReelError("REV_CONFLICT", "编辑期间 plan.json 被别的进程改过，未写入；重新读取后再编辑",
                            committed=False)
        log = read_log(plan_path) or {"plan": os.path.basename(plan_path), "revisions": []}
        revs = log["revisions"]

        def add(obj, rec_op, parent):
            rev = f"r{len(revs)}"
            _atomic_json(os.path.join(hd, f"{rev}.json"), obj)
            revs.append({"rev": rev, "parent": parent, "op": rec_op, "time": _now(), "sha": _sha(obj)})
            return rev

        if not revs:
            add(cur, {"op": "init"}, None)
        elif _sha(cur) != revs[-1]["sha"]:
            add(cur, {"op": "external_edit"}, revs[-1]["rev"])
        parent = revs[-1]["rev"]
        rev = add(new_raw, op, parent)
        _atomic_json(plan_path, new_raw)
        _atomic_json(_log_path(plan_path), log)
        return rev, parent


def _run(plan_path, new_fn, op_record, base, dry_run, obs, detail):
    raw = planmod.load(plan_path)
    st = state(plan_path, raw)
    if base is not None and base != st["revision"]:
        raise ReelError("REV_CONFLICT", f"--base {base} 不是当前 revision {st['revision']}；先 timeline/history 读最新再改",
                        base=base, current=st["revision"], committed=False)
    new_raw, changed = new_fn(copy.deepcopy(raw))
    try:
        old_c = _compile(raw, plan_path, obs)
    except ReelError:
        old_c = None                       # 原 plan 本身编译不过：受影响区间按全片报
    try:
        new_c = _compile(new_raw, plan_path, obs)
    except ReelError as e:
        raise ReelError(e.code, f"编辑被拒绝，未写入：{e.message}", committed=False, op=op_record,
                        **{k: v for k, v in e.detail.items() if k not in ("committed", "op")})
    iv, shifted, content = affected(old_c, new_c)
    if changed is None:
        changed = content
    if dry_run:
        rev, parent = None, st["revision"]
    else:
        rev, parent = _commit(plan_path, st["sha"], new_raw, op_record)
    tl = timeline.build(new_c, detail=detail, revision=rev or f"{st['revision']}+dry-run")
    return {"committed": not dry_run, "dry_run": dry_run, "revision": rev, "parent": parent,
            "changed_ids": changed, "content_changed_ids": content, "shifted_ids": shifted,
            "affected_frames": iv, "adjustments": new_c["adjustments"], "timeline": timeline.text(tl)}


def edit(plan_path, ops, base=None, dry_run=False, obs=None, detail="events"):
    ops = ops if isinstance(ops, list) else [ops]
    if not ops:
        raise ReelError("OP_INVALID", "没有操作")

    def fn(p):
        changed = []
        for op in ops:
            for x in apply_op(p, op):
                if x not in changed:
                    changed.append(x)
        return p, changed
    rec = ops[0] if len(ops) == 1 else {"op": "batch", "ops": ops}
    return _run(plan_path, fn, rec, base, dry_run, obs, detail)


def undo(plan_path, to, base=None, dry_run=False, obs=None):
    log = read_log(plan_path)
    snap = os.path.join(hist_dir(plan_path), f"{to}.json")
    if not log or not any(r["rev"] == to for r in log["revisions"]) or not os.path.isfile(snap):
        raise ReelError("REV_NOT_FOUND", f"没有 revision {to}", known=[r["rev"] for r in (log or {}).get("revisions", [])])
    with open(snap, encoding="utf-8") as fh:
        target = json.load(fh)
    return _run(plan_path, lambda p: (target, None), {"op": "undo", "to": to},
                base, dry_run, obs, "events")


def history(plan_path):
    log = read_log(plan_path)
    st = state(plan_path)
    return {"current": st["revision"], "dirty": st["dirty"], "history_dir": hist_dir(plan_path),
            "revisions": [{k: r[k] for k in ("rev", "parent", "op", "time")} for r in (log or {}).get("revisions", [])]}
