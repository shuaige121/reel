"""Check profile: thresholds, required checks, banned text, registered exceptions.

Built-in profiles live in plugins/reel/profiles/<name>.json; a project profile is any JSON file and inherits with "extends".
Merge rules: dicts merge level by level; lists and scalars are replaced whole. The sha256 of the resolved profile's canonical JSON goes into the delivery receipt.
"""
import hashlib, json, os
from .errors import ReelError

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "profiles")
ACTIONS = ("fail", "needs_review")
AUDIO_MODES = ("required", "silent", "optional")


def _merge(base, over):
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _read(ref, seen):
    path = ref if (os.sep in ref or ref.endswith(".json")) else os.path.join(DIR, f"{ref}.json")
    path = os.path.abspath(os.path.expanduser(path))
    if path in seen:
        raise ReelError("PROFILE_INVALID", "profile extends forms a cycle", chain=list(seen) + [path])
    try:
        with open(path, encoding="utf-8") as fh:
            p = json.load(fh)
    except FileNotFoundError:
        raise ReelError("PROFILE_NOT_FOUND", f"profile not found: {ref}", path=path,
                        builtin=sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json")))
    except ValueError as e:
        raise ReelError("PROFILE_INVALID", f"profile is not valid JSON: {e}", path=path)
    if not isinstance(p, dict):
        raise ReelError("PROFILE_INVALID", "profile must be a JSON object", path=path)
    parent = p.get("extends")
    if parent:
        base, chain = _read(parent, seen | {path})
        return _merge(base, {k: v for k, v in p.items() if k != "extends"}), chain + [path]
    return p, [path]


def _validate(p):
    errs = []
    if not isinstance(p.get("required"), list) or not all(isinstance(x, str) for x in p["required"]):
        errs.append("required must be an array of strings")
    if p.get("audio") not in AUDIO_MODES:
        errs.append(f"audio must be one of {AUDIO_MODES}")
    for k in ("long_freeze", "low_motion", "loudness", "true_peak"):
        if (p.get(k) or {}).get("action") not in ACTIONS:
            errs.append(f"{k}.action must be one of {ACTIONS}")
    for k in ("head_action", "tail_action"):
        if (p.get("silence") or {}).get(k) not in ACTIONS:
            errs.append(f"silence.{k} must be one of {ACTIONS}")
    t = p.get("text") or {}
    for k in ("banned", "extra_banned", "allow", "require"):
        if not isinstance(t.get(k, []), list):
            errs.append(f"text.{k} must be an array")
    for i, a in enumerate(p.get("allow") or []):
        ok = (isinstance(a, dict) and isinstance(a.get("check"), str) and isinstance(a.get("t"), list)
              and len(a["t"]) == 2 and all(isinstance(x, (int, float)) for x in a["t"]) and a["t"][0] <= a["t"][1]
              and isinstance(a.get("reason"), str) and len(a["reason"].strip()) >= 4)
        if not ok:
            errs.append(f"allow[{i}] needs check, t=[start,end] (seconds) and a reason of at least 4 characters")
    if errs:
        raise ReelError("PROFILE_INVALID", "profile fields are invalid", errors=errs)


def load(ref=None):
    p, chain = _read(ref or "default", frozenset())
    _validate(p)
    canon = json.dumps(p, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    p["_chain"] = chain
    p["_sha256"] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return p
