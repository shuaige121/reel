"""Images for the decision layer: a multi-track timeline PNG (+ JSON sidecar) and scrub frame extraction (clean frames + annotated frames + index.json).

The picture source must always be stated:
  render@…        the real frame from the final video (--render)
  plan_preview    cropped directly from the media by the compiled framing window (no subtitle burn-in; transitions approximated as a fade)
  web_placeholder web shots are only drawn by the browser at render time; only a text placeholder is given here
"""
import io, json, math, os, re, subprocess
from .errors import ReelError
from . import plan as planmod, timeline as TL

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ReelError("BACKEND_UNAVAILABLE", "Pillow is required (python3 -m pip install pillow)")
    return Image, ImageDraw, ImageFont


class Fonts:
    def __init__(self, candidates=None):
        _, _, ImageFont = _pil()
        self.path = next((p for p in (candidates or FONT_CANDIDATES) if os.path.isfile(p)), None)
        self.fallback = self.path is None
        self._ImageFont, self._cache = ImageFont, {}

    def __call__(self, size):
        if size not in self._cache:
            self._cache[size] = (self._ImageFont.truetype(self.path, size) if self.path
                                 else self._ImageFont.load_default(size=size))
        return self._cache[size]

    def info(self):
        return {"font": self.path, "font_fallback": self.fallback,
                "note": None if self.path else "no CJK font found; using the PIL default font, Chinese may show as boxes"}


# ---------------- frame extraction ----------------

def _start_time(video):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
                        "-of", "csv=p=0", video], capture_output=True, text=True)
    try:
        return float(p.stdout.strip().split(",")[0])
    except ValueError:
        return 0.0


def grab(video, t):
    """Take the first frame with timestamp >= t (accurate input-side seek). Returns (PIL.Image, actual pts in seconds)."""
    Image, _, _ = _pil()
    p = subprocess.run(["ffmpeg", "-hide_banner", "-copyts", "-ss", f"{max(0.0, t):.6f}", "-i", video,
                        "-frames:v", "1", "-vf", "showinfo", "-f", "image2pipe", "-vcodec", "png", "-"],
                       capture_output=True)
    err = p.stderr.decode("utf-8", "replace")
    if p.returncode != 0 or not p.stdout:
        raise ReelError("FFMPEG_FAILED", f"frame extraction failed t={t:.3f}", path=video, stderr=err[-400:])
    m = re.search(r"n:\s*0\s.*?pts_time:([-0-9.]+)", err)
    return Image.open(io.BytesIO(p.stdout)).convert("RGB"), (float(m.group(1)) if m else None)


def grab_frame(video, f, fps, start=None):
    """Frame f (0-based) of the final video. Seek to (f-0.25)/fps and take the first frame with pts at or after that; the actual frame number is returned for checking."""
    start = _start_time(video) if start is None else start
    for back in range(4):          # the last frame from mpv carries a discard flag and cannot be decoded: step back and report the actual frame number honestly
        try:
            img, pts = grab(video, start + (f - back - 0.25) / fps)
            break
        except ReelError:
            if back == 3 or f - back - 1 < 0:
                raise
    actual = int(round((pts - start) * fps)) if pts is not None else None
    return img, actual, pts


class PlanFrames:
    """Compose an approximate picture from the media using the compile result (the framing window comes from the same ShotCamera.at as rendering)."""

    def __init__(self, c, size=None):
        self.c = c
        o = c["plan"]["output"]
        self.W, self.H = size or (o["width"], o["height"])
        self._img = {}

    def _src(self, path):
        Image, _, _ = _pil()
        if path not in self._img:
            self._img[path] = Image.open(path).convert("RGB")
        return self._img[path]

    def placeholder(self, lines, fonts):
        Image, ImageDraw, _ = _pil()
        im = Image.new("RGB", (self.W, self.H), (40, 44, 52))
        d = ImageDraw.Draw(im)
        fs = max(14, self.W // 22)
        y = self.H // 3
        for ln in lines:
            for k in range(0, len(ln), 18):
                d.text((self.W // 12, y), ln[k:k + 18], fill=(230, 230, 230), font=fonts(fs))
                y += int(fs * 1.4)
        return im

    def shot(self, i, local, fonts):
        """Frame local within shot i gives (image, source)."""
        Image, _, _ = _pil()
        c = self.c
        s = c["plan"]["shots"][i]
        m = c["metas"][s["id"]]
        fps = c["plan"]["output"]["fps"]
        if m["kind"] == "web":
            return self.placeholder([f"[web] {s['id']}", os.path.basename(s["web"]["url"]),
                                     "web shots are rendered by the browser; add --render to see the real picture"], fonts), "web_placeholder"
        r = c["cams"][s["id"]].at(local)
        if m["kind"] == "image":
            src = self._src(m["path"])
        else:
            src, _ = grab(m["path"], s["source_in"] + local / fps)
        return src.transform((self.W, self.H), Image.EXTENT, tuple(r), Image.BICUBIC), "plan_preview"

    def frame(self, f, fonts):
        """Frame f of the whole video: in a transition overlap the two shots are blended as an approximate fade."""
        Image, _, _ = _pil()
        c = self.c
        idx = [i for i, st in enumerate(c["starts"]) if st <= f < st + c["frames"][i]]
        if not idx:
            return self.placeholder([f"f{f} is out of range"], fonts), "out_of_range", []
        a, src = self.shot(idx[0], f - c["starts"][idx[0]], fonts)
        if len(idx) > 1:
            b, src_b = self.shot(idx[1], f - c["starts"][idx[1]], fonts)
            t = c["trans"][idx[0]]
            k = f - c["starts"][idx[1]]
            a = Image.blend(a, b, (k + 1) / (t + 1))
            src = f"{src}+{src_b}(transition≈blend)"
        return a, src, idx


# ---------------- waveform / camera sampling ----------------

def waveform(path, a_s, b_s, cols, rate=8000):
    """[a_s, b_s) mono peak envelope, one 0..1 value per column."""
    import array
    dur = max(0.0, b_s - a_s)
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{a_s:.4f}", "-t", f"{dur:.4f}", "-i", path, "-ac", "1",
                        "-ar", str(rate), "-f", "s16le", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "audio decoding failed", path=path, stderr=p.stderr.decode()[-300:])
    x = array.array("h")
    x.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    n = len(x)
    total = max(1, int(round(dur * rate)))
    out = []
    for k in range(cols):
        i0, i1 = k * total // cols, (k + 1) * total // cols
        seg = x[i0:min(i1, n)]
        out.append((max(max(seg), -min(seg)) / 32768.0) if len(seg) else 0.0)
    return out


def camera_speed(cam, fps):
    """Per frame: pan = framing-centre speed (window widths per second), zoom = d ln(1/width)/dt (1/s, positive = push in)."""
    pan, zoom = [], []
    prev = cam.at(0)
    for f in range(1, cam.n):
        r = cam.at(f)
        w0, w1 = prev[2] - prev[0], r[2] - r[0]
        dx = (r[0] + r[2] - prev[0] - prev[2]) / 2
        dy = (r[1] + r[3] - prev[1] - prev[3]) / 2
        pan.append(math.hypot(dx, dy) / w1 * fps)
        zoom.append(math.log(w0 / w1) * fps)
        prev = r
    return pan, zoom


# ---------------- multi-track PNG ----------------

COL = {"bg": (250, 250, 248), "grid": (220, 220, 215), "text": (30, 30, 30), "dim": (120, 120, 120),
       "shot": (70, 110, 170), "tr": (230, 140, 40), "cue": (60, 150, 100), "sent": (120, 90, 170),
       "wave": (90, 90, 90), "bgm": (170, 120, 60), "pan": (40, 120, 210), "zoom": (210, 60, 60)}


def _fit(d, text, font, width):
    """Truncate to fit within width pixels (labels do not run over adjacent shots)."""
    while text and d.textlength(text, font=font) > width:
        text = text[:-1]
    return text


def draw_timeline(c, tl, out_png, fa, fb, width=1600, render_video=None, fonts=None, beats=None, beat_offset=0.0):
    Image, ImageDraw, _ = _pil()
    fonts = fonts or Fonts()
    fps = tl["fps"]
    LM, RM = 96, 16
    ppf = (width - LM - RM) / (fb - fa)
    X = lambda f: LM + (min(max(f, fa), fb) - fa) * ppf
    rows = [("ruler", 30), ("V1", 170), ("C1", 50), ("A1", 110), ("BGM/TR", 34)] + \
        ([("BEAT", 46)] if beats else []) + [("CAM", 120)]
    H = sum(h for _, h in rows) + 10
    im = Image.new("RGB", (width, H), COL["bg"])
    d = ImageDraw.Draw(im)
    ys, y = {}, 0
    for name, h in rows:
        ys[name] = (y, y + h)
        d.line([(0, y), (width, y)], fill=COL["grid"])
        if name != "ruler":
            d.text((6, y + 4), name, fill=COL["text"], font=fonts(15))
        y += h
    side = {"png": out_png, "range_f": [fa, fb], "range_s": [round(fa / fps, 3), round(fb / fps, 3)],
            "fps": fps, "revision": tl.get("revision"), "width": width, "height": H,
            "x_map": {"x0": LM, "px_per_frame": ppf, "formula": "x = x0 + (frame - range_f[0]) * px_per_frame"},
            **fonts.info(), "tracks": []}

    # ticks
    span = (fb - fa) / fps
    step = next(s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120) if span / s <= 16)
    t = math.ceil(fa / fps / step) * step
    y0, y1 = ys["ruler"]
    while t * fps < fb:
        x = X(t * fps)
        d.line([(x, y0 + 18), (x, H)], fill=COL["grid"])
        d.text((x + 2, y0 + 2), f"{t:g}s", fill=COL["dim"], font=fonts(13))
        t += step

    # V1 thumbnails
    y0, y1 = ys["V1"]
    pf = PlanFrames(c)
    evs = []
    for e in tl["tracks"]["V1"]:
        i = next(k for k, s in enumerate(c["plan"]["shots"]) if s["id"] == e["id"])
        x0, x1 = X(e["start_f"]), X(e["end_f"])
        lane = i % 2                            # adjacent shots are staggered by half a lane so both are visible in a transition overlap
        by0, by1 = y0 + 22 + lane * 6, y1 - 20 + lane * 6 - 6
        d.rectangle([x0, by0, x1, by1], outline=COL["shot"], width=2)
        if render_video:
            ex0 = e["start_f"] + (e["transition_in"]["frames"] if e.get("transition_in") else 0)
            ex1 = e["end_f"] - (e["transition_out"]["frames"] if e.get("transition_out") else 0)
            mf = (ex0 + ex1) // 2
            img, actual, _ = grab_frame(render_video, mf, fps)
            src = f"render@f{actual}"
        else:
            img, kind = pf.shot(i, 0, fonts)
            src = "asset_first_frame(plan_crop)" if kind == "plan_preview" else kind
        th = by1 - by0 - 4
        tw = max(1, int(th * img.width / img.height))
        thumb = img.resize((tw, th))
        bw = int(x1 - x0) - 4
        if bw > 4:
            if tw > bw:
                thumb = thumb.crop(((tw - bw) // 2, 0, (tw - bw) // 2 + bw, th))
            im.paste(thumb, (int(x0 + 2 + max(0, (bw - thumb.width) // 2)), int(by0 + 2)))
        lab = _fit(d, f"{e['id']} {e['kind']} {e['end_s'] - e['start_s']:.2f}s", fonts(13), x1 - x0 - 4)
        d.text((x0 + 3, y0 + 3), lab, fill=COL["text"], font=fonts(13))
        d.text((x0 + 3, y1 - 16), _fit(d, e["camera"], fonts(12), x1 - x0 - 4), fill=COL["dim"], font=fonts(12))
        evs.append({"id": e["id"], "start_f": e["start_f"], "end_f": e["end_f"], "x": [round(x0, 1), round(x1, 1)],
                    "thumb_source": src})
    side["tracks"].append({"track": "V1", "y": [y0, y1], "events": evs})

    # C1 subtitles
    y0, y1 = ys["C1"]
    evs = []
    for k, e in enumerate(tl["tracks"]["C1"]):
        x0, x1 = X(e["start_f"]), X(e["end_f"])
        ly = y0 + 22 + (k % 2) * 12
        d.rectangle([x0, ly, max(x0 + 1, x1 - 1), ly + 12], fill=COL["cue"])
        d.text((x0 + 2, y0 + 4 if k % 2 == 0 else y1 - 14), _fit(d, e["text"], fonts(12), x1 - x0 - 2),
               fill=COL["text"], font=fonts(12))
        evs.append({"id": e["id"], "start_f": e["start_f"], "end_f": e["end_f"], "x": [round(x0, 1), round(x1, 1)]})
    side["tracks"].append({"track": "C1", "y": [y0, y1], "events": evs})

    # A1 waveform + sentences
    y0, y1 = ys["A1"]
    a1 = tl["tracks"]["A1"][0]
    cols = width - LM - RM
    voice = planmod.resolve(c["plan"], c["plan"]["audio"]["voice"])
    wav = waveform(voice, fa / fps, fb / fps, cols)
    mid, amp = y0 + 40, 30
    peak = max(wav) if wav else 0.0
    norm = peak if peak > 1e-4 else 1.0          # vertical axis is normalised to the range peak (quiet audio is still readable), the peak is labelled separately
    for k, v in enumerate(wav):
        d.line([(LM + k, mid - v / norm * amp), (LM + k, mid + v / norm * amp)], fill=COL["wave"])
    peak_db = round(20 * math.log10(peak), 1) if peak > 0 else None
    d.text((6, y0 + 22), f"peak {peak_db} dBFS" if peak_db is not None else "silent", fill=COL["dim"], font=fonts(11))
    evs = []
    for k, e in enumerate(a1["sentences"]):
        x0, x1 = X(e["start_f"]), X(e["end_f"])
        ly = y1 - 34 + (k % 2) * 16
        d.rectangle([x0, ly, max(x0 + 1, x1 - 1), ly + 3], fill=COL["sent"])
        d.text((x0 + 1, ly + 3), _fit(d, e["text"], fonts(11), x1 - x0 - 1), fill=COL["sent"], font=fonts(11))
        evs.append({"id": e["id"], "start_f": e["start_f"], "end_f": e["end_f"], "x": [round(x0, 1), round(x1, 1)]})
    side["tracks"].append({"track": "A1", "y": [y0, y1], "events": evs, "waveform": "peak, mono, 8kHz decode, normalized to range peak",
                           "peak_dbfs": peak_db})

    # BGM + transition markers
    y0, y1 = ys["BGM/TR"]
    evs = []
    for e in tl["tracks"]["BGM"]:
        x0, x1 = X(e["start_f"]), X(e["end_f"])
        d.rectangle([x0, y0 + 6, x1, y0 + 14], fill=COL["bgm"])
        d.text((x0 + 4, y0 + 16), f"BGM {os.path.basename(e['asset'])} gain={e['gain']} loop", fill=COL["bgm"],
               font=fonts(11))
        evs.append({"id": e["id"], "start_f": e["start_f"], "end_f": e["end_f"], "x": [round(x0, 1), round(x1, 1)]})
    if not tl["tracks"]["BGM"]:
        d.text((LM + 4, y0 + 8), "BGM none", fill=COL["dim"], font=fonts(12))
    for e in tl["tracks"]["V1"]:
        to = e.get("transition_out")
        if to and to["overlap_f"][1] > fa and to["overlap_f"][0] < fb:
            x0, x1 = X(to["overlap_f"][0]), X(to["overlap_f"][1])
            d.rectangle([x0, y0 + 2, max(x0 + 2, x1), y1 - 2], outline=COL["tr"], width=2)
            for yy in (ys["V1"][0] + 20, ys["V1"][1] - 4):
                d.line([(x0, yy), (max(x0 + 2, x1), yy)], fill=COL["tr"], width=3)
            d.text((x1 + 2, y0 + 18), to["type"], fill=COL["tr"], font=fonts(11))
            evs.append({"id": f"{e['id']}>tr", "type": to["type"], "start_f": to["overlap_f"][0],
                        "end_f": to["overlap_f"][1], "x": [round(x0, 1), round(x1, 1)]})
    side["tracks"].append({"track": "BGM/TR", "y": [y0, y1], "events": evs})

    # beat track: thin line = beat, thick line = downbeat; each cut's offset to the nearest beat (green if within 1 frame, otherwise red)
    if beats:
        y0, y1 = ys["BEAT"]
        bt = [x + beat_offset for x in beats.get("beats", [])]
        db = set(round(x + beat_offset, 4) for x in beats.get("downbeats", []))
        for x in bt:
            f = x * fps
            if fa <= f <= fb:
                strong = round(x, 4) in db
                d.line([(X(f), y0 + (4 if strong else 14)), (X(f), y0 + 26)], fill=COL["text"] if strong else COL["dim"],
                       width=3 if strong else 1)
        cuts = []
        for e in tl["tracks"]["V1"][1:]:
            f = e["start_f"]
            if not (fa <= f <= fb) or not bt:
                continue
            near = min(bt, key=lambda b: abs(b * fps - f))
            off_ms = (f / fps - near) * 1000
            ok = abs(f - near * fps) <= 1.0 + 1e-6
            col = (40, 150, 70) if ok else (200, 50, 50)
            d.line([(X(f), y0 + 2), (X(f), y1 - 2)], fill=col, width=2)
            d.text((X(f) + 3, y0 + 28), f"{off_ms:+.0f}ms", fill=col, font=fonts(10))
            cuts.append({"cut_f": f, "nearest_beat_s": round(near, 4), "offset_ms": round(off_ms, 1), "on_beat": ok})
        side["tracks"].append({"track": "BEAT", "y": [y0, y1], "bpm": beats.get("bpm"), "backend": beats.get("backend"),
                               "cuts": cuts, "on_beat": sum(c_["on_beat"] for c_ in cuts), "total_cuts": len(cuts)})

    # camera speed curve
    y0, y1 = ys["CAM"]
    series = []
    for e in tl["tracks"]["V1"]:
        if e["kind"] == "web":
            x0, x1 = X(e["start_f"]), X(e["end_f"])
            d.text((x0 + 3, y0 + 40), "web: skipped", fill=COL["dim"], font=fonts(11))
            continue
        cam = c["cams"][e["id"]]
        pan, zoom = camera_speed(cam, fps)
        series.append((e, pan, zoom))
    top = max([0.05] + [abs(v) for _, p, z in series for v in p + z])
    base_y, sc = y1 - 14, (y1 - y0 - 34) / top
    d.line([(LM, base_y), (width - RM, base_y)], fill=COL["grid"])
    cam_ev = []
    for e, pan, zoom in series:
        for vals, col in ((pan, COL["pan"]), (zoom, COL["zoom"])):
            pts = [(X(e["start_f"] + 1 + k), base_y - v * sc) for k, v in enumerate(vals)
                   if fa <= e["start_f"] + 1 + k < fb]
            if len(pts) > 1:
                d.line(pts, fill=col, width=2)
        cam_ev.append({"id": e["id"], "max_pan": round(max(pan or [0]), 4), "max_zoom_rate": round(max(zoom or [0]), 4)})
    d.text((LM + 4, y0 + 4), f"pan window-widths/s (blue)  zoom d ln(1/width)/dt 1/s (red, positive = push in)  full scale={top:.3f}",
           fill=COL["dim"], font=fonts(11))
    side["tracks"].append({"track": "CAM", "y": [y0, y1], "events": cam_ev, "full_scale": round(top, 4),
                           "units": {"pan": "viewport widths per second (engine camera only, not source motion)",
                                     "zoom": "d ln(1/viewport width)/dt, 1/s, positive = push in"}})
    im.save(out_png)
    sidecar = os.path.splitext(out_png)[0] + ".json"
    with open(sidecar, "w", encoding="utf-8") as fh:
        json.dump(side, fh, ensure_ascii=False, indent=1)
    return {"png": out_png, "sidecar": sidecar, "range_f": [fa, fb], **fonts.info(),
            "thumb_sources": sorted({e["thumb_source"] for e in side["tracks"][0]["events"]})}


# ---------------- scrub ----------------

def _overlay(im, c, tl, f, idx, overlays, fonts, header):
    Image, ImageDraw, _ = _pil()
    a = im.copy()
    d = ImageDraw.Draw(a, "RGBA")
    W, H = a.size
    fs = max(12, W // 40)
    if "safe" in overlays:
        for m, col in ((0.05, (255, 200, 0, 200)), (0.10, (0, 220, 255, 200))):
            d.rectangle([W * m, H * m, W * (1 - m), H * (1 - m)], outline=col, width=2)
        d.rectangle([0, H * 0.82, W, H], outline=(255, 80, 80, 200), width=2)
        d.text((6, H * 0.82 + 4), "platform UI zone (bottom 18%, estimated)", fill=(255, 80, 80, 255), font=fonts(fs))
    if "caption" in overlays:
        for e in tl["tracks"]["C1"]:
            if e["start_f"] <= f < e["end_f"]:
                # estimated position matching the mpv burn-in parameters: sub-pos=90, margin 60 (1920 high)
                cy = H * 0.90 - 60 * H / 1920
                bw = min(W * 0.9, len(e["text"]) * 42 * H / 1920 * 1.05)
                d.rectangle([(W - bw) / 2, cy - 42 * H / 1920 * 1.3, (W + bw) / 2, cy], outline=(0, 255, 120, 255),
                            width=2)
                d.text(((W - bw) / 2, cy - 42 * H / 1920 * 1.3 - fs - 4), f"{e['id']} (estimated position)", fill=(0, 255, 120, 255),
                       font=fonts(fs))
    if "bbox" in overlays:
        for i in idx:
            s = c["plan"]["shots"][i]
            if s.get("kind") == "web":
                continue
            cam = c["cams"][s["id"]]
            r = cam.at(f - c["starts"][i])
            for m in s["camera"]:
                if m["op"] != "zoom_to":
                    continue
                b = m["bbox"]
                px = (b[0] * cam.sw, b[1] * cam.sh, b[2] * cam.sw, b[3] * cam.sh)
                sx, sy = W / (r[2] - r[0]), H / (r[3] - r[1])
                box = [(px[0] - r[0]) * sx, (px[1] - r[1]) * sy, (px[2] - r[0]) * sx, (px[3] - r[1]) * sy]
                d.rectangle(box, outline=(255, 0, 200, 255), width=3)
                d.text((box[0] + 4, box[1] + 4), f"{s['id']}:{m.get('observation_id') or 'bbox'}",
                       fill=(255, 0, 200, 255), font=fonts(fs))
    d.rectangle([0, 0, W, fs * len(header) * 1.35 + 10], fill=(0, 0, 0, 170))
    for k, ln in enumerate(header):
        d.text((8, 5 + k * fs * 1.35), ln, fill=(255, 255, 255, 255), font=fonts(fs))
    return a


def scrub(c, tl, picks, out_dir, render_video=None, overlays=(), fonts=None):
    """picks: [(requested_t seconds, frame number, reason)] gives clean frames + annotated frames + index.json."""
    fonts = fonts or Fonts()
    os.makedirs(out_dir, exist_ok=True)
    fps = tl["fps"]
    pf = PlanFrames(c)
    start = _start_time(render_video) if render_video else None
    items = []
    for k, (t, f, why) in enumerate(picks):
        idx = [i for i, st in enumerate(c["starts"]) if st <= f < st + c["frames"][i]]
        if render_video:
            img, actual, pts = grab_frame(render_video, f, fps, start)
            src = f"render:{os.path.basename(render_video)}"
        else:
            img, src, idx = pf.frame(f, fonts)
            actual, pts = f, None
        act = TL.active_at(tl, f)
        base = f"{k:03d}_f{f:06d}"
        clean = os.path.join(out_dir, base + "_clean.png")
        annot = os.path.join(out_dir, base + "_annot.png")
        img.save(clean)
        header = [f"t={f / fps:.3f}s  f={f}" + (f"  (actual f={actual})" if actual != f else "") + f"  {why}",
                  "V1 " + ",".join(act["V1"]) + ("  TR " + ",".join(act["TR"]) if act["TR"] else "")
                  + "  C1 " + (",".join(act["C1"]) or "-") + "  A1 " + (",".join(act["A1"]) or "-"),
                  f"source={src}"]
        _overlay(img, c, tl, f, idx, overlays, fonts, header).save(annot)
        items.append({"requested_t": t, "frame": f, "frame_t": round(f / fps, 4), "actual_frame": actual,
                      "actual_pts": pts, "reason": why, "active": act, "source": src,
                      "clean": clean, "annotated": annot})
    idx_path = os.path.join(out_dir, "index.json")
    res = {"fps": fps, "revision": tl.get("revision"), "overlays": list(overlays), **fonts.info(), "frames": items}
    with open(idx_path, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=1)
    mism = [x for x in items if x["actual_frame"] != x["frame"]]
    return {"index": idx_path, "count": len(items), "frame_mismatch": mism, "source": items[0]["source"] if items else None}


def picks_for(c, tl, at=None, rng=None, samples=None, cuts=False):
    fps = tl["fps"]
    total = c["visual_f"]
    out = []
    if at:
        for t in at:
            f = int(math.floor(t * fps + 1e-9))
            if not 0 <= f < total:
                raise ReelError("ARG_INVALID", f"--at {t}s is outside [0, {total / fps:.3f})")
            out.append((t, f, "at"))
    if rng:
        fa, fb = TL.parse_range(rng, fps, total)
        fb = min(fb, total)
        n = samples or 6
        for k in range(n):
            f = fa + (k * (fb - 1 - fa) // (n - 1) if n > 1 else 0)
            out.append((round(f / fps, 4), f, f"range {k + 1}/{n}"))
    if cuts:
        for i in range(1, len(c["frames"])):
            cf = c["starts"][i]
            sid, prev = c["plan"]["shots"][i]["id"], c["plan"]["shots"][i - 1]["id"]
            t = c["trans"][i - 1]
            for f, why in ((cf - 1, f"cut {prev}|{sid} before"), (cf, f"cut {prev}|{sid} at"),
                           (cf + 1, f"cut {prev}|{sid} after")):
                out.append((round(f / fps, 4), f, why))
            if t:
                out.append((round((cf + t // 2) / fps, 4), cf + t // 2, f"transition {prev}>{sid} mid"))
                out.append((round((cf + t) / fps, 4), cf + t, f"transition {prev}>{sid} end"))
    if not out:
        raise ReelError("ARG_INVALID", "one of --at / --range / --cuts is required")
    return out
