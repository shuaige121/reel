"""Compiled timeline: maps a v1 plan onto read-only virtual tracks for the decision layer (the LLM) to read.

Tracks: V1 shots / C1 subtitle cues / A1 voice-over (sentences, expandable to words) / BGM.
All ranges are half-open [start_f, end_f) in output frames; shot starts and ends are taken directly from compile_plan's
frames / trans / starts and not recomputed (test_timeline asserts they match frame by frame).
Seconds of subtitles, sentences and words are quantised to frames with round(t*fps); this is display quantisation and does not include transcription error itself.
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
    """Each block of audio.subtitles is one voice-over sentence (timing comes from the subtitle file, timing_method=subtitle_file)."""
    au = plan["audio"]
    if not au.get("subtitles"):
        return [], []
    ev = subtitles.parse(planmod.resolve(plan, au["subtitles"]))
    sents, cues = [], []
    for k, (a, b, t) in enumerate(ev):
        sid = f"sent-{k + 1:02d}"
        sents.append({"id": sid, "start_s": round(a, 3), "end_s": round(b, 3), "start_f": _q(a, fps),
                      "end_f": _q(b, fps), "text": t, "timing_method": "subtitle_file"})
        for ca, cb, ct in subtitles.cues([(a, b, t)]):      # cues() handles each event independently, so calling it one by one gives the same result
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
    """The result of compile_plan becomes a timeline dict. With detail=words, A1 carries per-word entries."""
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
    """'A:B' (seconds, either end may be omitted) to a frame range [fa, fb)."""
    if not spec:
        return 0, total_f
    a, _, b = spec.partition(":")
    fa = int(math.floor(float(a) * fps + 1e-9)) if a.strip() else 0
    fb = int(math.ceil(float(b) * fps - 1e-9)) if b.strip() else total_f
    from .errors import ReelError
    if not (0 <= fa < fb):
        raise ReelError("ARG_INVALID", f"--range needs A<B (seconds): {spec}")
    return fa, fb


def _hit(e, fa, fb):
    return e["start_f"] < fb and e["end_f"] > fa


def clip(tl, fa, fb):
    """Keep only events that intersect [fa, fb) (events themselves are not trimmed; their real start and end are kept)."""
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
    """IDs of the events active at frame f (used by scrub)."""
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
