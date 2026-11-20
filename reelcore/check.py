"""Final-video self-check: four states pass / fail / needs_review / not_checked, each item carrying measurements, the rule and evidence time points.

A check that is not implemented is always not_checked, never shown as pass. Thresholds come from the profile (profiles/*.json) and every item states its rule.
When a detection itself fails (missing dependency, non-zero ffmpeg exit, unparseable output, did not reach the end of the video), that item is not_checked and carries an error,
which gate.evaluate counts as a blocker; this module only produces the report and the gate decides "can it be delivered".
"""
import hashlib, math, os, re, shutil, subprocess, json
from . import subtitles, verify, plan as planmod, textscan
from .errors import ReelError

BLACK_PIX_TH = 0.10          # blackdetect pixel threshold
BLACK_PIC_TH = 0.98          # proportion of black pixels in the picture
FREEZE_NOISE = "-60dB"
FREEZE_MIN_S = 0.5
CLIP_LEVEL = 0.99            # |sample| >= this counts as hitting the ceiling (a clipped flat top has ringing after AAC decoding, so 0.999 is too strict)
CLIP_MIN_SAMPLES = 10        # clipped samples >= this is a fail (about 0.2ms@48k; a single peak touching the ceiling does not count)

NOT_IMPLEMENTED = [
    ("caption_legibility", "actual subtitle layout bbox / safe zone / legibility: needs a layout-engine sidecar; only timing is checked at present"),
    ("pin_occlusion", "pin / photo-card occlusion: the v1 plan has no layer data"),
    ("voice_visual_alignment", "alignment of voice anchors with picture appearance times: the v1 plan has no word anchors"),
    ("content_correspondence", "correspondence of places / media content with the voice-over: needs media binding data and keyframe review"),
    ("ocr_visible_text", "text in the picture (PNG cards, screen recordings, the final video) is not OCR-ed: the text scan only covers input text and cannot claim that the video has no placeholders"),
]


def _item(name, status, rule, measured=None, evidence=None, note=None, **extra):
    return {"check": name, "status": status, "rule": rule, "measured": measured or {}, "evidence": evidence or [],
            **({"note": note} if note else {}), **extra}


def need_tools():
    miss = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if miss:
        raise ReelError("DEPENDENCY_MISSING", "missing dependency, cannot check", missing=miss)


def _ff(args):
    """One ffmpeg analysis: a non-zero exit gives FFMPEG_FAILED; not reporting that it reached the end gives OUTPUT_UNPARSABLE. Returns stderr."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:1"] + args + ["-f", "null", os.devnull],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "detection process failed", cmd=" ".join(args[:8]), rc=p.returncode,
                        stderr=p.stderr[-600:])
    if "progress=end" not in p.stdout:
        raise ReelError("OUTPUT_UNPARSABLE", "ffmpeg did not report reaching the end, so it cannot be confirmed that the whole video was analysed",
                        cmd=" ".join(args[:8]), stdout=p.stdout[-300:])
    return p.stderr


def _events(err, token, pattern):
    """Event lines are parsed one by one; if the number of lines containing the token differs from the number parsed, the output format changed and it must not be treated as "no events"."""
    found = re.findall(pattern, err)
    n = err.count(token)
    if n != len(found):
        i = err.find(token)
        raise ReelError("OUTPUT_UNPARSABLE", f"{token} events cannot be parsed", lines=n, parsed=len(found),
                        sample=err[max(0, i - 40): i + 80] if i >= 0 else "")
    return found


def _probe(video):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", video],
                       capture_output=True, text=True)
    try:
        info = json.loads(p.stdout) if p.returncode == 0 else None
        dur = float(info["format"]["duration"])
    except (ValueError, TypeError, KeyError):
        raise ReelError("ASSET_UNREADABLE", "cannot read the video", path=video, stderr=p.stderr[-300:])
    return info, dur


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _fps(v):
    n, _, d = (v.get("r_frame_rate") or "0/1").partition("/")
    try:
        return float(n) / float(d or 1) if float(d or 1) else 0.0
    except ValueError:
        return 0.0


# ---------------- individual checks ----------------

def decode_clean(video):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-f", "null", os.devnull], capture_output=True, text=True)
    ok = p.returncode == 0 and not p.stderr.strip()
    return _item("decode_clean", "pass" if ok else "fail", "whole video decodes without errors (ffmpeg -v error prints nothing and exits 0)",
                 {"rc": p.returncode}, [] if ok else [{"stderr": p.stderr.strip()[:300], "offending": True}])


def fmt(info, prof):
    f = prof["format"]
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    bad, m = [], {}
    if v is None:
        bad.append("no video stream")
    else:
        w, h = v.get("width"), v.get("height")
        rot = next((int(sd.get("rotation", 0)) for sd in v.get("side_data_list", []) if "rotation" in sd), 0)
        if abs(rot) == 90:
            w, h = h, w
        m.update(video_codec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"), width=w, height=h, fps=round(_fps(v), 3))
        if f.get("video_codec") and v.get("codec_name") not in f["video_codec"]:
            bad.append(f"video codec {v.get('codec_name')}, required {f['video_codec']}")
        if f.get("pix_fmt") and v.get("pix_fmt") not in f["pix_fmt"]:
            bad.append(f"pixel format {v.get('pix_fmt')}, required {f['pix_fmt']}")
        if f.get("width") and (w, h) != (f["width"], f["height"]):
            bad.append(f"frame size {w}x{h}, required {f['width']}x{f['height']}")
        if f.get("fps") and abs(_fps(v) - f["fps"]) >= 0.05:
            bad.append(f"frame rate {_fps(v):.3f}, required {f['fps']}")
    if a is None:
        if prof["audio"] == "required":
            bad.append("no audio track")
    else:
        m.update(audio_codec=a.get("codec_name"), sample_rate=int(a.get("sample_rate") or 0), channels=a.get("channels"))
        if f.get("audio_codec") and a.get("codec_name") not in f["audio_codec"]:
            bad.append(f"audio codec {a.get('codec_name')}, required {f['audio_codec']}")
        if f.get("sample_rate") and int(a.get("sample_rate") or 0) not in f["sample_rate"]:
            bad.append(f"sample rate {a.get('sample_rate')}, required {f['sample_rate']}")
    rule = f"profile.format：{ {k: x for k, x in f.items() if x} }；audio={prof['audio']}"
    return _item("format", "fail" if bad else "pass", rule, m, [{"why": b, "offending": True} for b in bad])


def first_frame(video, fps, prof):
    """Decode display frame 0 directly instead of relying on blackdetect's minimum duration (a single black frame cannot be caught with d=0.1)."""
    c = prof["first_frame"]
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-frames:v", "1", "-vf", "scale=96:-2,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "frame 0 failed to decode", stderr=p.stderr.decode(errors="replace")[-300:])
    b = p.stdout
    if not b:
        raise ReelError("OUTPUT_UNPARSABLE", "frame 0 produced no pixels")
    th = 16 + c["pix_th"] * 219                 # the black level of limited range is 16 and white 235; full-range black (0) also falls under the threshold
    ratio = sum(1 for x in b if x <= th) / len(b)
    black = ratio >= c["black_ratio"]
    return _item("first_frame", "fail" if black else "pass",
                 f"share of frame-0 pixels with luma <= {th:.0f} is >= {c['black_ratio']}: black screen, fail (frame 0 must show a picture)",
                 {"dark_ratio": round(ratio, 4)},
                 [{"t": [0.0, round(1 / fps, 4) if fps else 0.0], "frames": [0, 1], "offending": True}] if black else [])


def black(video, dur, fps, c=None):
    err = _ff(["-i", video, "-an", "-vf", f"blackdetect=d={0.5 / fps:.4f}:pix_th={BLACK_PIX_TH}:pic_th={BLACK_PIC_TH}"])
    iv = [(float(a), float(b)) for a, b in _events(err, "black_start", r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")]
    allowed = []
    if c:                       # the overlap of a fadeblack transition is intentional black
        for i, s in enumerate(c["plan"]["shots"][:-1]):
            if c["trans"][i] and s["transition_out"]["type"] == "fadeblack":
                st = c["starts"][i + 1]
                allowed.append((st / fps, (st + c["trans"][i]) / fps))
    ev, bad = [], False
    for a, b in iv:
        ok = any(x - 1 / fps <= a and b <= y + 1 / fps for x, y in allowed)
        bad |= not ok
        ev.append({"t": [round(a, 3), round(b, 3)], "frames": [int(round(a * fps)), int(round(b * fps))],
                   "allowed_fadeblack": ok, "offending": not ok})
    rule = f"blackdetect pix_th={BLACK_PIX_TH} pic_th={BLACK_PIC_TH} d≥{0.5 / fps:.4f}s(a single frame counts); fadeblack overlaps in the plan are allowed"
    return _item("black_frames", "fail" if bad else "pass", rule,
                 {"intervals": len(iv), "black_s": round(sum(b - a for a, b in iv), 3)}, ev)


def _shot_moves(c, i, f0, f1):
    """Whether the compiled camera move is moving in frames [f0, f1) of a shot (the framing window changes)."""
    s = c["plan"]["shots"][i]
    if s.get("kind") == "web":
        return None
    cam = c["cams"][s["id"]]
    if cam.static:
        return False
    f0, f1 = max(0, f0), min(cam.n - 1, f1)
    prev = cam.at(f0)
    for f in range(f0 + 1, f1 + 1):
        r = cam.at(f)
        if max(abs(r[k] - prev[k]) for k in range(4)) > 1e-3:
            return True
        prev = r
    return False


def _freeze_iv(video, dur, noise, min_s):
    err = _ff(["-i", video, "-an", "-vf", f"freezedetect=n={noise}:d={min_s}"])
    starts = [float(x) for x in _events(err, "freeze_start", r"freeze_start:\s*([0-9.]+)")]
    ends = [float(x) for x in _events(err, "freeze_end", r"freeze_end:\s*([0-9.]+)")]
    return [(a, ends[k] if k < len(ends) else dur) for k, a in enumerate(starts)]


def freeze(video, dur, fps, c=None):
    iv = _freeze_iv(video, dur, FREEZE_NOISE, FREEZE_MIN_S)
    rule = (f"freezedetect n={FREEZE_NOISE} d≥{FREEZE_MIN_S}s; with a plan: only inside static image shots (camera=[]) gives pass,"
            "inside a shot whose camera is moving gives fail, video/web shots (the source may itself be static) give needs_review; without a plan, needs_review")
    ev, worst = [], "pass"
    rank = {"pass": 0, "needs_review": 1, "fail": 2}
    for a, b in iv:
        fa, fb = int(round(a * fps)), int(round(b * fps))
        verdict, why = "needs_review", "no plan, so it cannot be judged whether the stillness is intentional"
        shots = []
        if c:
            verdict, why = "pass", "static image shot"
            for i, st in enumerate(c["starts"]):
                en = st + c["frames"][i]
                if st < fb and en > fa:
                    s = c["plan"]["shots"][i]
                    kind = c["metas"][s["id"]]["kind"]
                    mv = _shot_moves(c, i, max(fa, st) - st, min(fb, en) - st)
                    shots.append(s["id"])
                    if mv:
                        v, w = "fail", f"{s['id']} should have camera movement here"
                    elif kind == "image":
                        v, w = "pass", "static image shot"
                    else:
                        v, w = "needs_review", f"{s['id']} is a {kind} shot; the source picture may itself be static"
                    if rank[v] > rank[verdict]:
                        verdict, why = v, w
        worst = max(worst, verdict, key=rank.get)
        ev.append({"t": [round(a, 3), round(b, 3)], "frames": [fa, fb], "shots": shots, "verdict": verdict,
                   "reason": why, "offending": verdict != "pass"})
    return _item("freeze", worst, rule, {"intervals": len(iv)}, ev)


def long_freeze(video, dur, prof):
    c = prof["long_freeze"]
    iv = [(a, b) for a, b in _freeze_iv(video, dur, c["noise"], c["max_s"]) if b - a > c["max_s"]]
    ev = [{"t": [round(a, 3), round(b, 3)], "dur_s": round(b - a, 2), "offending": True} for a, b in iv]
    return _item("long_freeze", c["action"] if iv else "pass",
                 f"freezedetect n={c['noise']}: still for more than {c['max_s']} s (frame-hold padding) gives {c['action']}; "
                 "intentional long holds go into profile.allow", {"intervals": len(iv)}, ev)


def _low_runs(d, c, sf):
    """Sequence of adjacent differences to low-motion runs [(start s, end s, max difference in the run)]. An isolated single jitter sample (< spike_max, low on both sides) does not break a run."""
    lo, spike = c["diff_max"], c.get("spike_max", c["diff_max"])
    low = [x < lo or (x < spike and 0 < i < len(d) - 1 and d[i - 1] < lo and d[i + 1] < lo) for i, x in enumerate(d)]
    runs, start = [], None
    for i, ok in enumerate(low + [False]):
        if ok:
            start = i if start is None else start
        elif start is not None:
            a_, b_ = start / sf, i / sf          # difference i covers sample i to i+1
            if b_ - a_ >= c["window_s"]:
                runs.append((a_, b_, max(d[start:i])))
            start = None
    return runs


def low_motion(video, dur, prof):
    """Low-resolution grayscale is sampled at sample_fps; an average difference between adjacent samples below diff_max for at least window_s means a slow text-card push, a freeze or a static shot.
    Calibration (96 px wide, 4 fps): a text card pushed slowly from 1.00 to 1.08 gives <= 0.25; high-contrast colour bars pushed 1.00 to 1.06 give <= 0.65 and panned <= 0.76;
    a real talking-head clip and a 278 s dance recording give a maximum difference of >= 0.91 in any 4 s window. The 0.8 threshold lies between them with a small margin,
    so it only gives needs_review. Aligning by global scale/pan before comparing was tried: zoompan itself has rounding jitter and the residual after alignment is still 0.6-1.0, so it is unusable.
    The same slow-push clip had one sample at 0.82 on ffmpeg 6.1 (Linux), cutting a 4.25 s run into 3.5 s and missing it. Isolated jitter is now tolerated:
    a single sample below spike_max (0.9) with both neighbours below diff_max does not break the run. Median smoothing was tried and gave 8 false runs on the dance recording, so it is unusable;
    the isolated-jitter rule gives exactly the same results as the original rule on the two kinds of real footage above (4 clips in total)."""
    c = prof["low_motion"]
    sf = c["sample_fps"]
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-an", "-vf", f"fps={sf},scale=96:-2,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "low-motion sampling failed", stderr=p.stderr.decode(errors="replace")[-300:])
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", video], capture_output=True, text=True)
    try:
        W, H = (int(x) for x in pr.stdout.strip().split(",")[:2])
    except ValueError:
        raise ReelError("OUTPUT_UNPARSABLE", "cannot read the frame size", stdout=pr.stdout[-200:])
    h = max(2, int(round(H * 96 / W / 2)) * 2)
    n = len(p.stdout) // (96 * h)
    if n < 2 or n < dur * sf * 0.8:
        raise ReelError("OUTPUT_UNPARSABLE", "too few low-motion samples to cover the whole video", frames=n, expect=round(dur * sf))
    try:
        import numpy as np
        a = np.frombuffer(p.stdout[: n * 96 * h], np.uint8).reshape(n, h * 96).astype(np.float32)
        d = [float(x) for x in np.abs(np.diff(a, axis=0)).mean(axis=1)]
    except ImportError:
        sz = 96 * h
        fr = [p.stdout[i * sz:(i + 1) * sz] for i in range(n)]
        d = [sum(abs(x - y) for x, y in zip(fr[i], fr[i + 1])) / sz for i in range(n - 1)]
    runs = _low_runs(d, c, sf)
    ev = [{"t": [round(a_, 2), round(b_, 2)], "max_diff": round(m, 3), "offending": True} for a_, b_, m in runs]
    return _item("low_motion", c["action"] if runs else "pass",
                 f"{sf} fps low-resolution sampling, average difference of adjacent frames < {c['diff_max']} for at least {c['window_s']} s (long slow text-card push, freeze,"
                 f"static picture) gives {c['action']}: a human must confirm it is intentional", {"samples": n, "runs": len(runs)}, ev)


def _samples(video):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-vn", "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "audio decoding failed", stderr=p.stderr.decode()[-300:])
    ch = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                         "stream=channels,sample_rate", "-of", "json", video], capture_output=True, text=True)
    try:
        st = json.loads(ch.stdout)["streams"][0]
        sr, nch = int(st["sample_rate"]), int(st["channels"])
    except (ValueError, KeyError, IndexError):
        raise ReelError("OUTPUT_UNPARSABLE", "cannot read the audio sample rate / channels", stdout=ch.stdout[-200:])
    return p.stdout, sr, nch


def clipping(video):
    raw, sr, nch = _samples(video)
    try:
        import numpy as np
        x = np.frombuffer(raw, dtype="<f4")
        hit = np.nonzero(np.abs(x) >= CLIP_LEVEL)[0]
        n_clip = int(hit.size)
        peak = float(np.abs(x).max()) if x.size else 0.0
        times = (hit // nch / sr)
        times = [float(t) for t in times]
    except ImportError:
        import array
        x = array.array("f")
        x.frombytes(raw[: len(raw) // 4 * 4])
        times, peak = [], 0.0
        for k, v in enumerate(x):
            a = -v if v < 0 else v
            if a > peak:
                peak = a
            if a >= CLIP_LEVEL:
                times.append(k // nch / sr)
        n_clip = len(times)
    regions = []
    for t in times:                       # merge into ranges (a gap over 50ms starts a new one)
        if regions and t - regions[-1][1] <= 0.05:
            regions[-1][1] = t
        else:
            regions.append([t, t])
    status = "fail" if n_clip >= CLIP_MIN_SAMPLES else "pass"
    return status, {"clipped_samples": n_clip, "sample_peak": round(peak, 4),
                    "sample_peak_dbfs": round(20 * math.log10(peak), 2) if peak > 0 else None}, \
        [{"t": [round(a, 3), round(b, 3)], "offending": True} for a, b in regions[:20]]


def loudness(video):
    err = _ff(["-i", video, "-vn", "-af", "ebur128=peak=true+sample"])
    if "Summary:" not in err:
        raise ReelError("OUTPUT_UNPARSABLE", "ebur128 printed no Summary", stderr=err[-300:])
    summ = err[err.rfind("Summary:"):]
    num = lambda s: -math.inf if s == "-inf" else float(s)
    get = lambda pat: (num(m.group(1)) if (m := re.search(pat, summ, re.S)) else None)
    r = {"integrated_lufs": get(r"I:\s*(-?[0-9.]+|-inf)\s*LUFS"), "lra_lu": get(r"LRA:\s*(-?[0-9.]+)\s*LU"),
         "true_peak_dbtp": get(r"True peak:\s*Peak:\s*(-?[0-9.]+|-inf)"),
         "sample_peak_dbfs": get(r"Sample peak:\s*Peak:\s*(-?[0-9.]+|-inf)")}
    miss = [k for k in ("integrated_lufs", "true_peak_dbtp") if r[k] is None]
    if miss:                                  # silence gives -inf (it is parsed), only a missing field is None
        raise ReelError("OUTPUT_UNPARSABLE", f"ebur128 Summary lacks {miss}", summary=summ[:300])
    return {k: (None if v is not None and math.isinf(v) and k != "integrated_lufs" else v) for k, v in r.items()}


def audio_items(video, prof):
    st, meas, ev = clipping(video)
    lu = loudness(video)
    tp_c, lo_c = prof["true_peak"], prof["loudness"]
    clip = _item("audio_clipping", st, f"|sample|≥{CLIP_LEVEL} samples number >= {CLIP_MIN_SAMPLES}: fail; "
                 f"true peak > {tp_c['max_dbtp']} dBTP gives {tp_c['action']}", {**meas, "true_peak_dbtp": lu["true_peak_dbtp"]}, ev)
    tp = lu["true_peak_dbtp"]
    if st == "pass" and tp is not None and tp > tp_c["max_dbtp"]:
        clip["status"] = tp_c["action"]
        clip["note"] = f"not clipped, but the true peak {tp} dBTP > {tp_c['max_dbtp']}; transcoding / platform re-compression may clip"
    il = lu["integrated_lufs"]
    il_out = None if math.isinf(il) else il
    if prof["audio"] == "silent":
        ls = "pass" if il <= -60 else "fail"
        rule = "silent version: integrated <= -60 LUFS"
    else:
        lo, hi = lo_c["range"]
        ls = "pass" if (not math.isinf(il) and lo <= il <= hi) else lo_c["action"]
        rule = f"ebur128 integrated within [{lo}, {hi}] LUFS gives pass, otherwise {lo_c['action']} (the loudness target differs by platform; follow the profile)"
    return [clip, _item("loudness", ls, rule, {**lu, "integrated_lufs": il_out})]


def silence_items(video, dur, prof):
    c = prof["silence"]
    if prof["audio"] == "silent":
        note = "silent-version profile: silence at head and tail is not checked"
        return [_item(n, "pass", "not checked for a silent version", note=note) for n in ("head_silence", "tail_silence", "mid_silence")]
    err = _ff(["-i", video, "-vn", "-af", f"silencedetect=n={c['noise_db']}dB:d={c['min_s']}"])
    ss = [float(x) for x in _events(err, "silence_start", r"silence_start:\s*(-?[0-9.]+)")]
    se = [float(x) for x in _events(err, "silence_end", r"silence_end:\s*([0-9.]+)")]
    sil = [(max(0.0, s), se[i] if i < len(se) else dur) for i, s in enumerate(ss)]
    head = 0.0
    for s, e in sil:                       # continuous silence from 0 (it may be split into several adjacent segments)
        if s <= head + 0.1:
            head = max(head, e)
    hs = c["head_action"] if head >= c["head_max_s"] else "pass"
    h_item = _item("head_silence", hs, f"silencedetect n={c['noise_db']}dB: continuous silence at the start >= {c['head_max_s']} s gives "
                   f"{c['head_action']}(there must be sound at the start; the music should not begin in a blank intro)", {"head_silence_s": round(head, 2)},
                   [{"t": [0.0, round(head, 2)], "offending": True}] if hs != "pass" else [])
    tail = [(s, e) for s, e in sil if e >= dur - 0.05 and e - s >= c["tail_max_s"]]
    tail_s = max((e - s for s, e in sil if e >= dur - 0.05), default=0.0)
    ts = c["tail_action"] if tail else "pass"
    t_item = _item("tail_silence", ts, f"continuous silence at the end >= {c['tail_max_s']} s gives {c['tail_action']} (is the music / voice-over shorter than the picture?)",
                   {"tail_silence_s": round(tail_s, 2)},
                   [{"t": [round(s, 2), round(e, 2)], "offending": True} for s, e in tail])
    mid = [(s, e) for s, e in sil if s > head + 0.1 and (s, e) not in tail and e - s >= c["mid_review_s"]]
    m_item = _item("mid_silence", "needs_review" if mid else "pass",
                   f"silence in the middle >= {c['mid_review_s']} s gives needs_review (confirm it is not a dropout)", {"segments": len(mid)},
                   [{"t": [round(s, 2), round(e, 2)], "offending": True} for s, e in mid])
    return [h_item, t_item, m_item]


# ---------------- summary ----------------

def _guarded(items, names, fn):
    """A detection error does not abort the whole report: the affected items are recorded as not_checked + error and the gate counts them as blockers."""
    try:
        r = fn()
    except ReelError as e:
        err = e.to_dict()
    except Exception as e:  # noqa: BLE001
        err = {"code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}"}
    else:
        items.extend(r if isinstance(r, list) else [r])
        return
    for n in names:
        items.append(_item(n, "not_checked", "detection error, result unknown", note=f"{err['code']}: {err['message']}", error=err))


def _apply_allow(items, prof):
    """Exceptions registered in profile.allow: if all violating evidence of the item falls inside the registered ranges, it is pass (recorded as excused)."""
    allow = prof.get("allow") or []
    for it in items:
        if it["status"] not in ("fail", "needs_review"):
            continue
        rules = [a for a in allow if a["check"] == it["check"]]
        bad = [e for e in it["evidence"] if e.get("offending")]
        if not rules or not bad or any("t" not in e for e in bad):
            continue
        cover = lambda e: next((a for a in rules if a["t"][0] - 1e-3 <= e["t"][0] and e["t"][1] <= a["t"][1] + 1e-3), None)
        hits = [cover(e) for e in bad]
        if all(hits):
            it["excused"] = {"from": it["status"], "by_profile_allow": [{"t": a["t"], "reason": a["reason"]} for a in hits]}
            it["status"] = "pass"


def run(video, c=None, prof=None, text_sources=None, contract=None):
    from . import profiles
    prof = prof or profiles.load()
    need_tools()
    info, dur = _probe(video)
    items = []
    v0 = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    fps = c["plan"]["output"]["fps"] if c else (_fps(v0) or 30.0)
    has_audio = any(s.get("codec_type") == "audio" for s in info["streams"])
    _guarded(items, ["decode_clean"], lambda: decode_clean(video))
    _guarded(items, ["format"], lambda: fmt(info, prof))
    _guarded(items, ["first_frame"], lambda: first_frame(video, fps, prof))
    _guarded(items, ["black_frames"], lambda: black(video, dur, fps, c))
    _guarded(items, ["freeze"], lambda: freeze(video, dur, fps, c))
    _guarded(items, ["long_freeze"], lambda: long_freeze(video, dur, prof))
    _guarded(items, ["low_motion"], lambda: low_motion(video, dur, prof))
    if has_audio:
        _guarded(items, ["audio_clipping", "loudness"], lambda: audio_items(video, prof))
        _guarded(items, ["head_silence", "tail_silence", "mid_silence"], lambda: silence_items(video, dur, prof))
    elif prof["audio"] == "silent":
        items.append(_item("audio_clipping", "pass", "silent version: there is no audio track to check"))
        items.append(_item("loudness", "pass", "silent version: there is no audio track to check"))
        items.extend(silence_items(video, dur, prof))
    else:
        st = "fail" if prof["audio"] == "required" else "not_checked"
        items.append(_item("audio_clipping", st, "the video should have an audio track", {"audio_stream": False}))
        items.append(_item("loudness", "not_checked", "no audio track"))
        for n in ("head_silence", "tail_silence", "mid_silence"):
            items.append(_item(n, "not_checked", "no audio track"))
    if c:
        p = c["plan"]
        au = p["audio"]
        cues = subtitles.cues(subtitles.parse(planmod.resolve(p, au["subtitles"]))) if au.get("subtitles") else None
        o = p["output"]
        v = verify.check(video, expect_s=c["voice_s"], fps=o["fps"], width=o["width"], height=o["height"],
                         subs_cues=cues)
        for ch in v["checks"]:
            name = "subtitles_in_range" if ch["check"] == "subtitles_in_range" else f"verify.{ch['check']}"
            meas = {k: val for k, val in ch.items() if k not in ("check", "ok")}
            ev = [{"t": x, "offending": True} for x in ch.get("out_of_range", [])] if ch["check"] == "subtitles_in_range" else []
            items.append(_item(name, "pass" if ch["ok"] else "fail", "verify.check (the same rules as render acceptance)", meas, ev))
        if cues is None:
            items.append(_item("subtitles_in_range", "not_checked", "the plan has no audio.subtitles"))
    else:
        items.append(_item("subtitles_in_range", "not_checked", "--plan is needed to know the subtitle cues"))
    if contract:
        from . import mvcheck
        mvcheck.run(video, contract, info, dur, fps, items)
    ts = textscan.scan(text_sources or [], prof)
    items.append(_item("text_scan", ts["status"], ts["rule"], ts["measured"], ts["evidence"], ts.get("note")))
    for name, why in NOT_IMPLEMENTED:
        items.append(_item(name, "not_checked", why))
    _apply_allow(items, prof)
    checked = [x for x in items if x["status"] != "not_checked"]
    status = ("fail" if any(x["status"] == "fail" for x in checked) else
              "needs_review" if any(x["status"] == "needs_review" for x in checked) else "pass")
    count = {k: sum(1 for x in items if x["status"] == k) for k in ("pass", "fail", "needs_review", "not_checked")}
    extra = {}
    if contract:
        from . import mvcheck
        extra = {"contract": {"path": contract["_path"], "sha256": contract["_sha256"]},
                 "contract_required": mvcheck.required(contract)}
    return {**extra, "video": video, "video_sha256": sha256(video), "duration_s": round(dur, 3), "fps": fps, "plan": bool(c),
            "profile": {"name": prof.get("name"), "sha256": prof.get("_sha256"), "chain": prof.get("_chain")},
            "status": status, "status_scope": "only covers the items that were checked; not_checked items are unverified; whether it can be delivered is up to the gate",
            "counts": count, "items": items}
