"""交付闸门：把 check 报告、审片记录、brief 合成「能不能交付、能交成什么」。

- check 报告里 fail、检测出错、必查项缺失/未检查 → blockers（技术不合格，样片也不能交）
- needs_review 必须有绑定本片 sha256 的审片记录逐项处理；没处理 → 只能登记为待审样片
- 成片（final）另需：brief 字段齐全且 deliverable=final、TTS/克隆声音经试听批准
  （人记原话；或 agent 用 reel voice check 做 STT 核对）、
  最终版有完整看片记录（final_watch）和试听记录（final_listen，静音版除外）。
  final_listen 可以是人（记原话 quote），也可以是 agent（agent 审完即可交成片，用户看后再提意见），
  agent 签的要带时间码听片观察；回执的 approved_by 写明是谁审的。
审片记录是凭证不是真相：只校验形状和绑定关系（hash、时间码、原话），不证明有人真的看过。
"""
import json, os, re
from . import check, profiles, textscan, voicecheck
from .errors import ReelError

BRIEF_VERSION = "reel.brief.v1"
REVIEWS_VERSION = "reel.reviews.v1"
DELIVERABLES = ("final", "sample")
VOICE_MODES = ("tts", "clone", "recorded", "none")
KINDS = (None, "mv")   # kind=mv：check 和 deliver 必须带 --contract（MV 验收契约），样片也一样
CONTRACT_TEMPLATE = {"version": "reel.contract.v1", "kind": "mv", "output": {"width": 1080, "height": 1920, "fps": 24},
                     "songs": [{"path": "歌.m4a", "sha256": "<shasum -a 256>", "at": 0.0, "policy": "full"}],
                     "lyrics": {"path": "x.lyrics.json（reel lyrics import 歌.m4a --lyrics 歌词.md --out x.lyrics.json）",
                                "sha256": "..."},
                     "text_layer": {"path": "字层.mkv（只含歌词字形的灰度遮罩，与成片同长同帧率）", "sha256": "..."}}
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
    """返回 (brief, final_errors, fatal_errors)。fatal：任何交付都不行；final_errors：只能交待审样片。"""
    try:
        with open(path, encoding="utf-8") as fh:
            b = json.load(fh)
    except FileNotFoundError:
        raise ReelError("BRIEF_NOT_FOUND", "找不到 brief", path=path)
    except ValueError as e:
        raise ReelError("BRIEF_INVALID", f"brief 不是合法 JSON：{e}", path=path)
    if not isinstance(b, dict):
        raise ReelError("BRIEF_INVALID", "brief 必须是 JSON 对象", path=path)
    proj = os.path.realpath(os.path.dirname(os.path.abspath(path)))
    final, fatal = [], []
    if b.get("version") != BRIEF_VERSION:
        final.append(f"version 必须是 {BRIEF_VERSION}")
    name = b.get("name")
    if not isinstance(name, str) or not name.strip() or "/" in name:
        fatal.append("name（交付文件名前缀，不含 /）缺失")
    if not isinstance(b.get("language"), str) or not b["language"].strip():
        final.append("language（交付语言：字幕/配音/画面文字）缺失")
    if b.get("deliverable") not in DELIVERABLES:
        final.append(f"deliverable（成片还是样片）必须是 {DELIVERABLES} 之一")
    if not isinstance(b.get("must_not_change"), list):
        final.append("must_not_change（不许改的东西，可以是空数组但必须写）缺失")
    root = b.get("delivery_root")
    if not isinstance(root, str) or not root.strip():
        fatal.append("delivery_root（交付目录）缺失")
    else:
        r = os.path.realpath(os.path.expanduser(root))
        if not os.path.isabs(os.path.expanduser(root)):
            fatal.append("delivery_root 必须是绝对路径或 ~ 开头")
        elif not os.path.isdir(r):
            fatal.append(f"delivery_root 不存在：{r}")
        elif _too_broad(r) or proj == r or proj.startswith(r + os.sep):
            fatal.append(f"delivery_root 太宽（不能是主目录、iCloud 根、项目目录或它的上级）：{r}")
    if b.get("kind") not in KINDS:
        fatal.append(f"kind 只能不写或写 {KINDS[1:]}")
    v = b.get("voice")
    if not isinstance(v, dict) or v.get("mode") not in VOICE_MODES:
        final.append(f"voice.mode 必须是 {VOICE_MODES} 之一（没有配音写 none）")
    elif v["mode"] in ("tts", "clone"):
        final += _voice_errors(v, proj)
    return b, final, fatal


def _voice_errors(v, proj):
    errs = []
    if not isinstance(v.get("voice_id"), str) or not v["voice_id"].strip():
        errs.append("voice.voice_id（用的哪个声音）缺失")
    ap = v.get("approved")
    if not isinstance(ap, dict):
        return errs + ["声音未经试听批准：voice.approved 缺失（出试听样本后 reel voice check 样本 --script 试听文本 --brief brief.json；或人听完记原话）"]
    if ap.get("voice_id") != v.get("voice_id"):
        errs.append(f"批准的是 {ap.get('voice_id')!r}，现在用的是 {v.get('voice_id')!r}：换了声音要重新试听")
    by = ap.get("by")
    if not isinstance(by, str) or not by.strip():
        errs.append("voice.approved.by 缺失（谁批的：人，或用 STT 核对过的 agent）")
    elif is_agent(by):
        errs += voicecheck.approval_errors(ap)
    elif not isinstance(ap.get("quote"), str) or len(ap["quote"].strip()) < 2:
        errs.append("voice.approved.quote 缺失（人批的要记下批准时的原话）")
    s = ap.get("sample")
    if not isinstance(s, str) or not SHA_RE.match(str(ap.get("sample_sha256", ""))):
        errs.append("voice.approved 需要 sample（试听样本文件）和 sample_sha256")
    else:
        sp = s if os.path.isabs(s) else os.path.join(proj, s)
        if not os.path.isfile(sp):
            errs.append(f"试听样本不存在：{sp}")
        elif check.sha256(sp) != ap["sample_sha256"]:
            errs.append("试听样本的 sha256 对不上（样本被换过）")
    return errs


# ---------------- 审片记录 ----------------

def _obs_ok(o):
    t = o.get("t") if isinstance(o, dict) else None
    t_ok = isinstance(t, (int, float)) or (isinstance(t, list) and len(t) == 2
                                           and all(isinstance(x, (int, float)) for x in t))
    return t_ok and isinstance(o.get("note"), str) and len(o["note"].strip()) >= 2


def record_errors(r):
    errs = []
    if not isinstance(r, dict):
        return ["记录必须是对象"]
    if not SHA_RE.match(str(r.get("video_sha256", ""))):
        errs.append("video_sha256 不是 64 位十六进制")
    if not isinstance(r.get("check"), str) or not r["check"].strip():
        errs.append("check 缺失")
    if r.get("decision") not in ("accept", "reject"):
        errs.append("decision 必须是 accept / reject")
    if not isinstance(r.get("by"), str) or not r["by"].strip():
        errs.append("by 缺失")
    if not isinstance(r.get("note"), str) or len(r["note"].strip()) < 4:
        errs.append("note 少于 4 个字（空记录不算处理）")
    if r.get("check") == "final_listen" and not is_agent(r.get("by")):
        if not isinstance(r.get("quote"), str) or len(r["quote"].strip()) < 2:
            errs.append("final_listen 需要 quote（听完后的原话）")
    else:   # 其余审片记录，以及 agent 签的 final_listen：要可核查的时间码观察
        obs = r.get("observations")
        if not isinstance(obs, list) or not obs or not all(_obs_ok(o) for o in obs):
            errs.append("observations 需要至少一条 {t: 秒或[起,止], note}（可核查的时间码观察）")
    return errs


def load_reviews(path):
    if not path:
        return [], None
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
        d = json.loads(raw)
    except FileNotFoundError:
        raise ReelError("REVIEWS_NOT_FOUND", "找不到审片记录文件", path=path)
    except ValueError as e:
        raise ReelError("REVIEWS_INVALID", f"审片记录不是合法 JSON：{e}", path=path)
    recs = d.get("records") if isinstance(d, dict) else d
    if not isinstance(recs, list):
        raise ReelError("REVIEWS_INVALID", "审片记录需要 records 数组", path=path)
    return recs, check.sha256(path)


def split_reviews(recs, video_sha):
    valid, invalid, stale = [], [], []
    for i, r in enumerate(recs):
        errs = record_errors(r)
        if errs:
            invalid.append({"index": i, "check": r.get("check") if isinstance(r, dict) else None, "errors": errs})
        elif r["video_sha256"] != video_sha:
            stale.append({"index": i, "check": r["check"], "why": "绑定的不是这个文件（成片改过或换过）"})
        else:
            valid.append(r)
    return valid, invalid, stale


# ---------------- 汇总 ----------------

def evaluate(report, prof, recs=(), reviews_sha=None):
    items = report["items"]
    by_name = {}
    for it in items:
        by_name.setdefault(it["check"], it)
    blockers = []
    req = list(prof["required"]) + [r for r in report.get("contract_required", []) if r not in prof["required"]]  # 契约必查项 profile 去不掉
    for name in req:
        it = by_name.get(name)
        if it is None:
            blockers.append({"check": name, "why": "必查项没有结果（检测漏跑）"})
        elif it["status"] == "not_checked":
            blockers.append({"check": name, "why": "必查项未检查：" + (it.get("note") or it["rule"])})
    for it in items:
        if it.get("error") and it["check"] not in req:
            blockers.append({"check": it["check"], "why": "检测出错：" + it.get("note", "")})
        if it["status"] == "fail":
            blockers.append({"check": it["check"], "why": "fail", "evidence": it["evidence"][:3]})
    valid, invalid, stale = split_reviews(recs, report["video_sha256"])
    resolved, pending = [], []
    for it in items:
        if it["status"] != "needs_review":
            continue
        rs = [r for r in valid if r["check"] == it["check"]]
        if any(r["decision"] == "reject" for r in rs):
            blockers.append({"check": it["check"], "why": "审片判定不通过", "note": next(
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
    """同一成片只要有一条 reject，任何 accept 都不算；要重新批准，先显式删掉那条 reject。"""
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
    """brief 里写的 profile；项目 profile 的相对路径相对 brief 所在目录。"""
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
    """--profile 优先，其次 brief.profile，最后 default：check 和 deliver 用同一套阈值。
    contract：MV 验收契约（reel.contract.v1）路径；它的必查项并进 required，profile 去不掉。"""
    if brief and not contract and brief_kind(brief) == "mv":
        raise ReelError("MV_CONTRACT_REQUIRED", "brief 标了 kind=mv：check 和 deliver（含 --sample）都必须带 --contract mv.json",
                        brief=os.path.abspath(brief), template=CONTRACT_TEMPLATE)
    if brief and contract and brief_kind(brief) != "mv":
        raise ReelError("BRIEF_KIND_MISSING", "带了 MV 契约，brief 要写 \"kind\": \"mv\"", brief=os.path.abspath(brief))
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
