"""Web shots: text positions are read straight from the DOM (no OCR); both the camera and the highlight are drawn frame by frame by the browser.

- Locating: find a Range in the DOM by the source text; getClientRects gives the exact box of every character (document CSS pixels).
- Camera: a CSS transform (translate+scale, fractional pixels) on <html>; the browser re-rasterises at the current scale so pushed-in text stays sharp;
  screenshots are taken at 2x the output resolution and scaled down (supersampling) to suppress flicker from glyphs snapping to the pixel grid.
- Highlight: absolutely positioned highlight bars placed per line, with width following continuous progress to fractional pixels (not jumping character by character);
  the progress curve comes from per-word voice timing, made monotonic, low-passed and slightly advanced, so the sweep speed is continuous.
- Deterministic rendering: set the state frame by frame, then screenshot; nothing depends on live page animation, and the page's own animations/transitions are all disabled.
"""
import json, math, os, subprocess
from .errors import ReelError

SUPERSAMPLE = 2
BASE_CSS = """
*, *::before, *::after { animation: none !important; transition: none !important; caret-color: transparent !important; }
html { overflow: hidden !important; transform-origin: 0 0 !important; scroll-behavior: auto !important; }
::-webkit-scrollbar { display: none !important; }
#__reel_hl { position: absolute; left: 0; top: 0; width: 0; height: 0; pointer-events: none; z-index: 2147483646; }
#__reel_hl div { position: absolute; border-radius: 3px; mix-blend-mode: multiply; }
#__reel_dim { position: absolute; pointer-events: none; z-index: 2147483645; border-radius: 6px;
              box-shadow: 0 0 0 200000px rgba(0,0,0,0.55); opacity: 0; }
"""

# Runs in the page: find a Range by the source text and return the box of every character (document coordinates). Matching is done after whitespace normalisation and also works across nodes.
FIND_JS = r"""
([target, occurrence]) => {
  const norm = s => s.replace(/\s+/g, '');
  const want = norm(target);
  if (!want) return {error: 'EMPTY'};
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(n) {
      const p = n.parentElement; if (!p) return NodeFilter.FILTER_REJECT;
      const tag = p.tagName; if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT') return NodeFilter.FILTER_REJECT;
      const cs = getComputedStyle(p); if (cs.display === 'none' || cs.visibility === 'hidden') return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    }});
  const map = []; let flat = '';
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    const t = n.nodeValue;
    for (let i = 0; i < t.length; i++) { if (!/\s/.test(t[i])) { flat += t[i]; map.push([n, i]); } }
  }
  let idx = -1, from = 0;
  for (let k = 0; k < occurrence; k++) { idx = flat.indexOf(want, from); if (idx < 0) break; from = idx + 1; }
  if (idx < 0) return {error: 'NOT_FOUND', count: flat.split(want).length - 1};
  const sx = window.scrollX, sy = window.scrollY;
  const chars = [];
  for (let j = idx; j < idx + want.length; j++) {
    const [node, off] = map[j]; const r = document.createRange();
    r.setStart(node, off); r.setEnd(node, off + 1);
    const rs = r.getClientRects(); const b = rs.length ? rs[0] : r.getBoundingClientRect();
    chars.push([b.left + sx, b.top + sy, b.right + sx, b.bottom + sy]);
  }
  return {chars, text: want};
}
"""

SET_FRAME_JS = r"""
([cam, hls, dim]) => {
  const el = document.documentElement;
  el.style.transform = `translate(${-cam[0] * cam[2]}px, ${-cam[1] * cam[2]}px) scale(${cam[2]})`;
  let box = document.getElementById('__reel_hl');
  if (!box) { box = document.createElement('div'); box.id = '__reel_hl'; el.appendChild(box); }
  box.innerHTML = '';
  for (const [x, y, w, h, color] of hls) {
    if (w <= 0) continue;
    const d = document.createElement('div');
    d.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px;background:${color}`;
    box.appendChild(d);
  }
  let dm = document.getElementById('__reel_dim');
  if (!dm) { dm = document.createElement('div'); dm.id = '__reel_dim'; el.appendChild(dm); }
  if (dim) { const [x, y, w, h, a] = dim; dm.style.cssText = `left:${x}px;top:${y}px;width:${w}px;height:${h}px;opacity:${a}`; }
  else dm.style.opacity = 0;
  return document.body.getBoundingClientRect().top;   // force synchronous layout
}
"""


def _pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ReelError("BACKEND_UNAVAILABLE", "web shots need Python playwright (pip install playwright && playwright install chromium)")
    return sync_playwright


class Page:
    """Open the page and prepare it for rendering. layout_w: page layout width (CSS px); the viewport height follows the output aspect ratio."""

    def __init__(self, url, out_w, out_h, layout_w, css="", wait_ms=800):
        self.out_w, self.out_h, self.layout_w = out_w, out_h, layout_w
        self.layout_h = round(layout_w * out_h / out_w)
        self.dpr = out_w / layout_w * SUPERSAMPLE
        self._ctx = _pw()().start()
        self.browser = self._ctx.chromium.launch()
        self.page = self.browser.new_page(viewport={"width": layout_w, "height": self.layout_h},
                                          device_scale_factor=self.dpr)
        target = url if "://" in url else "file://" + os.path.abspath(url)
        try:
            self.page.goto(target, wait_until="networkidle", timeout=60000)
        except Exception as e:  # noqa: BLE001
            self.close()
            raise ReelError("WEB_LOAD_FAILED", f"cannot open the page: {e}", url=url)
        self.page.add_style_tag(content=BASE_CSS + (css or ""))
        self.page.evaluate("document.fonts ? document.fonts.ready : null")
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.wait_for_timeout(wait_ms)
        self.doc_w, self.doc_h = self.page.evaluate(
            "[Math.max(document.documentElement.scrollWidth, innerWidth), document.documentElement.scrollHeight]")

    def find(self, text, occurrence=1):
        r = self.page.evaluate(FIND_JS, [text, occurrence])
        if r.get("error") == "NOT_FOUND":
            raise ReelError("TEXT_NOT_FOUND", f"this text was not found on the page (whitespace ignored): {text[:40]}", occurrences=r.get("count"))
        if r.get("error"):
            raise ReelError("TEXT_NOT_FOUND", f"empty text: {text!r}")
        return r["chars"], r["text"]

    def frame(self, cam_rect, hls, dim):
        """cam_rect: framing window in document coordinates (x0,y0,x1,y1). Returns PNG bytes (output resolution x SUPERSAMPLE)."""
        s = self.layout_w / (cam_rect[2] - cam_rect[0])
        self.page.evaluate(SET_FRAME_JS, [[cam_rect[0], cam_rect[1], s], hls, dim])
        return self.page.screenshot(type="png", animations="disabled", caret="hide")

    def close(self):
        try:
            self.browser.close()
        finally:
            self._ctx.stop()


# ---------------- text to lines to continuous highlight ----------------

def lines_of(chars):
    """Group the per-character boxes by line: [(line box x0,y0,x1,y1, list of character indices on that line)]."""
    lines = []
    for i, c in enumerate(chars):
        if lines and abs(((c[1] + c[3]) / 2) - ((lines[-1][0][1] + lines[-1][0][3]) / 2)) < (c[3] - c[1]) * 0.5 \
                and c[0] >= lines[-1][0][0] - 1:
            b = lines[-1][0]
            lines[-1] = ((min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3])), lines[-1][1] + [i])
        else:
            lines.append(((c[0], c[1], c[2], c[3]), [i]))
    return lines


def highlight_rects(chars, progress, color, pad=2.0):
    """progress is in [0, len(chars)] and may be fractional: character k half swept is k+0.5. Returns the highlight bars per line."""
    out = []
    for (x0, y0, x1, y1), idx in lines_of(chars):
        a, b = idx[0], idx[-1] + 1
        if progress <= a:
            break
        if progress >= b:
            right = chars[idx[-1]][2]
        else:
            k = int(math.floor(progress))
            frac = progress - k
            c = chars[k]
            right = c[0] + (c[2] - c[0]) * frac
        out.append([x0 - pad, y0 - pad * 0.5, max(0.0, right - x0 + pad), (y1 - y0) + pad, color])
    return out


def bbox_of(chars):
    return [min(c[0] for c in chars), min(c[1] for c in chars), max(c[2] for c in chars), max(c[3] for c in chars)]


# ---------------- voice to per-word timing to smooth progress curve ----------------

def _norm(s):
    import re
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE).lower()


def char_timeline(events):
    """[(start, end, text)] → [(char, t_start, t_end)]，within a sentence, time is shared evenly by character count (when the word timing comes from whisper it is already per character)."""
    out = []
    for a, b, t in events:
        cs = [c for c in _norm(t)]
        if not cs:
            continue
        step = (b - a) / len(cs)
        for i, c in enumerate(cs):
            out.append((c, a + i * step, a + (i + 1) * step))
    return out


def align(target, timeline, min_ratio=0.6):
    """Align the page text to the per-word voice timeline (tolerating the odd whisper error). Returns (t_start, t_end) for every target character."""
    import difflib
    tgt = _norm(target)
    spoken = "".join(c for c, _, _ in timeline)
    if not tgt:
        raise ReelError("TEXT_NOT_IN_VOICE", "target text is empty")
    sm = difflib.SequenceMatcher(None, spoken, tgt, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size]
    matched = sum(b.size for b in blocks)
    if matched / len(tgt) < min_ratio:
        raise ReelError("TEXT_NOT_IN_VOICE", f"this text was not found in the voice-over (matched {matched}/{len(tgt)} characters): {target[:30]}",
                        matched=matched, need=len(tgt))
    pos = {}
    for b in blocks:
        for k in range(b.size):
            pos[b.b + k] = b.a + k
    known = sorted(pos)
    times = []
    for j in range(len(tgt)):
        if j in pos:
            _, s, e = timeline[pos[j]]
        else:                                  # unmatched characters: linearly interpolate between the known characters before and after
            lo = max([k for k in known if k < j], default=None)
            hi = min([k for k in known if k > j], default=None)
            ts = timeline[pos[lo]][2] if lo is not None else timeline[pos[hi]][1]
            te = timeline[pos[hi]][1] if hi is not None else timeline[pos[lo]][2]
            n = (hi if hi is not None else len(tgt)) - (lo if lo is not None else -1)
            f = (j - (lo if lo is not None else -1)) / max(1, n)
            s = e = ts + (te - ts) * f
        times.append((s, e))
    return times


def spread_to_page(page_text, word_times):
    """word_times covers only text characters (after _norm); it is spread back over the page characters. Punctuation is None and the progress curve interpolates across it between the surrounding text."""
    out, k = [], 0
    for ch in page_text:
        if _norm(ch):
            out.append(word_times[k]); k += 1
        else:
            out.append(None)
    if k != len(word_times):
        raise ReelError("TEXT_MISMATCH", "page text and alignment result differ in character count", page_words=k, voice=len(word_times))
    return out


def progress_curve(times, fps, n_frames, t_offset, lead=0.08, smooth_s=0.10):
    """Per-frame progress (0..number of characters, fractional, continuous).
    Nodes are only the start times (t_i, i) of text characters, with the end of the last character mapping to full; punctuation has no node and is interpolated across naturally (no sudden jump).
    Then apply a Gaussian low-pass (sigma=smooth_s) and make it monotonic, advancing the whole curve by lead seconds."""
    import numpy as np
    knots_t, knots_p = [], []
    for i, tt in enumerate(times):
        if tt is not None:
            knots_t.append(tt[0]); knots_p.append(float(i))
    last = max(i for i, tt in enumerate(times) if tt is not None)
    knots_t.append(max(times[last][1], knots_t[-1] + 1e-3)); knots_p.append(float(len(times)))
    kt = np.maximum.accumulate(np.array(knots_t))
    kt = kt + np.arange(len(kt)) * 1e-6            # strictly increasing, as np.interp requires
    ts = np.array([t_offset + f / fps + lead for f in range(n_frames)])
    prog = np.interp(ts, kt, np.array(knots_p), left=0.0, right=float(len(times)))
    sig = smooth_s * fps
    if sig > 0.5:
        r = int(np.ceil(3 * sig))
        ker = np.exp(-0.5 * (np.arange(-r, r + 1) / sig) ** 2); ker /= ker.sum()
        prog = np.convolve(np.pad(prog, r, mode="edge"), ker, mode="valid")
    return np.maximum.accumulate(np.clip(prog, 0, len(times))).tolist()


# ---------------- camera follow: comfort zone + easing ----------------

def follow_camera(line_boxes_by_frame, column, start_rect, aspect, zoom_w, doc_w, doc_h, fps, comfort=0.28,
                  move_s=0.6, col_pad=14.0, max_speed=0.7, start="first_line"):
    """Reading-style follow: the framing width fits at least the whole text column (no line starts or ends cut off), is fixed horizontally on the column, and follows only the line being spoken vertically.
    An eased move starts only when the current line leaves the vertical comfort zone of the framing window (centre +-comfort); finally the speed kinks are smoothed offline."""
    import numpy as np
    n = len(line_boxes_by_frame)
    w = min(doc_w, max(zoom_w, column[2] - column[0] + 2 * col_pad))
    h = w / aspect
    tx = min(max((column[0] + column[2]) / 2, w / 2), doc_w - w / 2)
    cx, cy = (start_rect[0] + start_rect[2]) / 2, (start_rect[1] + start_rect[3]) / 2
    cw = start_rect[2] - start_rect[0]
    first = next((b for b in line_boxes_by_frame if b is not None), None)
    if start == "first_line" and first is not None:      # start directly at the first sentence's position instead of swinging down from the page top
        cx, cw = tx, w
        cy = min(max((first[1] + first[3]) / 2, h / 2), doc_h - h / 2)
    rects, move = [], None
    for f in range(n):
        if move and f >= move[1]:
            cx, cy, cw = move[3]; move = None
        if move is None:
            b = line_boxes_by_frame[f]
            if b is not None:
                by = (b[1] + b[3]) / 2
                off = abs(by - cy) / (cw / aspect)
                if abs(cw - w) > 1 or abs(cx - tx) > 1 or off > comfort:
                    ty = min(max(by, h / 2), doc_h - h / 2)
                    # duration grows with distance: the peak speed (about pi/2 times the average for cosine easing) does not exceed max_speed picture-heights per second
                    dist = max(abs(ty - cy) / h, abs(tx - cx) / w, abs(math.log(w / cw)))
                    dur = max(move_s, dist * math.pi / 2 / max_speed)
                    move = (f, f + max(2, int(round(dur * fps))), (cx, cy, cw), (tx, ty, w))
        if move:
            p = (f - move[0]) / (move[1] - move[0])
            e = (1 - math.cos(math.pi * min(1, max(0, p)))) / 2
            a, b2 = move[2], move[3]
            fx, fy = a[0] + (b2[0] - a[0]) * e, a[1] + (b2[1] - a[1]) * e
            fw = a[2] * (b2[2] / a[2]) ** e
        else:
            fx, fy, fw = cx, cy, cw
        fh = fw / aspect
        rects.append((fx - fw / 2, fy - fh / 2, fx + fw / 2, fy + fh / 2))
    arr = np.array(rects)
    k = max(1, int(round(0.12 * fps)))
    if k > 1 and n > 2 * k:
        c = (arr[:, :2] + arr[:, 2:]) / 2
        pad = np.pad(c, ((k, k), (0, 0)), mode="edge")
        ker = np.ones(k) / k
        cs = np.stack([np.convolve(pad[:, j], ker, mode="same")[k:-k] for j in range(2)], 1)
        half = (arr[:, 2:] - arr[:, :2]) / 2
        arr = np.concatenate([cs - half, cs + half], 1)
    return [tuple(r) for r in arr.tolist()]


# ---------------- render one web shot ----------------

def render_shot(shot, n, fps, out_w, out_h, t_offset, voice_timeline, seg_enc, out_path, log=print):
    """shot["web"]: {url, layout_width, css, occurrence}; shot["camera"]/["highlight"] hold the source text."""
    web = shot["web"]
    aspect = out_w / out_h
    pg = Page(web["url"], out_w, out_h, web.get("layout_width") or (390 if out_w < out_h else 1280), web.get("css", ""))
    try:
        top = (0.0, 0.0, float(pg.layout_w), pg.layout_w / aspect)
        items = []
        for h in shot.get("highlight", []):
            chars, page_text = pg.find(h["text"], h.get("occurrence", 1))
            if h.get("sync", "voice") == "voice":
                if not voice_timeline:
                    raise ReelError("TEXT_NOT_IN_VOICE", "highlight must follow the voice-over, but there are no subtitles / per-word timing")
                times = spread_to_page(page_text, align(page_text, voice_timeline))
            else:
                t0, t1 = h["t0"] + t_offset, h["t1"] + t_offset
                step = (t1 - t0) / len(chars)
                times = [(t0 + i * step, t0 + (i + 1) * step) for i in range(len(chars))]
            prog = progress_curve(times, fps, n, t_offset, lead=h.get("lead", 0.08))
            items.append({"chars": chars, "prog": prog, "color": h.get("color", "rgba(255,221,0,0.65)"),
                          "style": h.get("style", "marker"), "lines": lines_of(chars)})
        # camera: follow = follow the line being spoken; zoom_to source text (push to a passage once) is also supported
        cam = shot.get("camera", [])
        zoom_w = pg.layout_w / (cam[0].get("zoom", 1.8) if cam else 1.0)
        rects = None
        if cam and cam[0]["op"] == "follow":
            cur = []
            for f in range(n):
                box = None
                for it in items:
                    p = it["prog"][f]
                    if 0 < p < len(it["chars"]) + 0.5:
                        for lb, idx in it["lines"]:
                            if idx[0] <= min(p, len(it["chars"]) - 1e-6) < idx[-1] + 1:
                                box = lb; break
                    if box:
                        break
                cur.append(box if box else (cur[-1] if cur else None))
            column = bbox_of([c for it in items for c in it["chars"]])
            rects = follow_camera(cur, column, top, aspect, zoom_w, pg.doc_w, pg.doc_h, fps,
                                  comfort=cam[0].get("comfort", 0.28), move_s=cam[0].get("move_s", 0.6),
                                  max_speed=cam[0].get("max_speed", 0.7), start=cam[0].get("start", "first_line"))
        elif cam and cam[0]["op"] == "zoom_to":
            from . import camera as C
            m = dict(cam[0])
            b = bbox_of(pg.find(m["text"], m.get("occurrence", 1))[0])
            m["bbox"] = [b[0] / pg.doc_w, b[1] / pg.doc_h, b[2] / pg.doc_w, b[3] / pg.doc_h]
            m.setdefault("padding", 0.25); m.setdefault("max_zoom", 3.0); m.setdefault("easing", "ease_in_out")
            m.setdefault("t0", 0); m.setdefault("t1", n / fps)
            fake = {"id": shot["id"], "duration": n / fps, "space": "image",
                    "viewport": [top[0] / pg.doc_w, top[1] / pg.doc_h, top[2] / pg.doc_w, top[3] / pg.doc_h],
                    "camera": [{k: m[k] for k in ("op", "bbox", "t0", "t1", "easing", "padding", "max_zoom")}]}
            sc = C.ShotCamera(fake, {"width": pg.doc_w, "height": pg.doc_h}, out_w, out_h, fps)
            rects = [sc.at(f) for f in range(n)]
        else:
            rects = [top] * n
        # per frame: set camera and highlight, screenshot, pipe to ffmpeg (supersampled, scaled back to the output size)
        ff = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "image2pipe", "-framerate", str(fps), "-i", "-",
                               "-vf", f"scale={out_w}:{out_h}:flags=lanczos,format=yuv420p,setsar=1",
                               "-frames:v", str(n)] + seg_enc + ["-r", str(fps), out_path],
                              stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        trace = []
        for f in range(n):
            hls = []
            dim = None
            for it in items:
                p = it["prog"][f]
                if it["style"] in ("marker", "both"):
                    hls += highlight_rects(it["chars"], p, it["color"])
                if it["style"] in ("spotlight", "both") and p > 0:
                    lb = bbox_of(it["chars"])
                    a = min(1.0, p / max(1.0, len(it["chars"]) * 0.15))
                    if p >= len(it["chars"]):
                        a = 1.0
                    dim = [lb[0] - 8, lb[1] - 6, lb[2] - lb[0] + 16, lb[3] - lb[1] + 12, a]
            ff.stdin.write(pg.frame(rects[f], hls, dim))
            trace.append({"rect": rects[f], "prog": [it["prog"][f] for it in items]})
        ff.stdin.close()
        err = ff.stderr.read().decode()
        if ff.wait() != 0:
            raise ReelError("FFMPEG_FAILED", "web shot encoding failed", stderr=err[-800:])
        return {"doc": [pg.doc_w, pg.doc_h], "layout": [pg.layout_w, pg.layout_h],
                "items": [{"chars": it["chars"], "lines": [lb for lb, _ in it["lines"]]} for it in items],
                "trace": trace}
    finally:
        pg.close()
