"""Framing geometry: turns the sparse camera decisions in a plan into per-frame framing windows and compiles them into perspective corner expressions.

Coordinate convention: internally always source-image pixels (rotation applied), rect = (x0, y0, x1, y1).
Rendering does no up-front centre crop: it first crops "the region the whole shot will use" and then scales it up to the canvas, so a bbox near the picture edge can still be pushed in on.
perspective samples at 1/256-pixel sub-pixel precision, avoiding the integer-rounding jitter of zoompan/crop.
"""
import math
from .errors import ReelError

MAX_CANVAS = 4320        # upper limit of the canvas long side (bounds memory and speed)
EASE = {
    "linear": "{p}",
    "ease_in": "(1-cos({p}*PI/2))",
    "ease_out": "sin({p}*PI/2)",
    "ease_in_out": "((1-cos(PI*{p}))/2)",
}


def ease_py(name, p):
    p = min(1.0, max(0.0, p))
    return {"linear": p, "ease_in": 1 - math.cos(p * math.pi / 2), "ease_out": math.sin(p * math.pi / 2),
            "ease_in_out": (1 - math.cos(math.pi * p)) / 2}[name]


def cover_rect(sw, sh, aspect):
    """The largest centred window inside the source with the output aspect ratio (the range seen when covering the frame)."""
    if sw / sh > aspect:
        w, h = sh * aspect, sh
    else:
        w, h = sw, sw / aspect
    return ((sw - w) / 2, (sh - h) / 2, (sw + w) / 2, (sh + h) / 2)


def norm_to_px(b, space, sw, sh, aspect):
    if space == "cover":
        c = cover_rect(sw, sh, aspect)
        cw, ch = c[2] - c[0], c[3] - c[1]
        return (c[0] + b[0] * cw, c[1] + b[1] * ch, c[0] + b[2] * cw, c[1] + b[3] * ch)
    return (b[0] * sw, b[1] * sh, b[2] * sw, b[3] * sh)


def _shift_inside(cx, cy, w, h, sw, sh):
    cx = min(max(cx, w / 2), sw - w / 2)
    cy = min(max(cy, h / 2), sh - h / 2)
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def fit_viewport(rect, sw, sh, aspect, what):
    """Expand any rectangle to the output aspect ratio (grow only, never shrink) and move it into the picture; if it does not fit, raise an error instead of cropping silently."""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if w / h < aspect:
        w = h * aspect
    else:
        h = w / aspect
    cw = cover_rect(sw, sh, aspect)
    maxw = cw[2] - cw[0]
    if w > maxw * (1 + 1e-9):          # tolerates only floating-point error; even 1 px too much is not cropped silently
        raise ReelError("SUBJECT_TOO_LARGE", f"{what} is larger than the source after expanding to the output aspect ratio; it cannot be framed completely",
                        need_width_px=round(w, 2), max_width_px=round(maxw, 2))
    w = min(w, maxw)
    h = w / aspect
    return _shift_inside(cx, cy, w, h, sw, sh)


def zoom_target(bbox_px, padding, max_zoom, sw, sh, aspect):
    x0, y0, x1, y1 = bbox_px
    fit_viewport(bbox_px, sw, sh, aspect, "subject itself")        # only an error if the subject itself does not fit
    lo, hi = 0.0, padding                                    # padding is only margin: when it does not fit it shrinks automatically to the largest feasible value
    try:
        fit_viewport((x0 - (x1 - x0) * hi, y0 - (y1 - y0) * hi, x1 + (x1 - x0) * hi, y1 + (y1 - y0) * hi),
                     sw, sh, aspect, "subject (with padding)")
        lo = hi
    except ReelError:
        for _ in range(30):
            mid = (lo + hi) / 2
            try:
                fit_viewport((x0 - (x1 - x0) * mid, y0 - (y1 - y0) * mid, x1 + (x1 - x0) * mid, y1 + (y1 - y0) * mid),
                             sw, sh, aspect, "subject (with padding)")
                lo = mid
            except ReelError:
                hi = mid
    pw, ph = (x1 - x0) * lo, (y1 - y0) * lo
    vp = fit_viewport((x0 - pw, y0 - ph, x1 + pw, y1 + ph), sw, sh, aspect, "subject (with padding)")
    cw = cover_rect(sw, sh, aspect)
    minw = (cw[2] - cw[0]) / max_zoom
    w = vp[2] - vp[0]
    if w < minw:     # pushed in too close: enlarge the framing window to the smallest size max_zoom allows; the subject stays inside
        cx, cy = (vp[0] + vp[2]) / 2, (vp[1] + vp[3]) / 2
        vp = _shift_inside(cx, cy, minw, minw / aspect, sw, sh)
    return vp


class Target:
    """Camera target: a fixed framing window, or a tracking window that moves over time (fixed size, centre linearly interpolated between keyframes, held outside the ends)."""

    def __init__(self, keys, w):
        self.keys, self.w = keys, w          # keys: [(frame, cx, cy)] sorted by frame

    @classmethod
    def fixed(cls, r):
        return cls([(0, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2)], r[2] - r[0])

    def at(self, f):
        k = self.keys
        if f <= k[0][0] or len(k) == 1:
            return k[0][1], k[0][2], self.w
        for (fa, xa, ya), (fb, xb, yb) in zip(k, k[1:]):
            if f < fb:
                u = (f - fa) / (fb - fa)
                return xa + (xb - xa) * u, ya + (yb - ya) * u, self.w
        return k[-1][1], k[-1][2], self.w

    def expr(self, fv):
        k = self.keys
        cx, cy = repr(k[-1][1]), repr(k[-1][2])
        for (fa, xa, ya), (fb, xb, yb) in reversed(list(zip(k, k[1:]))):
            u = f"(({fv}-{fa})/{fb - fa})"
            cx = f"if(lt({fv},{fb}),({xa!r}+({xb - xa!r})*{u}),{cx})"
            cy = f"if(lt({fv},{fb}),({ya!r}+({yb - ya!r})*{u}),{cy})"
        if len(k) > 1:
            cx = f"if(lt({fv},{k[0][0]}),{k[0][1]!r},{cx})"
            cy = f"if(lt({fv},{k[0][0]}),{k[0][2]!r},{cy})"
        return cx, cy, repr(self.w)


class ShotCamera:
    """Per-frame framing window of one shot. segments: [(f0, f1, rectA, Target, easing)], sorted by time and joined end to end."""

    def __init__(self, shot, meta, out_w, out_h, fps):
        self.sw, self.sh = meta["width"], meta["height"]
        self.aspect = out_w / out_h
        self.n = int(round(shot["duration"] * fps))
        space = shot.get("space", "image")
        if shot.get("viewport"):
            start = fit_viewport(norm_to_px(shot["viewport"], space, self.sw, self.sh, self.aspect),
                                 self.sw, self.sh, self.aspect, f"{shot['id']}.viewport")
        else:
            start = cover_rect(self.sw, self.sh, self.aspect)
        self.start = start
        self.segments = []
        cur = start
        px = lambda b: (b[0] * self.sw, b[1] * self.sh, b[2] * self.sw, b[3] * self.sh)
        for m in shot.get("camera", []):
            if m["op"] == "zoom_to" and m.get("track_boxes"):
                # tracking: compute a framing window per keyframe, use the largest size for all (the subject stays whole and the picture does not jump in size), and follow the centre
                vps = [(t, zoom_target(px(b), m["padding"], m["max_zoom"], self.sw, self.sh, self.aspect))
                       for t, b in m["track_boxes"]]
                w = max(v[2] - v[0] for _, v in vps)
                keys = []
                for t, v in vps:
                    r = _shift_inside((v[0] + v[2]) / 2, (v[1] + v[3]) / 2, w, w / self.aspect, self.sw, self.sh)
                    keys.append((t * fps, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2))
                tgt = Target(keys, w)
            elif m["op"] == "zoom_to":
                tgt = Target.fixed(zoom_target(px(m["bbox"]), m["padding"], m["max_zoom"], self.sw, self.sh,
                                               self.aspect))
            else:
                tgt = Target.fixed(fit_viewport(norm_to_px(m["viewport"], space, self.sw, self.sh, self.aspect),
                                                self.sw, self.sh, self.aspect, f"{shot['id']}.move_to"))
            f0 = int(round(m["t0"] * fps))
            f1 = min(int(round(m["t1"] * fps)), self.n - 1)
            f1 = max(f1, f0 + 1)
            self.segments.append((f0, f1, cur, tgt, m["easing"]))
            cx, cy, w = tgt.at(f1)
            cur = (cx - w / 2, cy - w / self.aspect / 2, cx + w / 2, cy + w / self.aspect / 2)
        self.end = self.at(self.n - 1)

    @property
    def static(self):
        return not self.segments

    def _rect(self, cx, cy, w):
        h = w / self.aspect
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def at(self, f):
        """Framing window of frame f (0-based), Python version, same formula as the expression version: linear centre, logarithmically interpolated size (visually constant-speed push-in)."""
        for f0, f1, a, tgt, ez in self.segments:
            if f < f1:
                e = ease_py(ez, (f - f0) / (f1 - f0))
                acx, acy, aw = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2, a[2] - a[0]
                tcx, tcy, tw = tgt.at(f)
                return self._rect(acx + (tcx - acx) * e, acy + (tcy - acy) * e, aw * (tw / aw) ** e)
        if self.segments:
            return self._rect(*self.segments[-1][3].at(f))
        return self.start

    def region(self):
        """The source region the whole shot will use (integer pixels), expanded to the output aspect ratio and clamped inside the picture."""
        rs = [self.at(f) for f in range(self.n)] + [self.start]
        x0, y0 = min(r[0] for r in rs), min(r[1] for r in rs)
        x1, y1 = max(r[2] for r in rs), max(r[3] for r in rs)
        w, h = x1 - x0, y1 - y0
        if w / h < self.aspect:
            w = min(h * self.aspect, self.sw)
        else:
            h = min(w / self.aspect, self.sh)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        r = _shift_inside(cx, cy, w, h, self.sw, self.sh)
        ix0, iy0 = max(0, math.floor(r[0])), max(0, math.floor(r[1]))
        ix1, iy1 = min(self.sw, math.ceil(r[2])), min(self.sh, math.ceil(r[3]))
        return ix0, iy0, ix1, iy1

    def filter(self, out_w, out_h):
        """Returns (first part, second part) filters: the first crops the region and scales it to the canvas (for images a loop is inserted between the two), the second does the camera move and outputs."""
        if self.static:
            x0, y0, x1, y1 = self.start
            pre = (f"format=yuv444p,crop=w={x1 - x0:.4f}:h={y1 - y0:.4f}:x={x0:.4f}:y={y0:.4f}:exact=1,"
                   f"scale={out_w}:{out_h}:flags=lanczos")
            return pre, "format=yuv420p,setsar=1"
        rx0, ry0, rx1, ry1 = self.region()
        rw, rh = rx1 - rx0, ry1 - ry0
        # width is monotonic within a segment, so the smallest framing window is at a segment endpoint
        minw = min([self.start[2] - self.start[0]] + [t.w for _, _, _, t, _ in self.segments])
        k = min(2 * out_w / minw, MAX_CANVAS / max(rw, rh))   # smallest framing window is about 2x output width, bounded by the canvas limit
        cw, ch = max(2, int(round(rw * k / 2)) * 2), max(2, int(round(rh * k / 2)) * 2)
        kx, ky = cw / rw, ch / rh
        pre = (f"format=yuv444p,crop=w={rw}:h={rh}:x={rx0}:y={ry0}:exact=1,"
               f"scale={cw}:{ch}:flags=lanczos")
        cx_e, cy_e, w_e = self._expr()
        # canvas coordinates: X = (x - rx0) * kx
        hw = f"(({w_e})/2)"
        hh = f"(({w_e})/{self.aspect!r}/2)"
        X = lambda sgn: f"(({cx_e}){sgn}{hw}-{rx0})*{kx!r}"
        Y = lambda sgn: f"(({cy_e}){sgn}{hh}-{ry0})*{ky!r}"
        corners = (f"x0='{X('-')}':y0='{Y('-')}':x1='{X('+')}':y1='{Y('-')}':"
                   f"x2='{X('-')}':y2='{Y('+')}':x3='{X('+')}':y3='{Y('+')}'")
        post = (f"perspective={corners}:sense=source:eval=frame:interpolation=cubic,"
                f"scale={out_w}:{out_h}:flags=lanczos,format=yuv420p,setsar=1")
        return pre, post

    def _expr(self):
        """Three piecewise expressions for cx, cy, w; f = on-1 (perspective's on starts at 1)."""
        f = "(on-1)"
        ex = dict(zip(("cx", "cy", "w"), self.segments[-1][3].expr(f)))
        for f0, f1, a, tgt, ez in reversed(self.segments):
            p = f"clip(({f}-{f0})/{f1 - f0},0,1)"
            e = EASE[ez].format(p=p)
            acx, acy, aw = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2, a[2] - a[0]
            tcx, tcy, tw = tgt.expr(f)
            seg = {"cx": f"({acx!r}+(({tcx})-{acx!r})*{e})", "cy": f"({acy!r}+(({tcy})-{acy!r})*{e})",
                   "w": f"({aw!r}*pow(({tw})/{aw!r},{e}))"}
            ex = {k: f"if(lt({f},{f1}),{seg[k]},{ex[k]})" for k in ex}
        return ex["cx"], ex["cy"], ex["w"]
