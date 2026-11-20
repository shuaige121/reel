"""Controlled delivery: re-check, decide final or sample-pending-review, copy to the delivery directory and verify, write a hash-bound receipt.

The receipt only proves "this file passed these checks, was copied here on these grounds, and the hash matched after copying"; it does not prove the user watched it.
The registry records the delivery directories of registered tasks so the Bash guard protects only those; the delivery log (deliveries.jsonl)
lets the completion gate (Stop hook) check whether a valid receipt exists when "done" is claimed. State directory: $REEL_STATE_DIR or ~/.local/state/reel.
"""
import datetime, json, os, re, shutil, tempfile
from . import check, gate
from .errors import ReelError

RECEIPT_VERSION = "reel.receipt.v1"
VERSION_RE = re.compile(r"^v\d+(\.\d+)*$")
SAMPLE_TAG = "sample-pending-review"   # file-name tag of a sample delivery (sample pending review)


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


# ---------------- registry ----------------

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
            raise ReelError("BRIEF_INVALID", "the brief cannot be registered", errors=fatal)
        tasks.append({"brief": brief_path, "project_dir": os.path.dirname(brief_path), "name": b["name"],
                      "delivery_root": os.path.realpath(os.path.expanduser(b["delivery_root"])),
                      "delivery_root_as_written": os.path.abspath(os.path.expanduser(b["delivery_root"])),
                      "deliverable": b.get("deliverable"), "registered_at": _now()})
    _write_json(_registry_path(), {"version": "reel.registry.v1", "tasks": tasks})
    return {"registered": not remove, "brief": brief_path, "brief_warnings": final_errs, "tasks": len(tasks)}


# ---------------- delivery ----------------

def _copy(src, dst):
    shutil.copyfile(src, dst)


def _copy_verified(src, target, want_sha):
    """Copy to a temp file in the same directory, verify the hash, then put it in place without overwriting. On mismatch the temp file is removed and an error is raised."""
    d = os.path.dirname(target)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".reel-partial-", suffix=os.path.splitext(target)[1])
    os.close(fd)
    try:
        # mkstemp creates the file owner-only (0600); a delivered file gets the normal mode, 0666 minus the umask
        um = os.umask(0); os.umask(um)
        os.chmod(tmp, 0o666 & ~um)
        _copy(src, tmp)
        with open(tmp, "rb") as fh:
            os.fsync(fh.fileno())
        got = check.sha256(tmp)
        if got != want_sha:
            raise ReelError("COPY_MISMATCH", "hash mismatch after copying; not delivered", expect=want_sha, got=got)
        try:
            os.link(tmp, target)             # fails if the target exists, so nothing is overwritten
        except FileExistsError:
            raise ReelError("VERSION_EXISTS", "a file with the same name already exists in the delivery directory; refusing to overwrite", target=target)
        except OSError:
            if os.path.exists(target):
                raise ReelError("VERSION_EXISTS", "a file with the same name already exists in the delivery directory; refusing to overwrite", target=target)
            os.rename(tmp, target)
            tmp = None
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
    got = check.sha256(target)
    if got != want_sha:
        raise ReelError("COPY_MISMATCH", "hash mismatch after placement", target=target, expect=want_sha, got=got)
    return got


def deliver(video, brief_path, version, c=None, plan_path=None, profile_ref=None, reviews=None, subs=(), texts=(),
            sample=False, contract=None):
    brief_path = os.path.abspath(brief_path)
    b, final_errs, fatal = gate.load_brief(brief_path)
    if fatal:
        raise ReelError("BRIEF_INVALID", "the brief lacks fields required for delivery; cannot deliver", errors=fatal, brief=brief_path)
    if not VERSION_RE.match(version or ""):
        raise ReelError("ARG_INVALID", "version format is v1 / v2 / v3.1", version=version)
    proj = os.path.dirname(brief_path)
    root = os.path.realpath(os.path.expanduser(b["delivery_root"]))
    stem = f"{b['name']}-{version}" + (f"-{SAMPLE_TAG}" if sample else "")
    target = os.path.join(root, stem + os.path.splitext(video)[1].lower())
    rec_path = os.path.join(proj, ".reel", "receipts", f"{b['name']}-{version}.json")
    promote = None   # a sample of the same version that the user approved is promoted to final: only the same file (same hash) counts, and every final condition still applies
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
        raise ReelError("VERSION_EXISTS", "this version number was already delivered; use another one (old versions are neither deleted nor overwritten)",
                        target=target, receipt=rec_path if os.path.exists(rec_path) else None)
    # always re-check this file: external reports are not accepted, so an old report cannot stand in
    rep, prof = gate.check_video(video, c, profile_ref, brief_path, subs, texts, reviews, contract)
    g = rep["gate"]
    rep_path = os.path.join(proj, ".reel", "reports",
                            f"{rep['video_sha256'][:12]}-{_now().replace(':', '')}.json")
    _write_json(rep_path, rep)
    if promote and promote.get("video_sha256") != rep["video_sha256"]:
        raise ReelError("PROMOTE_MISMATCH", "the file to promote is not the sample delivered for this version (the file changed); use a new version number and deliver a new sample for the user to review",
                        sample_receipt=promote["receipt"], sample_sha256=promote.get("video_sha256"),
                        this_sha256=rep["video_sha256"])
    if g["status"] == "fail":
        raise ReelError("DELIVERY_BLOCKED", "technical checks failed: neither a final nor a sample can be delivered",
                        blockers=g["blockers"], report=rep_path)
    final_blockers = list(final_errs)
    if b.get("deliverable") != "final":
        final_blockers.append("brief.deliverable is not final")
    if g["pending_review"]:
        final_blockers.append(f"unresolved REVIEW: {g['pending_review']}")
    for k, why in g["final_rejected"].items():
        if why:
            final_blockers.append(f"{k} did not pass: {why}")
    if not g["final_reviews"]["final_watch"]:
        final_blockers.append("no full-watch record for this video (reviews needs check=final_watch with timecoded observations)")
    if prof["audio"] != "silent" and not g["final_reviews"]["final_listen"]:
        final_blockers.append("no listening record for this video (reviews needs check=final_listen: a quote of the person's own words after listening, or timecoded listening observations by the agent)")
    if g["invalid_reviews"]:
        final_blockers.append(f"{len(g['invalid_reviews'])} review records are invalid (empty or missing fields)")
    if not sample and final_blockers:
        raise ReelError("DELIVERY_NOT_FINAL", "final conditions are not met; add --sample to register it as a sample pending review",
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
    """The receipt is still valid: the delivered file exists and its hash is unchanged."""
    try:
        with open(path, encoding="utf-8") as fh:
            r = json.load(fh)
    except (OSError, ValueError) as e:
        return {"ok": False, "why": f"cannot read the receipt: {e}"}
    if r.get("version") != RECEIPT_VERSION:
        return {"ok": False, "why": "not a reel receipt"}
    d = r.get("delivered")
    if not d or not os.path.isfile(d):
        return {"ok": False, "why": "the delivered file is gone", "delivered": d}
    if check.sha256(d) != r.get("delivered_sha256"):
        return {"ok": False, "why": "the delivered file was changed after the receipt", "delivered": d}
    return {"ok": True, "status": r["status"], "delivered": d, "name": r.get("name"), "label": r.get("label")}
