"""受控交付：重新检查 → 判定成片/待审样片 → 复制到交付目录并校验 → 写 hash 绑定的回执。

回执只证明「这个文件经过这些检查、按这些凭据被复制到这里，复制后 hash 一致」，不证明用户看过。
登记表（registry）记下已登记任务的交付目录，供 Bash guard 只守这些目录；交付流水（deliveries.jsonl）
供完成闸门（Stop hook）核对「声称完成」时有没有有效回执。状态目录：$REEL_STATE_DIR 或 ~/.local/state/reel。
"""
import datetime, json, os, re, shutil, tempfile
from . import check, gate
from .errors import ReelError

RECEIPT_VERSION = "reel.receipt.v1"
VERSION_RE = re.compile(r"^v\d+(\.\d+)*$")
SAMPLE_TAG = "sample-pending-review"   # file-name tag of a sample delivery (待审样片)


def state_dir():
    d = os.environ.get("REEL_STATE_DIR") or os.path.expanduser("~/.local/state/reel")
    os.makedirs(d, exist_ok=True)
    return d


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _sha_or_none(path):
    return check.sha256(path) if path and os.path.isfile(path) else None


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------- 登记表 ----------------

def _registry_path():
    return os.path.join(state_dir(), "registry.json")


def load_registry():
    try:
        with open(_registry_path(), encoding="utf-8") as fh:
            return json.load(fh).get("tasks", [])
    except (OSError, ValueError, AttributeError):
        return []


def register(brief_path, remove=False):
    brief_path = os.path.abspath(brief_path)
    b, final_errs, fatal = gate.load_brief(brief_path)
    tasks = [t for t in load_registry() if t.get("brief") != brief_path]
    if not remove:
        if fatal:
            raise ReelError("BRIEF_INVALID", "brief 不能登记", errors=fatal)
        tasks.append({"brief": brief_path, "project_dir": os.path.dirname(brief_path), "name": b["name"],
                      "delivery_root": os.path.realpath(os.path.expanduser(b["delivery_root"])),
                      "delivery_root_as_written": os.path.abspath(os.path.expanduser(b["delivery_root"])),
                      "deliverable": b.get("deliverable"), "registered_at": _now()})
    _write_json(_registry_path(), {"version": "reel.registry.v1", "tasks": tasks})
    return {"registered": not remove, "brief": brief_path, "brief_warnings": final_errs, "tasks": len(tasks)}


# ---------------- 交付 ----------------

def _copy(src, dst):
    shutil.copyfile(src, dst)


def _copy_verified(src, target, want_sha):
    """复制到同目录临时文件 → 校验 hash → 不覆盖地落位。不一致则删临时文件并报错。"""
    d = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".reel-partial-", suffix=os.path.splitext(target)[1])
    os.close(fd)
    try:
        _copy(src, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        got = check.sha256(tmp)
        if got != want_sha:
            raise ReelError("COPY_MISMATCH", "复制后 hash 不一致，未交付", expect=want_sha, got=got)
        try:
            os.link(tmp, target)             # 目标已存在则失败，不会覆盖
        except FileExistsError:
            raise ReelError("VERSION_EXISTS", "交付目录里已有同名文件，拒绝覆盖", target=target)
        except OSError:
            if os.path.exists(target):
                raise ReelError("VERSION_EXISTS", "交付目录里已有同名文件，拒绝覆盖", target=target)
            os.rename(tmp, target)
            tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    got = check.sha256(target)
    if got != want_sha:
        raise ReelError("COPY_MISMATCH", "落位后 hash 不一致", target=target, expect=want_sha, got=got)
    return got


def deliver(video, brief_path, version, c=None, plan_path=None, profile_ref=None, reviews=None, subs=(), texts=(),
            sample=False, contract=None):
    brief_path = os.path.abspath(brief_path)
    b, final_errs, fatal = gate.load_brief(brief_path)
    if fatal:
        raise ReelError("BRIEF_INVALID", "brief 缺交付必需字段，不能交付", errors=fatal, brief=brief_path)
    if not VERSION_RE.match(version or ""):
        raise ReelError("ARG_INVALID", "版本号格式 v1 / v2 / v3.1", version=version)
    proj = os.path.dirname(brief_path)
    root = os.path.realpath(os.path.expanduser(b["delivery_root"]))
    stem = f"{b['name']}-{version}" + (f"-{SAMPLE_TAG}" if sample else "")
    target = os.path.join(root, stem + os.path.splitext(video)[1].lower())
    rec_path = os.path.join(proj, ".reel", "receipts", f"{b['name']}-{version}.json")
    promote = None   # 同一版本的待审样片被用户认可后升成片：只认同一个文件（hash 一致），成片条件一条不少
    if not sample and os.path.exists(rec_path) and not os.path.exists(target):
        try:
            with open(rec_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (OSError, ValueError):
            prev = {}
        if prev.get("status") == "sample_pending_review":
            promote = {**prev, "receipt": rec_path}
            rec_path = os.path.join(proj, ".reel", "receipts", f"{b['name']}-{version}-final.json")
    if os.path.exists(target) or os.path.exists(rec_path):
        raise ReelError("VERSION_EXISTS", "这个版本号已经交付过，换一个版本号（旧版不删不覆盖）",
                        target=target, receipt=rec_path if os.path.exists(rec_path) else None)
    # 总是对这个文件重新检查：不接受外部报告，旧报告无法冒充
    rep, prof = gate.check_video(video, c, profile_ref, brief_path, subs, texts, reviews, contract)
    g = rep["gate"]
    rep_path = os.path.join(proj, ".reel", "reports",
                            f"{rep['video_sha256'][:12]}-{_now().replace(':', '')}.json")
    _write_json(rep_path, rep)
    if promote and promote.get("video_sha256") != rep["video_sha256"]:
        raise ReelError("PROMOTE_MISMATCH", "要升成片的文件不是这个版本交过的待审样片（文件改过）；换新版本号重新交样片给用户看",
                        sample_receipt=promote["receipt"], sample_sha256=promote.get("video_sha256"),
                        this_sha256=rep["video_sha256"])
    if g["status"] == "fail":
        raise ReelError("DELIVERY_BLOCKED", "技术检查不通过：成片和待审样片都不能交付",
                        blockers=g["blockers"], report=rep_path)
    final_blockers = list(final_errs)
    if b.get("deliverable") != "final":
        final_blockers.append("brief.deliverable 不是 final")
    if g["pending_review"]:
        final_blockers.append(f"未处理的 REVIEW：{g['pending_review']}")
    for k, why in g["final_rejected"].items():
        if why:
            final_blockers.append(f"{k} 判定不通过：{why}")
    if not g["final_reviews"]["final_watch"]:
        final_blockers.append("没有本片的完整看片记录（reviews 里 check=final_watch，带时间码观察）")
    if prof["audio"] != "silent" and not g["final_reviews"]["final_listen"]:
        final_blockers.append("没有本片的试听记录（reviews 里 check=final_listen：人听完记原话 quote，或 agent 带时间码听片观察）")
    if g["invalid_reviews"]:
        final_blockers.append(f"有 {len(g['invalid_reviews'])} 条审片记录不合格（空记录/缺字段）")
    if not sample and final_blockers:
        raise ReelError("DELIVERY_NOT_FINAL", "不满足成片条件；可以加 --sample 登记为待审样片",
                        final_blockers=final_blockers, report=rep_path)
    delivered_sha = _copy_verified(video, target, rep["video_sha256"])
    fr = g["final_reviews"]
    approved_by = None if sample else ("human" if any(fr[k] and not gate.is_agent(fr[k]["by"]) for k in ("final_watch", "final_listen"))
                                        else "agent")
    statement = "copied to the delivery folder and hash-verified" + (
        "; final cut reviewed by the agent (watched and listened); the user has not seen it yet, and their feedback"
        " becomes the next version" if approved_by == "agent" else
        "; this does not mean the user has watched or approved it"
        + ("; this is a sample pending review, not a final cut" if sample else ""))
    receipt = {
        "version": RECEIPT_VERSION, "status": "sample_pending_review" if sample else "final",
        "name": b["name"], "label": version, "created_at": _now(),
        "source": os.path.abspath(video), "delivered": target, "delivered_sha256": delivered_sha,
        "video_sha256": rep["video_sha256"], "copy_verified": True,
        "hashes": {"brief": check.sha256(brief_path), "profile": prof["_sha256"], "plan": _sha_or_none(plan_path),
                   "report": check.sha256(rep_path), "reviews": g["reviews_sha256"],
                   "contract": (rep.get("contract") or {}).get("sha256")},
        "paths": {"brief": brief_path, "report": rep_path, "plan": plan_path, "reviews": reviews, "contract": contract,
                  "profile": prof["_chain"]},
        "gate": {k: g[k] for k in ("status", "pending_review", "resolved_by_review", "not_covered")},
        "final_blockers": final_blockers if sample else [],
        "promoted_from": promote["receipt"] if promote else None,
        "approved_by": approved_by,
        "statement": statement,
    }
    _write_json(rec_path, receipt)
    with open(os.path.join(state_dir(), "deliveries.jsonl"), "a", encoding="utf-8") as fh:
        fh.write(json.dumps({**receipt, "receipt": rec_path}, ensure_ascii=False) + "\n")
    if not any(t.get("brief") == brief_path for t in load_registry()):
        register(brief_path)
    return {**receipt, "receipt": rec_path}


def verify_receipt(path):
    """回执仍然有效：交付文件还在、hash 没变。"""
    try:
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
    except (OSError, ValueError) as e:
        return {"ok": False, "why": f"读不了回执：{e}"}
    if r.get("version") != RECEIPT_VERSION:
        return {"ok": False, "why": "不是 reel 回执"}
    d = r.get("delivered")
    if not d or not os.path.isfile(d):
        return {"ok": False, "why": "交付文件不在了", "delivered": d}
    if check.sha256(d) != r.get("delivered_sha256"):
        return {"ok": False, "why": "交付文件在回执之后被改过", "delivered": d}
    return {"ok": True, "status": r["status"], "delivered": d, "name": r.get("name"), "label": r.get("label")}
