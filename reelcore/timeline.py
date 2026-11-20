"""编译后时间线：把 v1 plan 映射成只读虚拟轨道，给决策层（LLM）读。

轨道：V1 镜头 / C1 字幕 cue / A1 配音（句子，可展开到词）/ BGM。
所有区间都是半开区间 [start_f, end_f)，单位为输出帧；镜头起止直接取自 compile_plan 的
frames / trans / starts，不另算一遍（test_timeline 逐帧断言一致）。
字幕、句子、词的秒数按 round(t*fps) 量化到帧；这是显示用的量化，不包含转写本身的误差。
"""
import json, math
from . import plan as planmod, subtitles


def _s(f, fps):
    return round(f / fps, 3)


def camera_kind(s):
    ops = []
    for m in s.get("camera") or []:
        ops.append("track" if m.get("track_boxes") else m["op"])
    return "+".join(ops) or "static"


def _q(t, fps):
    return int(round(t * fps))


def sentences(plan, fps):
    """audio.subtitles 的每一块 = 一句配音（时间来自字幕文件，timing_method=subtitle_file）。"""
    au = plan["audio"]
    if not au.get("subtitles"):
        return [], []
    ev = subtitles.parse(planmod.resolve(plan, au["subtitles"]))
    sents, cues = [], []
    for k, (a, b, t) in enumerate(ev):
        sid = f"sent-{k + 1:02d}"
        sents.append({"id": sid, "start_s": round(a, 3), "end_s": round(b, 3), "start_f": _q(a, fps),
                      "end_f": _q(b, fps), "text": t, "timing_method": "subtitle_file"})
        for ca, cb, ct in subtitles.cues([(a, b, t)]):      # cues() 逐事件独立处理，逐条调用结果相同
            cues.append({"id": f"cue-{len(cues) + 1:02d}", "start_s": round(ca, 3), "end_s": round(cb, 3),
                         "start_f": _q(ca, fps), "end_f": _q(cb, fps), "text": ct, "sentence": sid})
    return sents, cues


def words(plan, fps):
    au = plan["audio"]
    if not au.get("words"):
        return []
    path = planmod.resolve(plan, au["words"])
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as fh:
            ev = [(w["t0"], w["t1"], w["text"]) for w in json.load(fh)]
        method = "asr_words"
    else:
        ev = subtitles.parse(path, allow_zero=True)
        method = "subtitle_words"
    return [{"id": f"w-{k + 1:04d}", "start_s": round(a, 3), "end_s": round(b, 3), "start_f": _q(a, fps),
             "end_f": _q(b, fps), "text": t, "timing_method": method} for k, (a, b, t) in enumerate(ev)]


def build(c, detail="events", revision=None):
    """compile_plan 的结果 → 时间线 dict。detail=words 时 A1 附逐词。"""
    plan = c["plan"]
    fps = plan["output"]["fps"]
    frames, trans, starts = c["frames"], c["trans"], c["starts"]
    sents, cues = sentences(plan, fps)
    v1 = []
    for i, s in enumerate(plan["shots"]):
        st, n = starts[i], frames[i]
        en = st + n
        m = c["metas"][s["id"]]
        ev = {"id": s["id"], "kind": m["kind"], "start_f": st, "end_f": en, "start_s": _s(st, fps),
              "end_s": _s(en, fps), "frames": n, "camera": camera_kind(s)}
        if m["kind"] == "web":
            ev["url"] = s["web"]["url"]
            ev["highlights"] = [h["text"] for h in s.get("highlight", [])]
        else:
            ev["asset"] = s["asset"]
            ev["source_in"] = s["source_in"]
            if m["kind"] == "video":
                ev["source_out"] = round(s["source_in"] + n / fps, 3)
            ev["static"] = c["cams"][s["id"]].static
            zs = [x.get("observation_id") for x in s["camera"] if x["op"] == "zoom_to" and x.get("observation_id")]
            if zs:
                ev["observations"] = zs
        if trans[i]:
            ev["transition_out"] = {"type": s["transition_out"]["type"], "frames": trans[i],
                                    "overlap_f": [en - trans[i], en]}
        if i and trans[i - 1]:
            ev["transition_in"] = {"type": plan["shots"][i - 1]["transition_out"]["type"], "frames": trans[i - 1],
                                   "overlap_f": [st, st + trans[i - 1]]}
        ev["voice"] = [{"id": x["id"], "text": x["text"]} for x in sents if x["start_f"] < en and x["end_f"] > st]
        v1.append(ev)
    a1 = {"id": "voice-main", "asset": plan["audio"]["voice"], "start_f": 0, "end_f": c["voice_f"],
          "start_s": 0.0, "end_s": round(c["voice_s"], 3), "sentences": sents}
    if detail == "words":
        a1["words"] = words(plan, fps)
    au = plan["audio"]
    bgm = ([{"id": "bgm", "asset": au["bgm"], "gain": au["bgm_gain"], "loop": True, "start_f": 0,
             "end_f": c["voice_f"], "start_s": 0.0, "end_s": round(c["voice_s"], 3)}] if au.get("bgm") else [])
    return {"revision": revision, "detail": detail, "fps": fps, "interval": "[start,end)", "visual_f": c["visual_f"],
            "voice_f": c["voice_f"], "duration_s": _s(c["visual_f"], fps), "voice_s": round(c["voice_s"], 3),
            "adjustments": c["adjustments"], "sentence_timing": "subtitle_file" if sents else None,
            "tracks": {"V1": v1, "C1": cues, "A1": [a1], "BGM": bgm}}


def parse_range(spec, fps, total_f):
    """'A:B'（秒，可省略一端）→ 帧区间 [fa, fb)。"""
    if not spec:
        return 0, total_f
    a, _, b = spec.partition(":")
    fa = int(math.floor(float(a) * fps + 1e-9)) if a.strip() else 0
    fb = int(math.ceil(float(b) * fps - 1e-9)) if b.strip() else total_f
    from .errors import ReelError
    if not (0 <= fa < fb):
        raise ReelError("ARG_INVALID", f"--range 需要 A<B（秒）: {spec}")
    return fa, fb


def _hit(e, fa, fb):
    return e["start_f"] < fb and e["end_f"] > fa


def clip(tl, fa, fb):
    """只保留与 [fa, fb) 相交的事件（事件本身不裁剪，保留真实起止）。"""
    out = dict(tl)
    tr = tl["tracks"]
    a1 = dict(tr["A1"][0])
    a1["sentences"] = [x for x in a1["sentences"] if _hit(x, fa, fb)]
    if "words" in a1:
        a1["words"] = [x for x in a1["words"] if _hit(x, fa, fb)]
    out["tracks"] = {"V1": [x for x in tr["V1"] if _hit(x, fa, fb)], "C1": [x for x in tr["C1"] if _hit(x, fa, fb)],
                     "A1": [a1], "BGM": [x for x in tr["BGM"] if _hit(x, fa, fb)]}
    out["range_f"] = [fa, fb]
    out["range_s"] = [_s(fa, tl["fps"]), _s(fb, tl["fps"])]
    return out


def _t(e):
    return f"{e['start_s']:.3f}–{e['end_s']:.3f}  f[{e['start_f']},{e['end_f']})"


def text(tl):
    fps = tl["fps"]
    L = [f"revision={tl['revision'] or '-'}  fps={fps}  frames={tl['visual_f']} ({tl['duration_s']:.3f}s)  "
         f"voice={tl['voice_s']:.3f}s  interval=[start,end)"]
    if tl.get("range_f"):
        L.append(f"range={tl['range_s'][0]:.3f}–{tl['range_s'][1]:.3f}  f[{tl['range_f'][0]},{tl['range_f'][1]})")
    for a in tl["adjustments"]:
        L.append(f"adjust: {a['shot']} frames {a['frames_from']}→{a['frames_to']} (fit_last_shot)")
    for e in tl["tracks"]["V1"]:
        src = e.get("asset") or e.get("url")
        extra = f"  in={e['source_in']}" + (f"→{e['source_out']}" if "source_out" in e else "") \
            if e["kind"] == "video" else ""
        L.append(f"V1  {e['id']:<6} {e['kind']:<5} {_t(e)}  {e['frames']}f  cam={e['camera']}  src={src}{extra}")
        if e.get("transition_in"):
            ti = e["transition_in"]
            L.append(f"      tr_in ={ti['type']} {ti['frames']}f f[{ti['overlap_f'][0]},{ti['overlap_f'][1]})")
        if e.get("transition_out"):
            to = e["transition_out"]
            L.append(f"      tr_out={to['type']} {to['frames']}f f[{to['overlap_f'][0]},{to['overlap_f'][1]})")
        if e.get("voice") and tl.get("detail") != "summary":
            L.append("      voice: " + " | ".join(f"{v['id']} “{v['text']}”" for v in e["voice"]))
    for e in tl["tracks"]["C1"] if tl.get("detail") != "summary" else []:
        L.append(f"C1  {e['id']:<7} {_t(e)}  “{e['text']}”  ({e['sentence']})")
    a1 = tl["tracks"]["A1"][0]
    L.append(f"A1  {a1['id']}  {_t(a1)}  src={a1['asset']}")
    for e in a1["sentences"] if tl.get("detail") != "summary" else []:
        L.append(f"    {e['id']}  {_t(e)}  “{e['text']}”")
    for e in a1.get("words", []):
        L.append(f"    {e['id']}  {_t(e)}  {e['text']}  [{e['timing_method']}]")
    if tl["tracks"]["BGM"]:
        for e in tl["tracks"]["BGM"]:
            L.append(f"BGM {e['id']}  {_t(e)}  gain={e['gain']} loop  src={e['asset']}")
    else:
        L.append("BGM none")
    return "\n".join(L)


def active_at(tl, f):
    """第 f 帧活跃的事件 ID（scrub 用）。"""
    tr = tl["tracks"]
    hit = lambda e: e["start_f"] <= f < e["end_f"]
    act = {"V1": [e["id"] for e in tr["V1"] if hit(e)], "C1": [e["id"] for e in tr["C1"] if hit(e)],
           "A1": [e["id"] for e in tr["A1"][0]["sentences"] if hit(e)],
           "BGM": [e["id"] for e in tr["BGM"] if hit(e)], "TR": []}
    for e in tr["V1"]:
        to = e.get("transition_out")
        if to and to["overlap_f"][0] <= f < to["overlap_f"][1]:
            act["TR"].append(f"{e['id']}>{to['type']}")
    return act
