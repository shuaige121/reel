"""读 Shotcut / MLT 工程，输出与 reel timeline 同一种文字时间线，并检测重复用料。只读，不改工程。

MLT 结构要点：
- <profile> 给帧率；主 <tractor> 的 <track producer=playlist_id hide=video|audio> 定义轨道顺序（越靠后越上层）。
- <playlist> 里依次是 <entry producer in out>（占 out-in+1 帧）与 <blank length>（空白）。
- entry 指向的 producer/chain 带 resource（素材路径，相对工程目录）；若指向 tractor，则是 Shotcut 的「同轨转场」。
- 滤镜挂在 entry / producer 上：记录 mlt_service 与 shotcut:filter，便于 AI 知道「这里有推近/遮罩/调色」。
"""
import os, re
import xml.etree.ElementTree as ET
from .errors import ReelError


def _props(el):
    return {p.get("name"): (p.text or "") for p in el.findall("property")}


def _frames(v, fps):
    """MLT 时间：整数帧，或 HH:MM:SS.mmm / HH:MM:SS:FF 时间码。"""
    if v is None or v == "":
        return 0
    v = str(v)
    if re.fullmatch(r"-?\d+", v):
        return int(v)
    m = re.fullmatch(r"(\d+):(\d+):(\d+(?:\.\d+)?)", v)
    if m:
        h, mi, s = m.groups()
        return int(round((int(h) * 3600 + int(mi) * 60 + float(s)) * fps))
    m = re.fullmatch(r"(\d+):(\d+):(\d+):(\d+)", v)
    if m:
        h, mi, s, f = map(int, m.groups())
        return int(round((h * 3600 + mi * 60 + s) * fps)) + f
    raise ReelError("MLT_INVALID", f"无法解析的时间 {v!r}")


def _filters(el):
    out = []
    for f in el.findall("filter"):
        pr = _props(f)
        if pr.get("disable") == "1":
            continue
        name = pr.get("shotcut:filter") or pr.get("mlt_service") or "?"
        keys = {k: v[:60] for k, v in pr.items() if k in ("rect", "transition.rect", "level", "gain", "argument", "text",
                                                           "resource", "av.lut3d", "shotcut:animIn", "shotcut:animOut")}
        out.append({"filter": name, **({"params": keys} if keys else {})})
    return out


def load(path):
    if not os.path.isfile(path):
        raise ReelError("ASSET_NOT_FOUND", f"工程文件不存在: {path}")
    if os.path.getsize(path) == 0:
        raise ReelError("MLT_INVALID", "工程文件大小为 0（可能是 iCloud 未下载的占位文件）", path=path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        raise ReelError("MLT_INVALID", f"不是合法的 MLT XML: {e}", path=path)
    prof = root.find("profile")
    fps = float(prof.get("frame_rate_num", 25)) / float(prof.get("frame_rate_den", 1)) if prof is not None else 25.0
    base = os.path.dirname(os.path.abspath(path))
    byid = {el.get("id"): el for el in root if el.get("id")}
    tractors = [el for el in root if el.tag == "tractor"]
    main = byid.get(root.get("producer")) or (tractors[-1] if tractors else None)
    if main is None:
        raise ReelError("MLT_INVALID", "找不到主 tractor")

    def resource(pid):
        el = byid.get(pid)
        if el is None:
            return None, {}
        pr = _props(el)
        res = pr.get("resource") or pr.get("warp_resource") or ""
        if res and not os.path.isabs(res) and not res.startswith(("color:", "#")) and ":" not in res[:3]:
            res = os.path.normpath(os.path.join(pr.get("xml_root") or base, res))
        return res, pr

    tracks = []
    for idx, tr in enumerate(main.findall("track")):
        pl = byid.get(tr.get("producer"))
        if pl is None or pl.tag != "playlist":
            continue
        pp = _props(pl)
        kind = "audio" if (tr.get("hide") == "video" or pp.get("shotcut:audio") == "1") else "video"
        name = pp.get("shotcut:name") or pl.get("id")
        t, evs = 0, []
        for el in pl:
            if el.tag == "blank":
                t += _frames(el.get("length"), fps)
            elif el.tag == "entry":
                a, b = _frames(el.get("in"), fps), _frames(el.get("out"), fps)
                n = b - a + 1
                pid = el.get("producer")
                target = byid.get(pid)
                ev = {"id": f"{pl.get('id')}#{len(evs)}", "start_f": t, "end_f": t + n, "producer": pid,
                      "in_f": a, "out_f": b}
                if target is not None and target.tag == "tractor":
                    # 同轨转场：tractor 里两个 track 各引用一个片段，转场类型在其 transition 里
                    svc = [(_props(x).get("mlt_service"), _props(x).get("shotcut:transition")) for x in target.findall("transition")]
                    ev.update(kind="transition", transition=[s for s in svc if s[0]],
                              between=[x.get("producer") for x in target.findall("track")])
                else:
                    res, pr = resource(pid)
                    ev.update(kind="clip", resource=res, caption=pr.get("shotcut:caption") or os.path.basename(res or ""),
                              service=pr.get("mlt_service"), speed=pr.get("warp_speed"))
                    fl = _filters(el) + (_filters(target) if target is not None else [])
                    if fl:
                        ev["filters"] = fl
                evs.append(ev)
                t += n
        tracks.append({"id": pl.get("id"), "name": name, "kind": kind, "order": idx,
                       "hidden": tr.get("hide") in ("both",) or pp.get("hide") == "1", "events": evs})
    comp = []
    for tr in main.findall("transition"):
        pr = _props(tr)
        comp.append({"service": pr.get("mlt_service"), "a_track": pr.get("a_track"), "b_track": pr.get("b_track"),
                     "disabled": pr.get("disable") == "1"})
    total = max([e["end_f"] for t_ in tracks for e in t_["events"]] + [0])
    return {"file": path, "fps": fps, "frames": total, "seconds": round(total / fps, 3), "tracks": tracks,
            "compositing": comp}


def text(tl, fa=0, fb=None, show_audio=True):
    fps = tl["fps"]; fb = fb if fb is not None else tl["frames"]
    s = lambda f: f"{f / fps:7.2f}"
    lines = [f"MLT {os.path.basename(tl['file'])}  fps={fps:g}  frames={tl['frames']} ({tl['seconds']}s)  "
             f"tracks={len(tl['tracks'])}  interval=[start,end)"]
    for t in tl["tracks"]:
        if t["kind"] == "audio" and not show_audio:
            continue
        evs = [e for e in t["events"] if e["end_f"] > fa and e["start_f"] < fb]
        if not evs:
            continue
        lines.append(f"[{t['id']}] {t['name']} ({t['kind']}{', hidden' if t['hidden'] else ''})")
        for e in evs:
            if e["kind"] == "transition":
                lines.append(f"  {s(e['start_f'])}–{s(e['end_f'])}  TRANSITION {[x[1] or x[0] for x in e['transition']]}")
                continue
            src = f"{e['caption']}  src[{e['in_f'] / fps:.2f}–{(e['out_f'] + 1) / fps:.2f}]"
            fl = ", ".join(f["filter"] for f in e.get("filters", []))
            lines.append(f"  {s(e['start_f'])}–{s(e['end_f'])}  {src}" + (f"  fx: {fl}" if fl else ""))
    return "\n".join(lines)


def duplicates(tl, min_overlap_s=0.3):
    """同一素材文件在视频轨上被多次使用且源区间重叠 ≥min_overlap_s → 观众会看到同一段画面两次。"""
    fps = tl["fps"]
    uses = {}
    for t in tl["tracks"]:
        if t["kind"] != "video" or t["hidden"]:
            continue
        for e in t["events"]:
            res = e.get("resource") or ""
            if e["kind"] != "clip" or not res or e.get("service") in ("color", "colour") \
                    or re.fullmatch(r"(0x[0-9a-fA-F]{6,8}|#[0-9a-fA-F]{3,8}|[a-z]+)", os.path.basename(res)) or t["id"] == "background":
                continue                         # 纯色块/透明占位不是素材
            if os.path.splitext(res)[1].lower() not in (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".webm"):
                continue                         # 图片/SVG 图层反复使用是正常的；只查动态画面
            uses.setdefault(res, []).append((t["id"], e))
    found = []
    for res, lst in uses.items():
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                (ta, a), (tb, b) = lst[i], lst[j]
                ov = min(a["out_f"], b["out_f"]) - max(a["in_f"], b["in_f"]) + 1
                if ov / fps >= min_overlap_s:
                    found.append({"resource": os.path.basename(res), "overlap_s": round(ov / fps, 2),
                                  "first": {"track": ta, "at_s": round(a["start_f"] / fps, 2), "id": a["id"]},
                                  "second": {"track": tb, "at_s": round(b["start_f"] / fps, 2), "id": b["id"]}})
    found.sort(key=lambda d: d["first"]["at_s"])
    return found
