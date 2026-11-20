"""Delivery gate: combines the check report, the review records and the brief into "can it be delivered, and as what".

- a fail, a detection error, or a missing / unchecked required item in the check report becomes a blocker (technically unacceptable; not even a sample can be delivered)
- each needs_review item must be handled by a review record bound to this video's sha256; if not, it can only be registered as a sample pending review
- a final additionally needs: a complete brief with deliverable=final, and a TTS / cloned voice approved by audition
  (a person records their own words; or an agent runs an STT check with reel voice check),
  plus a full watch record (final_watch) and a listening record (final_listen, except for silent versions) for the final version.
  final_listen can come from a person (recording a quote of their words) or from an agent (once the agent has reviewed, the final may be delivered and the user comments after watching),
  an agent sign-off must carry timecoded listening observations; the receipt's approved_by says who reviewed.
A review record is a voucher, not the truth: only its shape and bindings (hash, timecodes, quote) are validated; it does not prove anyone really watched.
"""
import json, os, re
from . import check, profiles, textscan, voicecheck
from .errors import ReelError

BRIEF_VERSION = "reel.brief.v1"
REVIEWS_VERSION = "reel.reviews.v1"
DELIVERABLES = ("final", "sample")
VOICE_MODES = ("tts", "clone", "recorded", "none")
KINDS = (None, "mv")   # kind=mv: check and deliver must carry --contract (the MV acceptance contract), samples included
CONTRACT_TEMPLATE = {"version": "reel.contract.v1", "kind": "mv", "output": {"width": 1080, "height": 1920, "fps": 24},
                     "songs": [{"path": "song.m4a", "sha256": "<shasum -a 256>", "at": 0.0, "policy": "full"}],
                     "lyrics": {"path": "x.lyrics.json (reel lyrics import song.m4a --lyrics lyrics.md --out x.lyrics.json)",
                                "sha256": "..."},
                     "text_layer": {"path": "textlayer.mkv (a grayscale mask holding only the lyric glyphs, same length and frame rate as the video)", "sha256": "..."}}
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
AI_NAMES = re.compile(r"claude|agent|codex|gpt|gemini|assistant|llm|bot|^ai$|^model$|^director$", re.I)


def is_agent(by):
    return isinstance(by, str) and bool(AI_NAMES.search(by))


# ---------------- brief ----------------

def _too_broad(root):
    home = os.path.realpath(os.path.expanduser("~"))
    broad = {"/", "/Users", "/tmp", "/private/tmp", home} | {
        os.path.realpath(os.path.join(home, x)) for x in
        ("Desktop", "Documents", "Downloads", "work", "Library", "Library/Mobile Documents",
         "Library/Mobile Documents/com~apple~CloudDocs")}
    return root in broad


def load_brief(path):
    """Returns (brief, final_errors, fatal_errors). fatal: no delivery of any kind is possible; final_errors: only a sample pending review can be delivered."""
    try:
        with open(path, encoding="utf-8") as fh:
            b = json.load(fh)
    except FileNotFoundError:
        raise ReelError("BRIEF_NOT_FOUND", "brief not found", path=path)
    except ValueError as e:
        raise ReelError("BRIEF_INVALID", f"brief is not valid JSON: {e}", path=path)
    if not isinstance(b, dict):
        raise ReelError("BRIEF_INVALID", "brief must be a JSON object", path=path)
    proj = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    final, fatal = [], []
    if b.get("version") != BRIEF_VERSION:
        final.append(f"version must be {BRIEF_VERSION}")
    name = b.get("name")
    if not isinstance(name, str) or not name.strip() or "/" in name:
        fatal.append("name (the delivery file-name prefix, without /) is missing")
    if not isinstance(b.get("language"), str) or not b["language"].strip():
        final.append("language (the delivery language: subtitles / voice / on-screen text) is missing")
    if b.get("deliverable") not in DELIVERABLES:
        final.append(f"deliverable (final or sample) must be one of {DELIVERABLES}")
    if not isinstance(b.get("must_not_change"), list):
        final.append("must_not_change (things that must not change; may be an empty array but must be present) is missing")
    root = b.get("delivery_root")
    if not isinstance(root, str) or not root.strip():
        fatal.append("delivery_root (the delivery directory) is missing")
    else:
        r = os.path.realpath(os.path.expanduser(root))
        if not os.path.isabs(os.path.expanduser(root)):
            fatal.append("delivery_root must be an absolute path or start with ~")
        elif not os.path.isdir(r):
            fatal.append(f"delivery_root does not exist: {r}")
        elif _too_broad(r) or proj == r or proj.startswith(r + os.sep):
            fatal.append(f"delivery_root is too broad (it must not be the home directory, the iCloud root, the project directory or a parent of it): {r}")
    if b.get("kind") not in KINDS:
        fatal.append(f"kind may only be omitted or be one of {KINDS[1:]}")
    v = b.get("voice")
    if not isinstance(v, dict) or v.get("mode") not in VOICE_MODES:
        final.append(f"voice.mode must be one of {VOICE_MODES} (write none if there is no voice-over)")
    elif v["mode"] in ("tts", "clone"):
        final += _voice_errors(v, proj)
    return b, final, fatal


def _voice_errors(v, proj):
    errs = []
    if not isinstance(v.get("voice_id"), str) or not v["voice_id"].strip():
        errs.append("voice.voice_id (which voice is used) is missing")
    ap = v.get("approved")
    if not isinstance(ap, dict):
        return errs + ["the voice was not approved by audition: voice.approved is missing (after producing an audition sample run reel voice check SAMPLE --script AUDITION_TEXT --brief brief.json; or a person records their own words after listening)"]
    if ap.get("voice_id") != v.get("voice_id"):
        errs.append(f"approved voice is {ap.get('voice_id')!r} but {v.get('voice_id')!r} is in use: a changed voice must be auditioned again")
    by = ap.get("by")
    if not isinstance(by, str) or not by.strip():
        errs.append("voice.approved.by is missing (who approved: a person, or an agent that did an STT check)")
    elif is_agent(by):
        errs += voicecheck.approval_errors(ap)
    elif not isinstance(ap.get("quote"), str) or len(ap["quote"].strip()) < 2:
        errs.append("voice.approved.quote is missing (when a person approves, their own words must be recorded)")
    s = ap.get("sample")
    if not isinstance(s, str) or not SHA_RE.match(str(ap.get("sample_sha256", ""))):
        errs.append("voice.approved needs sample (the audition sample file) and sample_sha256")
    else:
        sp = s if os.path.isabs(s) else os.path.join(proj, s)
        if not os.path.isfile(sp):
            errs.append(f"the audition sample does not exist: {sp}")
        elif check.sha256(sp) != ap["sample_sha256"]:
            errs.append("the audition sample sha256 does not match (the sample was replaced)")
    return errs


# ---------------- review records ----------------

def _obs_ok(o):
    t = o.get("t") if isinstance(o, dict) else None
    t_ok = isinstance(t, (int, float)) or (isinstance(t, list) and len(t) == 2
                                           and all(isinstance(x, (int, float)) for x in t))
    return t_ok and isinstance(o.get("note"), str) and len(o["note"].strip()) >= 2


def record_errors(r):
    errs = []
    if not isinstance(r, dict):
        return ["a record must be an object"]
    if not SHA_RE.match(str(r.get("video_sha256", ""))):
        errs.append("video_sha256 is not 64 hex characters")
    if not isinstance(r.get("check"), str) or not r["check"].strip():
        errs.append("check is missing")
    if r.get("decision") not in ("accept", "reject"):
        errs.append("decision must be accept / reject")
    if not isinstance(r.get("by"), str) or not r["by"].strip():
        errs.append("by is missing")
    if not isinstance(r.get("note"), str) or len(r["note"].strip()) < 4:
        errs.append("note is shorter than 4 characters (an empty record does not count as handled)")
    if r.get("check") == "final_listen" and not is_agent(r.get("by")):
        if not isinstance(r.get("quote"), str) or len(r["quote"].strip()) < 2:
            errs.append("final_listen needs quote (the words said after listening)")
    else:   # all other review records, and an agent-signed final_listen: verifiable timecoded observations are required
        obs = r.get("observations")
        if not isinstance(obs, list) or not obs or not all(_obs_ok(o) for o in obs):
            errs.append("observations needs at least one {t: seconds or [start,end], note} (a verifiable timecoded observation)")
    return errs


def load_reviews(path):
    if not path:
        return [], None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        d = json.loads(raw)
    except FileNotFoundError:
        raise ReelError("REVIEWS_NOT_FOUND", "review records file not found", path=path)
    except ValueError as e:
        raise ReelError("REVIEWS_INVALID", f"review records are not valid JSON: {e}", path=path)
    recs = d.get("records") if isinstance(d, dict) else d
    if not isinstance(recs, list):
        raise ReelError("REVIEWS_INVALID", "review records need a records array", path=path)
    return recs, check.sha256(path)


def split_reviews(recs, video_sha):
    valid, invalid, stale = [], [], []
    for i, r in enumerate(recs):
        errs = record_errors(r)
        if errs:
            invalid.append({"index": i, "check": r.get("check") if isinstance(r, dict) else None, "errors": errs})
        elif r["video_sha256"] != video_sha:
            stale.append({"index": i, "check": r["check"], "why": "bound to a different file (the video was changed or replaced)"})
        else:
            valid.append(r)
    return valid, invalid, stale


# ---------------- summary ----------------

def evaluate(report, prof, recs=(), reviews_sha=None):
    items = report["items"]
    by_name = {}
    for it in items:
        by_name.setdefault(it["check"], it)
    blockers = []
    req = list(prof["required"]) + [r for r in report.get("contract_required", []) if r not in prof["required"]]  # required checks from the contract cannot be removed by a profile
    for name in req:
        it = by_name.get(name)
        if it is None:
            blockers.append({"check": name, "why": "required check has no result (the detection did not run)"})
        elif it["status"] == "not_checked":
            blockers.append({"check": name, "why": "required check not performed: " + (it.get("note") or it["rule"])})
    for it in items:
        if it.get("error") and it["check"] not in req:
            blockers.append({"check": it["check"], "why": "detection error: " + it.get("note", "")})
        if it["status"] == "fail":
            blockers.append({"check": it["check"], "why": "fail", "evidence": it["evidence"][:3]})
    valid, invalid, stale = split_reviews(recs, report["video_sha256"])
    resolved, pending = [], []
    for it in items:
        if it["status"] != "needs_review":
            continue
        rs = [r for r in valid if r["check"] == it["check"]]
        if any(r["decision"] == "reject" for r in rs):
            blockers.append({"check": it["check"], "why": "review verdict is not a pass", "note": next(
                r["note"] for r in rs if r["decision"] == "reject")})
        elif rs:
            resolved.append({"check": it["check"], "by": rs[0]["by"], "note": rs[0]["note"]})
        else:
            pending.append(it["check"])
    status = "fail" if blockers else ("needs_review" if pending else "pass")
    return {"status": status, "blockers": blockers, "pending_review": pending, "resolved_by_review": resolved,
            "invalid_reviews": invalid, "stale_reviews": stale, "reviews_sha256": reviews_sha,
            "final_reviews": {k: _final(valid, k) for k in ("final_watch", "final_listen")},
            "final_rejected": {k: next((r["note"] for r in valid if r["check"] == k and r["decision"] == "reject"), None)
                               for k in ("final_watch", "final_listen")},
            "not_covered": [it["check"] for it in items if it["status"] == "not_checked" and it["check"] not in req]}


def _final(valid, k):
    """As long as one reject exists for a video, no accept counts; to approve again, first delete that reject explicitly."""
    rs = [r for r in valid if r["check"] == k]
    if not rs or any(r["decision"] == "reject" for r in rs):
        return None
    return {"by": rs[-1]["by"], "note": rs[-1]["note"]}


def text_sources(brief=None, plan=None, subs=(), texts=()):
    plan_subs = None
    if plan is not None and (plan.get("audio") or {}).get("subtitles"):
        from . import plan as planmod
        plan_subs = planmod.resolve(plan, plan["audio"]["subtitles"])
    return textscan.collect(brief=brief, plan=plan, plan_subs=plan_subs, subs=subs, texts=texts)


def brief_profile_ref(brief):
    """The profile named in the brief; a relative path of a project profile is relative to the brief's directory."""
    try:
        with open(brief, encoding="utf-8") as fh:
            ref = json.load(fh).get("profile")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(ref, str) and (os.sep in ref or ref.endswith(".json")) and not os.path.isabs(os.path.expanduser(ref)):
        ref = os.path.join(os.path.dirname(os.path.abspath(brief)), ref)
    return ref if isinstance(ref, str) else None


def brief_kind(brief):
    try:
        with open(brief, encoding="utf-8") as fh:
            k = json.load(fh).get("kind")
    except (OSError, ValueError, AttributeError):
        return None
    return k


def check_video(video, c=None, profile_ref=None, brief=None, subs=(), texts=(), reviews=None, contract=None):
    """--profile wins, then brief.profile, then default: check and deliver use the same thresholds.
    contract: path of the MV acceptance contract (reel.contract.v1); its required checks are merged into required and a profile cannot remove them."""
    if brief and not contract and brief_kind(brief) == "mv":
        raise ReelError("MV_CONTRACT_REQUIRED", "the brief says kind=mv: check and deliver (including --sample) must carry --contract mv.json",
                        brief=os.path.abspath(brief), template=CONTRACT_TEMPLATE)
    if brief and contract and brief_kind(brief) != "mv":
        raise ReelError("BRIEF_KIND_MISSING", "an MV contract was given, so the brief must say \"kind\": \"mv\"", brief=os.path.abspath(brief))
    prof = profiles.load(profile_ref or (brief_profile_ref(brief) if brief else None))
    src = text_sources(brief, c["plan"] if c else None, subs, texts)
    con = None
    if contract:
        from . import mvcheck
        con = mvcheck.load(contract)
    rep = check.run(video, c, prof, src, con)
    recs, rsha = load_reviews(reviews)
    rep["gate"] = evaluate(rep, prof, recs, rsha)
    return rep, prof
