"""EditPlan v1：LLM 决策层的唯一产物。只含素材引用、时间和有类型的操作，不含 filter 或 shell。

{
  "version": "reel.plan.v1",
  "src_dir": "素材目录（asset 相对它）",
  "output": {"width": 1080, "height": 1920, "fps": 60},
  "duration_policy": "require_match" | "fit_last_shot",
  "shots": [{
      "id": "s1", "asset": "a.jpg", "source_in": 0, "duration": 3.2,
      "space": "image" | "cover",               # viewport / move_to 的坐标系：原图，或居中铺满窗
      "viewport": [x0,y0,x1,y1] | null,         # 初始取景窗（归一化）；null=居中铺满
      "camera": [                               # 空数组 = 真正静止
        {"op": "zoom_to", "bbox": [..], "observation_id": "obs-1",   # bbox 永远是原图坐标
         "t0": 0.5, "t1": 3.0, "easing": "ease_in_out", "padding": 0.15, "max_zoom": 2.5},
        {"op": "move_to", "viewport": [..], "t0": .., "t1": .., "easing": "linear"}
      ],
      "transition_out": {"type": "fade", "duration": 0.4}
  }],
  "audio": {"voice": "voice.mp3", "subtitles": "voice.vtt", "bgm": null, "bgm_gain": 0.16,
            "keywords": ["关键词"]},
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
            errs.append(f"{where}: 未知字段 {k!r}")


def validate(plan, observations=None):
    """纯结构/语义校验（不读文件）。返回规范化后的 plan；有错一次性全部报出。"""
    errs = []
    if not isinstance(plan, dict):
        raise ReelError("PLAN_INVALID", "plan 必须是 JSON 对象")
    _keys(plan, {"version", "src_dir", "output", "duration_policy", "shots", "audio", "look"}, "plan", errs)
    if plan.get("version") != VERSION:
        errs.append(f"version 必须是 {VERSION}")
    out = plan.get("output") or {}
    _keys(out, {"width", "height", "fps"}, "output", errs)
    for k in ("width", "height", "fps"):
        if not (isinstance(out.get(k), int) and out.get(k) > 0):
            errs.append(f"output.{k} 必须是正整数")
    if isinstance(out.get("width"), int) and (out["width"] % 2 or out.get("height", 0) % 2):
        errs.append("output 宽高必须是偶数（yuv420p）")
    fps = out.get("fps") if isinstance(out.get("fps"), int) and out.get("fps") > 0 else 30
    pol = plan.setdefault("duration_policy", "require_match")
    if pol not in POLICIES:
        errs.append(f"duration_policy 必须是 {POLICIES}")
    obs = {o["id"]: o for o in (observations or [])}

    shots = plan.get("shots")
    if not isinstance(shots, list) or not shots:
        errs.append("shots 至少一个")
        shots = []
    ids = set()
    for i, s in enumerate(shots):
        w = f"shots[{i}]"
        if not isinstance(s, dict):
            errs.append(f"{w} 必须是对象"); continue
        _keys(s, {"id", "kind", "asset", "web", "highlight", "source_in", "duration", "space", "viewport", "camera",
                  "transition_out", "note"}, w, errs)
        if s.get("kind") == "web":
            _validate_web(s, w, errs)
            s.setdefault("source_in", 0); s.setdefault("viewport", None); s.setdefault("space", "image")
            sid = s.setdefault("id", f"s{i + 1}")
            if sid in ids:
                errs.append(f"{w}: id 重复 {sid!r}")
            ids.add(sid)
            d = s.get("duration")
            if not (_num(d) and d > 0):
                errs.append(f"{w}.duration 必须 >0")
            _validate_transition(s, i, shots, errs, w, d if _num(d) else None)
            continue
        if s.setdefault("space", "image") not in ("image", "cover"):
            errs.append(f"{w}.space 必须是 image / cover")
        sid = s.setdefault("id", f"s{i + 1}")
        if sid in ids:
            errs.append(f"{w}: id 重复 {sid!r}")
        ids.add(sid)
        if not isinstance(s.get("asset"), str) or not s["asset"]:
            errs.append(f"{w}.asset 缺失")
        s.setdefault("source_in", 0)
        if not (_num(s["source_in"]) and s["source_in"] >= 0):
            errs.append(f"{w}.source_in 必须 ≥0")
        d = s.get("duration")
        if not (_num(d) and d > 0):
            errs.append(f"{w}.duration 必须 >0"); d = None
        elif round(d * fps) < 2:
            errs.append(f"{w}.duration 太短（<2 帧）")
        vp = s.setdefault("viewport", None)
        if vp is not None and not _bbox_ok(vp):
            errs.append(f"{w}.viewport 必须是 [x0,y0,x1,y1]，0≤x0<x1≤1")
        cam = s.setdefault("camera", [])
        if not isinstance(cam, list):
            errs.append(f"{w}.camera 必须是数组"); cam = []
        last_t1 = 0.0
        for j, m in enumerate(cam):
            mw = f"{w}.camera[{j}]"
            if not isinstance(m, dict):
                errs.append(f"{mw} 必须是对象"); continue
            op = m.get("op")
            if op == "zoom_to":
                _keys(m, {"op", "bbox", "observation_id", "track", "track_boxes", "t0", "t1", "easing", "padding",
                          "max_zoom"}, mw, errs)
                m.pop("track_boxes", None)          # 派生字段：只能由 track 重新生成，不接受外部提交
                tr = m.get("track")
                if tr is not None:
                    # 跟踪：同一目标在多个源时间点的 observation；框随时间移动，镜头跟随
                    if not (isinstance(tr, list) and len(tr) >= 2 and all(isinstance(x, str) for x in tr)):
                        errs.append(f"{mw}.track 需要 ≥2 个 observation id")
                    elif any(x not in obs for x in tr):
                        errs.append(f"{mw}.track 有不存在的 observation: {[x for x in tr if x not in obs]}")
                    else:
                        ts = [obs[x]["t"] if obs[x].get("t") is not None else 0.0 for x in tr]
                        si = s.get("source_in", 0) if _num(s.get("source_in")) else 0
                        if ts != sorted(ts) or len(set(ts)) != len(ts):
                            errs.append(f"{mw}.track 必须按时间严格递增")
                        if any(obs[x].get("asset") not in (None, s.get("asset")) for x in tr):
                            errs.append(f"{mw}.track 里有别的素材的 observation")
                        if d is not None and any(not (si - 0.5 <= t <= si + d + 0.5) for t in ts):
                            errs.append(f"{mw}.track 的时间点超出本镜头源区间 [{si}, {si + d}]s")
                        m["track_boxes"] = [[t - si, obs[x]["bbox"]] for t, x in zip(ts, tr)]
                        m.setdefault("bbox", obs[tr[0]]["bbox"])
                        if j != len(cam) - 1:
                            errs.append(f"{mw}: 带 track 的 zoom_to 必须是本镜头最后一个运镜")
                oid = m.get("observation_id")
                if oid is not None and oid not in obs:
                    errs.append(f"{mw}.observation_id {oid!r} 不存在（先 locate/observe）")
                if m.get("bbox") is None and oid in obs:
                    m["bbox"] = obs[oid]["bbox"]
                if not _bbox_ok(m.get("bbox")):
                    errs.append(f"{mw}.bbox 缺失或非法")
                elif oid in obs and _iou(m["bbox"], obs[oid]["bbox"]) < 0.5:
                    errs.append(f"{mw}.bbox 与 observation {oid} 不一致（IoU<0.5），不要手改检测框")
                if oid in obs and obs[oid].get("asset") not in (None, s.get("asset")):
                    errs.append(f"{mw}: observation {oid} 属于另一个素材 {obs[oid].get('asset')}")
                if oid in obs and d is not None and _num(s.get("source_in")) and obs[oid].get("t") is not None:
                    ot, si = obs[oid]["t"], s["source_in"]
                    if not (si - 0.5 <= ot <= si + d + 0.5):
                        errs.append(f"{mw}: observation {oid} 取自 t={ot}s，不在本镜头源区间 "
                                    f"[{si}, {si + d}]s 内；单帧框不能套到别的画面（先在本区间内 locate）")
                m.setdefault("padding", 0.15)
                m.setdefault("max_zoom", 3.0)
                if not (_num(m["padding"]) and 0 <= m["padding"] <= 2):
                    errs.append(f"{mw}.padding 必须在 0..2")
                if not (_num(m["max_zoom"]) and m["max_zoom"] >= 1):
                    errs.append(f"{mw}.max_zoom 必须 ≥1")
            elif op == "move_to":
                _keys(m, {"op", "viewport", "t0", "t1", "easing"}, mw, errs)
                if not _bbox_ok(m.get("viewport")):
                    errs.append(f"{mw}.viewport 缺失或非法")
            else:
                errs.append(f"{mw}.op 必须是 zoom_to / move_to"); continue
            m.setdefault("easing", "ease_in_out")
            if m["easing"] not in EASINGS:
                errs.append(f"{mw}.easing 必须是 {EASINGS}")
            t0, t1 = m.get("t0", 0), m.get("t1", d)
            m["t0"], m["t1"] = t0, t1
            if not (_num(t0) and _num(t1) and 0 <= t0 < t1):
                errs.append(f"{mw}: 需要 0≤t0<t1"); continue
            if d is not None and t1 > d + 1e-6:
                errs.append(f"{mw}.t1={t1} 超出镜头时长 {d}")
            if t0 < last_t1 - 1e-6:
                errs.append(f"{mw}: 与上一个运镜时间重叠")
            last_t1 = t1
        _validate_transition(s, i, shots, errs, w, d)

    au = plan.get("audio") or {}
    _keys(au, {"voice", "subtitles", "words", "bgm", "bgm_gain", "keywords"}, "audio", errs)
    if not isinstance(au.get("voice"), str):
        errs.append("audio.voice 缺失（配音是时间线的基准）")
    au.setdefault("subtitles", None)
    au.setdefault("words", None)
    au.setdefault("bgm", None)
    au.setdefault("bgm_gain", 0.16)
    au.setdefault("keywords", [])
    if not (_num(au["bgm_gain"]) and 0 <= au["bgm_gain"] <= 1):
        errs.append("audio.bgm_gain 必须在 0..1")
    look = plan.setdefault("look", {})
    _keys(look, {"grade", "lut"}, "look", errs)
    look.setdefault("grade", False)
    look.setdefault("lut", None)
    plan["audio"] = au
    if errs:
        raise ReelError("PLAN_INVALID", f"plan 有 {len(errs)} 处错误", errors=errs)
    return plan


def _validate_transition(s, i, shots, errs, w, d):
    tr = s.get("transition_out")
    if tr is None:
        return
    if i == len(shots) - 1:
        errs.append(f"{w}: 最后一个镜头不能有 transition_out")
    elif not isinstance(tr, dict) or tr.get("type") not in TRANSITIONS or not _num(tr.get("duration")) \
            or tr["duration"] <= 0:
        errs.append(f"{w}.transition_out 需要 type∈{TRANSITIONS} 且 duration>0")
    else:
        nxt = shots[i + 1] if isinstance(shots[i + 1], dict) else {}
        lim = min(d or 0, nxt.get("duration") or 0) / 2
        if tr["duration"] > lim:
            errs.append(f"{w}.transition_out.duration={tr['duration']} 超过相邻镜头较短者的一半 {lim:.3f}")


HL_STYLES = ("marker", "spotlight", "both")


def _validate_web(s, w, errs):
    """网页镜头：文字位置从 DOM 读，所以 camera/highlight 里写的是原文，不是坐标。"""
    web = s.get("web")
    if not isinstance(web, dict) or not isinstance(web.get("url"), str) or not web["url"]:
        errs.append(f"{w}.web.url 缺失（网址或本地 html 路径）"); return
    _keys(web, {"url", "layout_width", "css"}, f"{w}.web", errs)
    lw = web.get("layout_width")          # 默认在渲染时按输出比例定：竖屏 390（手机排版，字大、整行可见），横屏 1280
    if lw is not None and not (isinstance(lw, int) and 240 <= lw <= 3840):
        errs.append(f"{w}.web.layout_width 必须是 240..3840 的整数")
    cam = s.setdefault("camera", [])
    if not isinstance(cam, list) or len(cam) > 1:
        errs.append(f"{w}.camera：网页镜头最多一个运镜（follow 或 zoom_to）")
    for m in cam if isinstance(cam, list) else []:
        if m.get("op") == "follow":
            _keys(m, {"op", "zoom", "comfort", "move_s", "max_speed", "start"}, f"{w}.camera", errs)
            if m.setdefault("start", "first_line") not in ("first_line", "top"):
                errs.append(f"{w}.camera.start 必须是 first_line / top")
            if not (_num(m.setdefault("max_speed", 0.7)) and 0.1 <= m["max_speed"] <= 3):
                errs.append(f"{w}.camera.max_speed 必须在 0.1..3（画面高度/秒）")
            if not (_num(m.setdefault("zoom", 1.8)) and 1 <= m["zoom"] <= 6):
                errs.append(f"{w}.camera.zoom 必须在 1..6")
            if not (_num(m.setdefault("comfort", 0.28)) and 0.05 <= m["comfort"] <= 0.45):
                errs.append(f"{w}.camera.comfort 必须在 0.05..0.45")
            if not (_num(m.setdefault("move_s", 0.6)) and 0.15 <= m["move_s"] <= 3):
                errs.append(f"{w}.camera.move_s 必须在 0.15..3")
        elif m.get("op") == "zoom_to":
            _keys(m, {"op", "text", "occurrence", "t0", "t1", "easing", "padding", "max_zoom"}, f"{w}.camera", errs)
            if not isinstance(m.get("text"), str) or not m["text"].strip():
                errs.append(f"{w}.camera.zoom_to 需要 text（页面原文）")
            if m.get("easing", "ease_in_out") not in EASINGS:
                errs.append(f"{w}.camera.easing 必须是 {EASINGS}")
        else:
            errs.append(f"{w}.camera.op 必须是 follow / zoom_to")
    hl = s.setdefault("highlight", [])
    if not isinstance(hl, list):
        errs.append(f"{w}.highlight 必须是数组"); return
    for j, h in enumerate(hl):
        hw = f"{w}.highlight[{j}]"
        _keys(h, {"text", "occurrence", "sync", "t0", "t1", "style", "color", "lead"}, hw, errs)
        if not isinstance(h.get("text"), str) or not h["text"].strip():
            errs.append(f"{hw}.text 缺失（页面原文）")
        if h.setdefault("sync", "voice") not in ("voice", "manual"):
            errs.append(f"{hw}.sync 必须是 voice / manual")
        if h["sync"] == "manual" and not (_num(h.get("t0")) and _num(h.get("t1")) and 0 <= h["t0"] < h["t1"]):
            errs.append(f"{hw}: manual 需要 0≤t0<t1（镜头内时间）")
        if h.setdefault("style", "marker") not in HL_STYLES:
            errs.append(f"{hw}.style 必须是 {HL_STYLES}")


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
        raise ReelError("PLAN_NOT_FOUND", f"plan 不存在: {path}")
    except json.JSONDecodeError as e:
        raise ReelError("PLAN_INVALID", f"plan 不是合法 JSON: {e}")


# ---------------- 旧文本 plan 适配 ----------------
# 行格式：文件名 | 入点秒 | 时长秒 | 运镜(in/out/panL/panR/still/drift)
LEGACY_Z = 1.18      # 旧 in/out 的倍率
LEGACY_PAN_Z = 1.12


def _centered(z):
    m = (1 - 1 / z) / 2
    return [m, m, 1 - m, 1 - m]


def legacy_camera(move, dur):
    """旧运镜名 → (viewport, camera)，坐标系为 cover（相对居中铺满窗）。"""
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
    if move == "drift":   # 旧版 still 的实际行为：1.04→1.08 缓推
        return _centered(1.04), [{"op": "move_to", "viewport": _centered(1.08), "t0": 0, "t1": dur,
                                  "easing": "linear"}]
    if move == "still":
        return None, []
    raise ReelError("PLAN_INVALID", f"未知运镜 {move!r}（in/out/panL/panR/still/drift）")


def from_legacy(text_path, src_dir, voice, subtitles=None, width=1080, height=1920, fps=60, xfade=0.0,
                bgm=None, grade=False, lut=None, keywords=None, policy="fit_last_shot"):
    shots = []
    for n, ln in enumerate(open(text_path, encoding="utf-8"), 1):
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        parts = [x.strip() for x in ln.split("|")]
        if len(parts) < 3:
            raise ReelError("PLAN_INVALID", f"{text_path}:{n} 至少需要 文件名|入点|时长", line=ln)
        try:
            ss, dur = float(parts[1]), float(parts[2])
        except ValueError:
            raise ReelError("PLAN_INVALID", f"{text_path}:{n} 入点/时长不是数字", line=ln)
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
