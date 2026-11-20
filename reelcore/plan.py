"""EditPlan v1: the only product of the LLM decision layer. It holds only media references, times and typed operations, with no filter or shell.

{
  "version": "reel.plan.v1",
  "src_dir": "media directory (assets are relative to it)",
  "output": {"width": 1080, "height": 1920, "fps": 60},
  "duration_policy": "require_match" | "fit_last_shot",
  "shots": [{
      "id": "s1", "asset": "a.jpg", "source_in": 0, "duration": 3.2,
      "space": "image" | "cover",               # coordinate system of viewport / move_to: the source image, or the centred cover window
      "viewport": [x0,y0,x1,y1] | null,         # initial framing window (normalised); null = centred cover
      "camera": [                               # empty array = truly static
        {"op": "zoom_to", "bbox": [..], "observation_id": "obs-1",   # bbox is always in source-image coordinates
         "t0": 0.5, "t1": 3.0, "easing": "ease_in_out", "padding": 0.15, "max_zoom": 2.5},
        {"op": "move_to", "viewport": [..], "t0": .., "t1": .., "easing": "linear"}
      ],
      "transition_out": {"type": "fade", "duration": 0.4}
  }],
  "audio": {"voice": "voice.mp3", "subtitles": "voice.vtt", "bgm": null, "bgm_gain": 0.16,
            "keywords": ["keyword"]},
  "look": {"grade": false, "lut": null}
}
"""
import json, math, os
from .errors import ReelError

VERSION = "reel.plan.v1"
EASINGS = ("linear", "ease_in", "ease_out", "ease_in_out")
TRANSITIONS = ("fade", "slideleft", "slideright", "fadeblack", "dissolve", "circleopen")
POLICIES = ("require_match", "fit_last_shot")


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _bbox_ok(b):
    return (isinstance(b, list) and len(b) == 4 and all(_num(x) for x in b)
            and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1)


def _keys(obj, allowed, where, errs):
    for k in obj:
        if k not in allowed:
            errs.append(f"{where}: unknown field {k!r}")


def validate(plan, observations=None):
    """Pure structure/semantic validation (reads no files). Returns the normalised plan; all errors are reported at once."""
    errs = []
    if not isinstance(plan, dict):
        raise ReelError("PLAN_INVALID", "plan must be a JSON object")
    _keys(plan, {"version", "src_dir", "output", "duration_policy", "shots", "audio", "look"}, "plan", errs)
    if plan.get("version") != VERSION:
        errs.append(f"version must be {VERSION}")
    out = plan.get("output") or {}
    _keys(out, {"width", "height", "fps"}, "output", errs)
    for k in ("width", "height", "fps"):
        if not (isinstance(out.get(k), int) and out.get(k) > 0):
            errs.append(f"output.{k} must be a positive integer")
    if isinstance(out.get("width"), int) and (out["width"] % 2 or out.get("height", 0) % 2):
        errs.append("output width and height must be even (yuv420p)")
    fps = out.get("fps") if isinstance(out.get("fps"), int) and out.get("fps") > 0 else 30
    pol = plan.setdefault("duration_policy", "require_match")
    if pol not in POLICIES:
        errs.append(f"duration_policy must be {POLICIES}")
    obs = {o["id"]: o for o in (observations or [])}

    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        errs.append("shots needs at least one")
        shots = []
    ids = set()
    for i, s in enumerate(shots):
        w = f"shots[{i}]"
        if not isinstance(s, dict):
            errs.append(f"{w} must be an object"); continue
        _keys(s, {"id", "kind", "asset", "web", "highlight", "source_in", "duration", "space", "viewport", "camera",
                  "transition_out", "note"}, w, errs)
        if s.get("kind") == "web":
            _validate_web(s, w, errs)
            s.setdefault("source_in", 0); s.setdefault("viewport", None); s.setdefault("space", "image")
            sid = s.setdefault("id", f"s{i + 1}")
            if sid in ids:
                errs.append(f"{w}: duplicate id {sid!r}")
            ids.add(sid)
            d = s.get("duration")
            if not (_num(d) and d > 0):
                errs.append(f"{w}.duration must be >0")
            _validate_transition(s, i, shots, errs, w, d if _num(d) else None)
            continue
        if s.setdefault("space", "image") not in ("image", "cover"):
            errs.append(f"{w}.space must be image / cover")
        sid = s.setdefault("id", f"s{i + 1}")
        if sid in ids:
            errs.append(f"{w}: duplicate id {sid!r}")
        ids.add(sid)
        if not isinstance(s.get("asset"), str) or not s["asset"]:
            errs.append(f"{w}.asset is missing")
        s.setdefault("source_in", 0)
        if not (_num(s["source_in"]) and s["source_in"] >= 0):
            errs.append(f"{w}.source_in must be >=0")
        d = s.get("duration")
        if not (_num(d) and d > 0):
            errs.append(f"{w}.duration must be >0"); d = None
        elif round(d * fps) < 2:
            errs.append(f"{w}.duration is too short (<2 frames)")
        vp = s.setdefault("viewport", None)
        if vp is not None and not _bbox_ok(vp):
            errs.append(f"{w}.viewport must be [x0,y0,x1,y1] with 0<=x0<x1<=1")
        cam = s.setdefault("camera", [])
        if not isinstance(cam, list):
            errs.append(f"{w}.camera must be an array"); cam = []
        last_t1 = 0.0
        for j, m in enumerate(cam):
            mw = f"{w}.camera[{j}]"
            if not isinstance(m, dict):
                errs.append(f"{mw} must be an object"); continue
            op = m.get("op")
            if op == "zoom_to":
                _keys(m, {"op", "bbox", "observation_id", "track", "track_boxes", "t0", "t1", "easing", "padding",
                          "max_zoom"}, mw, errs)
                m.pop("track_boxes", None)          # derived field: it can only be regenerated from track and external submissions are not accepted
                tr = m.get("track")
                if tr is not None:
                    # tracking: observations of the same target at several source times; the box moves over time and the camera follows
                    if not (isinstance(tr, list) and len(tr) >= 2 and all(isinstance(x, str) for x in tr)):
                        errs.append(f"{mw}.track needs at least 2 observation ids")
                    elif any(x not in obs for x in tr):
                        errs.append(f"{mw}.track has observations that do not exist: {[x for x in tr if x not in obs]}")
                    else:
                        ts = [obs[x]["t"] if obs[x].get("t") is not None else 0.0 for x in tr]
                        si = s.get("source_in", 0) if _num(s.get("source_in")) else 0
                        if ts != sorted(ts) or len(set(ts)) != len(ts):
                            errs.append(f"{mw}.track must be strictly increasing in time")
                        if any(obs[x].get("asset") not in (None, s.get("asset")) for x in tr):
                            errs.append(f"{mw}.track contains an observation of a different asset")
                        if d is not None and any(not (si - 0.5 <= t <= si + d + 0.5) for t in ts):
                            errs.append(f"{mw}.track has a time outside this shot's source range [{si}, {si + d}]s")
                        m["track_boxes"] = [[t - si, obs[x]["bbox"]] for t, x in zip(ts, tr)]
                        m.setdefault("bbox", obs[tr[0]]["bbox"])
                        if j != len(cam) - 1:
                            errs.append(f"{mw}: a zoom_to with track must be the last camera move of this shot")
                oid = m.get("observation_id")
                if oid is not None and oid not in obs:
                    errs.append(f"{mw}.observation_id {oid!r} does not exist (run locate/observe first)")
                if m.get("bbox") is None and oid in obs:
                    m["bbox"] = obs[oid]["bbox"]
                if not _bbox_ok(m.get("bbox")):
                    errs.append(f"{mw}.bbox is missing or invalid")
                elif oid in obs and _iou(m["bbox"], obs[oid]["bbox"]) < 0.5:
                    errs.append(f"{mw}.bbox does not match observation {oid} (IoU<0.5); do not edit detection boxes by hand")
                if oid in obs and obs[oid].get("asset") not in (None, s.get("asset")):
                    errs.append(f"{mw}: observation {oid} belongs to another asset {obs[oid].get('asset')}")
                if oid in obs and d is not None and _num(s.get("source_in")) and obs[oid].get("t") is not None:
                    ot, si = obs[oid]["t"], s["source_in"]
                    if not (si - 0.5 <= ot <= si + d + 0.5):
                        errs.append(f"{mw}: observation {oid} was taken at t={ot}s, outside this shot's source range "
                                    f"[{si}, {si + d}]s; a single-frame box cannot be applied to another picture (locate within this range first)")
                m.setdefault("padding", 0.15)
                m.setdefault("max_zoom", 3.0)
                if not (_num(m["padding"]) and 0 <= m["padding"] <= 2):
                    errs.append(f"{mw}.padding must be within 0..2")
                if not (_num(m["max_zoom"]) and m["max_zoom"] >= 1):
                    errs.append(f"{mw}.max_zoom must be >=1")
            elif op == "move_to":
                _keys(m, {"op", "viewport", "t0", "t1", "easing"}, mw, errs)
                if not _bbox_ok(m.get("viewport")):
                    errs.append(f"{mw}.viewport is missing or invalid")
            else:
                errs.append(f"{mw}.op must be zoom_to / move_to"); continue
            m.setdefault("easing", "ease_in_out")
            if m["easing"] not in EASINGS:
                errs.append(f"{mw}.easing must be {EASINGS}")
            t0, t1 = m.get("t0", 0), m.get("t1", d)
            m["t0"], m["t1"] = t0, t1
            if not (_num(t0) and _num(t1) and 0 <= t0 < t1):
                errs.append(f"{mw}: needs 0<=t0<t1"); continue
            if d is not None and t1 > d + 1e-6:
                errs.append(f"{mw}.t1={t1} exceeds the shot duration {d}")
            if t0 < last_t1 - 1e-6:
                errs.append(f"{mw}: overlaps the previous camera move in time")
            last_t1 = t1
        _validate_transition(s, i, shots, errs, w, d)

    au = plan.get("audio") or {}
    _keys(au, {"voice", "subtitles", "words", "bgm", "bgm_gain", "keywords"}, "audio", errs)
    if not isinstance(au.get("voice"), str):
        errs.append("audio.voice is missing (the voice-over is the timeline reference)")
    au.setdefault("subtitles", None)
    au.setdefault("words", None)
    au.setdefault("bgm", None)
    au.setdefault("bgm_gain", 0.16)
    au.setdefault("keywords", [])
    if not (_num(au["bgm_gain"]) and 0 <= au["bgm_gain"] <= 1):
        errs.append("audio.bgm_gain must be within 0..1")
    look = plan.setdefault("look", {})
    _keys(look, {"grade", "lut"}, "look", errs)
    look.setdefault("grade", False)
    look.setdefault("lut", None)
    plan["audio"] = au
    if errs:
        raise ReelError("PLAN_INVALID", f"plan has {len(errs)} errors", errors=errs)
    return plan


def _validate_transition(s, i, shots, errs, w, d):
    tr = s.get("transition_out")
    if tr is None:
        return
    if i == len(shots) - 1:
        errs.append(f"{w}: the last shot cannot have transition_out")
    elif not isinstance(tr, dict) or tr.get("type") not in TRANSITIONS or not _num(tr.get("duration")) \
            or tr["duration"] <= 0:
        errs.append(f"{w}.transition_out needs type in {TRANSITIONS} and duration>0")
    else:
        nxt = shots[i + 1] if isinstance(shots[i + 1], dict) else {}
        lim = min(d or 0, nxt.get("duration") or 0) / 2
        if tr["duration"] > lim:
            errs.append(f"{w}.transition_out.duration={tr['duration']} exceeds half of the shorter adjacent shot {lim:.3f}")


HL_STYLES = ("marker", "spotlight", "both")


def _validate_web(s, w, errs):
    """Web shot: text positions are read from the DOM, so camera/highlight hold the source text, not coordinates."""
    web = s.get("web")
    if not isinstance(web, dict) or not isinstance(web.get("url"), str) or not web["url"]:
        errs.append(f"{w}.web.url is missing (a URL or a local html path)"); return
    _keys(web, {"url", "layout_width", "css"}, f"{w}.web", errs)
    lw = web.get("layout_width")          # the default is decided at render time by output aspect: 390 for portrait (phone layout, large text, whole lines visible), 1280 for landscape
    if lw is not None and not (isinstance(lw, int) and 240 <= lw <= 3840):
        errs.append(f"{w}.web.layout_width must be an integer within 240..3840")
    cam = s.setdefault("camera", [])
    if not isinstance(cam, list) or len(cam) > 1:
        errs.append(f"{w}.camera: a web shot allows at most one camera move (follow or zoom_to)")
    for m in cam if isinstance(cam, list) else []:
        if m.get("op") == "follow":
            _keys(m, {"op", "zoom", "comfort", "move_s", "max_speed", "start"}, f"{w}.camera", errs)
            if m.setdefault("start", "first_line") not in ("first_line", "top"):
                errs.append(f"{w}.camera.start must be first_line / top")
            if not (_num(m.setdefault("max_speed", 0.7)) and 0.1 <= m["max_speed"] <= 3):
                errs.append(f"{w}.camera.max_speed must be within 0.1..3 (picture heights per second)")
            if not (_num(m.setdefault("zoom", 1.8)) and 1 <= m["zoom"] <= 6):
                errs.append(f"{w}.camera.zoom must be within 1..6")
            if not (_num(m.setdefault("comfort", 0.28)) and 0.05 <= m["comfort"] <= 0.45):
                errs.append(f"{w}.camera.comfort must be within 0.05..0.45")
            if not (_num(m.setdefault("move_s", 0.6)) and 0.15 <= m["move_s"] <= 3):
                errs.append(f"{w}.camera.move_s must be within 0.15..3")
        elif m.get("op") == "zoom_to":
            _keys(m, {"op", "text", "occurrence", "t0", "t1", "easing", "padding", "max_zoom"}, f"{w}.camera", errs)
            if not isinstance(m.get("text"), str) or not m["text"].strip():
                errs.append(f"{w}.camera.zoom_to needs text (the page source text)")
            if m.get("easing", "ease_in_out") not in EASINGS:
                errs.append(f"{w}.camera.easing must be {EASINGS}")
        else:
            errs.append(f"{w}.camera.op must be follow / zoom_to")
    hl = s.setdefault("highlight", [])
    if not isinstance(hl, list):
        errs.append(f"{w}.highlight must be an array"); return
    for j, h in enumerate(hl):
        hw = f"{w}.highlight[{j}]"
        _keys(h, {"text", "occurrence", "sync", "t0", "t1", "style", "color", "lead"}, hw, errs)
        if not isinstance(h.get("text"), str) or not h["text"].strip():
            errs.append(f"{hw}.text is missing (the page source text)")
        if h.setdefault("sync", "voice") not in ("voice", "manual"):
            errs.append(f"{hw}.sync must be voice / manual")
        if h["sync"] == "manual" and not (_num(h.get("t0")) and _num(h.get("t1")) and 0 <= h["t0"] < h["t1"]):
            errs.append(f"{hw}: manual needs 0<=t0<t1 (time within the shot)")
        if h.setdefault("style", "marker") not in HL_STYLES:
            errs.append(f"{hw}.style must be {HL_STYLES}")


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def resolve(plan, path):
    base = plan.get("src_dir") or ""
    return path if os.path.isabs(path) else os.path.join(base, path)


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ReelError("PLAN_NOT_FOUND", f"plan does not exist: {path}")
    except json.JSONDecodeError as e:
        raise ReelError("PLAN_INVALID", f"plan is not valid JSON: {e}")


# ---------------- legacy text plan adapter ----------------
# line format: file name | in-point seconds | duration seconds | camera move (in/out/panL/panR/still/drift)
LEGACY_Z = 1.18      # scale factor of the old in/out
LEGACY_PAN_Z = 1.12


def _centered(z):
    m = (1 - 1 / z) / 2
    return [m, m, 1 - m, 1 - m]


def legacy_camera(move, dur):
    """Old camera-move name to (viewport, camera), in the cover coordinate system (relative to the centred cover window)."""
    if move == "in":
        return None, [{"op": "move_to", "viewport": _centered(LEGACY_Z), "t0": 0, "t1": dur, "easing": "linear"}]
    if move == "out":
        return _centered(LEGACY_Z), [{"op": "move_to", "viewport": [0, 0, 1, 1], "t0": 0, "t1": dur,
                                      "easing": "linear"}]
    if move in ("panL", "panR"):
        w = 1 / LEGACY_PAN_Z
        left, right = [0, (1 - w) / 2, w, (1 + w) / 2], [1 - w, (1 - w) / 2, 1, (1 + w) / 2]
        a, b = (left, right) if move == "panL" else (right, left)
        return a, [{"op": "move_to", "viewport": b, "t0": 0, "t1": dur, "easing": "linear"}]
    if move == "drift":   # actual behaviour of the old still: a slow push from 1.04 to 1.08
        return _centered(1.04), [{"op": "move_to", "viewport": _centered(1.08), "t0": 0, "t1": dur,
                                  "easing": "linear"}]
    if move == "still":
        return None, []
    raise ReelError("PLAN_INVALID", f"unknown camera move {move!r}（in/out/panL/panR/still/drift）")


def from_legacy(text_path, src_dir, voice, subtitles=None, width=1080, height=1920, fps=60, xfade=0.0,
                bgm=None, grade=False, lut=None, keywords=None, policy="fit_last_shot"):
    shots = []
    for n, ln in enumerate(open(text_path, encoding="utf-8"), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) < 3:
            raise ReelError("PLAN_INVALID", f"{text_path}:{n} needs at least file name|in-point|duration", line=ln)
        try:
            ss, dur = float(parts[1]), float(parts[2])
        except ValueError:
            raise ReelError("PLAN_INVALID", f"{text_path}:{n} in-point/duration is not a number", line=ln)
        vp, cam = legacy_camera(parts[3] if len(parts) > 3 and parts[3] else "still", dur)
        shots.append({"id": f"s{len(shots) + 1}", "asset": parts[0], "source_in": ss, "duration": dur,
                      "space": "cover", "viewport": vp, "camera": cam})
    if xfade > 0:
        for i, s in enumerate(shots[:-1]):
            s["transition_out"] = {"type": "fade" if i % 2 == 0 else "slideleft", "duration": xfade}
    return {"version": VERSION, "src_dir": src_dir, "output": {"width": width, "height": height, "fps": fps},
            "duration_policy": policy, "shots": shots,
            "audio": {"voice": voice, "subtitles": subtitles, "bgm": bgm, "bgm_gain": 0.16,
                      "keywords": keywords or []},
            "look": {"grade": grade, "lut": lut}}
