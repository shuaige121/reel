"""MV acceptance contract (reel.contract.v1): independent of the render plan; an MV from any renderer is checked by the same rules.

Contract (JSON):
  {"version": "reel.contract.v1", "kind": "mv",
   "output": {"width": 720, "height": 1280, "fps": 30},
   "songs": [{"path": "song.m4a", "sha256": "...", "at": 0.0, "policy": "full"}],
            # a compilation lists several songs, at = where the song starts in the video; policy: full (play completely, the default) | trim (must carry "range": [a, b] and "reason")
   "lyrics": {"path": "x.lyrics.json", "sha256": "..."},          # the output of reel lyrics import; optional
   "text_layer": {"path": "textlayer.mkv", "sha256": "..."},             # grayscale mask of the lyric glyphs (bright = text), same length and frame rate as the video;
                                                                   # glyphs only, no particles, halos or flashes; required when lyrics is given
   "required": [], "release": {"regions": "...", "ai_label": true},  # release is only recorded, not judged
   "effects": {"path": "effects-layer.wav", "sha256": "...", "reason": "the user's own words"}}
            # Optional: sound effects the user asked to overlay. The effects layer = the video's audio track minus the original song (used when a single song plays completely from 0 s of the video);
            # song_identity / song_level use "original song + effects layer" as the reference; every audible 5 s window of the effects layer must correlate with the original song at |r| <= 0.3
            # (changing the song's gain or replacing a section correlates strongly with the original and cannot pass as an effect)

New check items (all are required under an mv contract and a profile cannot remove them):
  contract_files     every file in the contract exists and its sha256 is unchanged
  av_duration_match  audio/video stream duration difference of at most 1 frame
  output_format      width, height and frame rate equal the contract
  song_identity      each song over its full length: one window per 5 s, offset <= 1 ms, correlation >= 0.95, level difference <= 0.5 dB (silent windows skipped), and the whole song covered
                     (policy=full forbids truncation); in a compilation each song is located by at; when effects is declared the reference is the song plus the effects layer, and the effects layer must not correlate with the song
  song_level         true peak of the video <= original + 1.0 dB (lossy re-encoding raises it by about 0.5 dB), overall loudness difference <= 0.5 LU; when it passes, the default profile's absolute loudness / clipping
                     needs_review is recorded as excused (the audio track is used unchanged and the clipping comes from the original master)
  lyrics_on_screen   for each line take 3 frames in [t0+30%, t1-0.1]; the line counts as on screen when the text-layer pixels with ink >= character count x minimum glyph area (in at least 2 frames)
  lyrics_not_early   between [max(previous line end, t0-0.5), t0 - 1 frame] the text layer must not newly grow text of this line's size
Decoding is always done on the whole stream and then sliced by sample (input-side -ss gives a false 0.3 ms offset on AAC).
"""
import json, os, re, subprocess, unicodedata
import numpy as np
from .errors import ReelError
from . import check

SR = 16000
WIN_S = 5.0
LAG_MAX_S = 0.001
CORR_MIN = 0.95
GAIN_MAX_DB = 0.5
QUIET_DB = -45.0
EFF_CORR_MAX = 0.3   # upper limit of the correlation between the declared effects layer and the original song (every audible 5 s window)
TP_SLACK_DB = 1.0     # lossy re-encoding raises the inter-sample peak: Opus to AAC was measured at +0.5 dB (loudness unchanged); an overall gain change is caught by the LUFS difference and song_identity
LOUD_SLACK_LU = 0.5
INK_TH = 64          # text-layer grayscale > 64 counts as ink
MIN_CHAR_PX = 120    # each character has at least this many pixels (a conservative lower bound for a 720-wide picture and a font size of 40 px or more)
REQUIRED = ["contract_files", "av_duration_match", "output_format", "song_identity", "song_level"]
REQUIRED_LYRICS = ["lyrics_on_screen", "lyrics_not_early"]


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError) as e:
        raise ReelError("CONTRACT_INVALID", f"cannot read the contract: {e}", path=path)
    if c.get("version") != "reel.contract.v1" or c.get("kind") != "mv":
        raise ReelError("CONTRACT_INVALID", "not reel.contract.v1 / kind=mv", path=path)
    if not c.get("songs"):
        raise ReelError("CONTRACT_INVALID", "the contract has no songs", path=path)
    base = os.path.dirname(os.path.abspath(path))
    rel = lambda p: p if os.path.isabs(os.path.expanduser(p)) else os.path.join(base, p)
    for s in c["songs"]:
        s["path"] = rel(s["path"])
        s.setdefault("at", 0.0); s.setdefault("policy", "full")
        if s["policy"] == "trim" and not (s.get("range") and s.get("reason")):
            raise ReelError("CONTRACT_INVALID", "policy=trim must give range and reason", song=s["path"])
    for k in ("lyrics", "text_layer", "effects"):
        if c.get(k):
            c[k]["path"] = rel(c[k]["path"])
    if c.get("effects"):
        e = c["effects"]
        if not (e.get("path") and e.get("sha256") and (e.get("reason") or "").strip()):
            raise ReelError("CONTRACT_INVALID", "effects must give path, sha256 and reason (the user's own words asking for the sound effects)", path=path)
        if len(c["songs"]) != 1 or c["songs"][0]["policy"] != "full" or float(c["songs"][0]["at"]) != 0.0:
            raise ReelError("CONTRACT_INVALID", "effects only supports MVs with a single song, played completely, starting at 0 s", path=path)
    if c.get("lyrics") and not c.get("text_layer"):
        raise ReelError("CONTRACT_INVALID", "when lyrics is given, text_layer (the text-layer mask) is required, otherwise on-screen and early-appearance checks are impossible", path=path)
    if c.get("lyrics"):   # lyrics timing only accepts the output of reel lyrics import and it must belong to the contract's song (a hand-written timetable does not count)
        try:
            with open(c["lyrics"]["path"], encoding="utf-8") as fh:
                L = json.load(fh)
        except (OSError, ValueError) as e:
            raise ReelError("CONTRACT_INVALID", f"cannot read lyrics: {e}", path=c["lyrics"]["path"])
        if not isinstance(L, dict) or L.get("version") != "reel.lyrics.v1":
            raise ReelError("CONTRACT_INVALID", "lyrics must be the output of reel lyrics import (version=reel.lyrics.v1)",
                            path=c["lyrics"]["path"])
        if (L.get("song") or {}).get("sha256") not in {s.get("sha256") for s in c["songs"]}:
            raise ReelError("CONTRACT_INVALID", "the song of lyrics is not the contract's song (song.sha256 does not match)", path=c["lyrics"]["path"])
    c["_path"] = os.path.abspath(path)
    c["_sha256"] = check.sha256(path)
    return c


def required(c):
    return REQUIRED + (REQUIRED_LYRICS if c.get("lyrics") else []) + list(c.get("required") or [])


def _pcm(path, ch=1):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", str(ch), "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "audio decoding failed", path=path, stderr=p.stderr[-300:].decode(errors="ignore"))
    a = np.frombuffer(p.stdout, dtype=np.float32)
    return a.reshape(-1, ch) if ch > 1 else a


def _ebur(path):
    err = check._ff(["-i", path, "-vn", "-af", "ebur128=peak=true"])
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", err); tp = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", err)
    if not i or not tp:
        raise ReelError("OUTPUT_UNPARSABLE", "ebur128 output cannot be parsed", path=path)
    return float(i[-1]), float(tp[-1]) if tp[-1] != "-inf" else -120.0


def _item(*a, **k):
    return check._item(*a, **k)


def contract_files(c):
    bad = []
    deps = [("song", s) for s in c["songs"]] + [(k, c[k]) for k in ("lyrics", "text_layer", "effects") if c.get(k)]
    for kind, d in deps:
        if not os.path.exists(d["path"]):
            bad.append({"which": kind, "path": d["path"], "why": "file missing"})
        elif d.get("sha256") and check.sha256(d["path"]) != d["sha256"]:
            bad.append({"which": kind, "path": d["path"], "why": "sha256 changed (the file was replaced after the contract was written)"})
        elif not d.get("sha256"):
            bad.append({"which": kind, "path": d["path"], "why": "the contract has no sha256"})
    return _item("contract_files", "fail" if bad else "pass", "all files in the contract exist and their hashes are unchanged",
                 {"files": len(deps)}, [{"offending": True, **b} for b in bad])


def av_duration(info, fps):
    vs = [s for s in info["streams"] if s.get("codec_type") == "video"]
    as_ = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if not vs or not as_:
        return _item("av_duration_match", "fail", "the video needs a video stream and an audio stream", {"video": bool(vs), "audio": bool(as_)})
    vd, ad = float(vs[0].get("duration") or 0), float(as_[0].get("duration") or 0)
    ok = abs(vd - ad) <= 1.0 / fps + 1e-3
    return _item("av_duration_match", "pass" if ok else "fail", "video and audio stream durations differ by at most 1 frame",
                 {"video_s": round(vd, 3), "audio_s": round(ad, 3), "diff_s": round(vd - ad, 3)},
                 [] if ok else [{"t": [round(min(vd, ad), 3), round(max(vd, ad), 3)], "offending": True}])


def output_format(info, c, fps):
    o = c.get("output") or {}
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    got = {"width": v.get("width"), "height": v.get("height"), "fps": round(fps, 3)}
    bad = {k: (got[k], o[k]) for k in ("width", "height", "fps") if o.get(k) is not None and abs(float(got[k] or 0) - float(o[k])) > 0.01}
    if not o:
        return _item("output_format", "not_checked", "the contract has no output (width, height, frame rate)", got)
    return _item("output_format", "fail" if bad else "pass", "width, height and frame rate equal the contract", {**got, "mismatch": bad})


def song_identity(video, c):
    mv = _pcm(video)
    ev, meas = [], []
    eff = _pcm(c["effects"]["path"]) if c.get("effects") else None
    for s in c["songs"]:
        song = _pcm(s["path"])
        if eff is not None:   # reference = original song + declared effects layer; the effects layer must first pass "not correlated with the song"
            e = np.zeros(len(song), np.float32); e[:min(len(e), len(eff))] = eff[:len(song)]
            win = int(WIN_S * SR); active = 0
            for k in range(0, len(song) - SR, win):
                x, y = e[k:k + win], song[k:k + win]
                if 20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-12) < QUIET_DB:
                    continue
                active += 1; x = x - x.mean(); y = y - y.mean()
                r = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-12))
                if abs(r) > EFF_CORR_MAX:
                    ev.append({"song": os.path.basename(s["path"]), "t": [round(k / SR, 2), round((k + len(x)) / SR, 2)], "effects_song_corr": round(r, 3),
                               "why": "the declared effects layer correlates with the song: this alters the song instead of overlaying effects", "offending": True})
            song = song + e
            meas.append({"effects": os.path.basename(c["effects"]["path"]), "reason": c["effects"]["reason"], "effect_windows_5s": active})
        if s["policy"] == "trim":
            a, b = s["range"]; song = song[int(a * SR):int(b * SR)]
        i0 = int(round(s["at"] * SR))
        seg = mv[i0:i0 + len(song)]
        if len(song) < SR or len(seg) < SR:
            ev.append({"song": s["path"], "why": "no audio could be decoded", "offending": True}); continue
        if len(seg) < len(song) - SR / 30:
            ev.append({"song": s["path"], "why": f"this song is only {len(seg) / SR:.2f}s in the video vs {len(song) / SR:.2f}s original (truncated)",
                       "t": [round(s["at"] + len(seg) / SR, 3), round(s["at"] + len(song) / SR, 3)], "offending": True})
        n = min(len(seg), len(song)); win = int(WIN_S * SR); checked = 0
        for k in range(0, n - SR, win):
            x, y = seg[k:k + win], song[k:k + win]
            m = min(len(x), len(y)); x = x[:m] - x[:m].mean(); y = y[:m] - y[:m].mean()
            ry = np.sqrt(np.mean(y ** 2)) + 1e-12
            if 20 * np.log10(ry) < QUIET_DB:
                continue
            cc = np.fft.irfft(np.fft.rfft(x, 2 * m) * np.conj(np.fft.rfft(y, 2 * m)))
            lag = int(np.argmax(cc)); lag = lag - 2 * m if lag > m else lag
            r = float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-12))
            g = 20 * np.log10((np.sqrt(np.mean(x ** 2)) + 1e-12) / ry)
            checked += 1
            if abs(lag) > LAG_MAX_S * SR or r < CORR_MIN or abs(g) > GAIN_MAX_DB:
                t0 = s["at"] + k / SR
                ev.append({"song": os.path.basename(s["path"]), "t": [round(float(t0), 2), round(float(t0 + m / SR), 2)],
                           "lag_ms": round(float(lag) / SR * 1000, 2), "corr": round(float(r), 3), "gain_db": round(float(g), 2),
                           "offending": True})
        if checked == 0:
            ev.append({"song": s["path"], "why": "no window is comparable (all silent?)", "offending": True})
        meas.append({"song": os.path.basename(s["path"]), "at": s["at"], "windows": checked, "song_s": round(len(song) / SR, 3)})
    return _item("song_identity", "fail" if ev else "pass",
                 f"each song over its full length, one window per {WIN_S:.0f}s: offset <= {LAG_MAX_S * 1000:.0f} ms, correlation >= {CORR_MIN}, level difference <= {GAIN_MAX_DB} dB; truncation is not allowed"
                 + ("; reference = original song + the effects layer declared in the contract, effects-layer vs song |r| <= %.1f" % EFF_CORR_MAX if eff is not None else ""),
                 {"songs": meas}, ev[:12])


def song_level(video, c):
    if len(c["songs"]) != 1 or c["songs"][0]["policy"] != "full":
        return _item("song_level", "not_checked", "relative level is only judged for MVs with a single, fully played song (for compilations see the per-song level difference in song_identity)")
    mi, mtp = _ebur(video); ref = c["songs"][0]["path"]; tmp = None
    if c.get("effects"):   # reference = original song + declared effects layer (summed in 48 kHz floating point, then measured)
        import tempfile
        dec = lambda p: np.frombuffer(subprocess.run(["ffmpeg", "-v", "error", "-i", p, "-vn", "-ac", "2", "-ar", "48000", "-f", "f32le", "-"],
                                                     capture_output=True, check=True).stdout, np.float32).reshape(-1, 2)
        a, b = dec(ref), dec(c["effects"]["path"]); m = np.zeros_like(a); m[:min(len(a), len(b))] = b[:len(a)]
        tmp = tempfile.mkdtemp(prefix="reel_ref_"); ref = os.path.join(tmp, "ref.wav")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", "-", "-c:a", "pcm_f32le", ref],
                       input=np.ascontiguousarray(a + m, "<f4").tobytes(), check=True)
    si, stp = _ebur(ref)
    if tmp:
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
    ok = mtp <= stp + TP_SLACK_DB and abs(mi - si) <= LOUD_SLACK_LU
    return _item("song_level", "pass" if ok else "fail",
                 f"video true peak <= original + {TP_SLACK_DB} dB, overall loudness difference <= {LOUD_SLACK_LU} LU (audio track used unchanged" + ("; the reference includes the effects layer declared in the contract)" if c.get("effects") else "）"),
                 {"mv_lufs": mi, "song_lufs": si, "mv_peak_dbfs": mtp, "song_peak_dbfs": stp})


def _matte_frames(path, times, w=180):
    """Take grayscale frames of the text layer at given times (scaled to width w); the whole stream is decoded once and frames are picked by number, avoiding the inaccuracy of -ss."""
    info, dur = check._probe(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise ReelError("ASSET_UNREADABLE", "the text layer has no video stream", path=path)
    W, H = int(v["width"]), int(v["height"]); h = int(round(H * w / W))
    fps = check._fps(v) or 30.0
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={w}:{h}:flags=area,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "text-layer decoding failed", path=path)
    frames = np.frombuffer(p.stdout, np.uint8).reshape(-1, h, w)
    scale = (W * H) / (w * h)
    out = {}
    for t in times:
        k = min(len(frames) - 1, max(0, int(round(t * fps))))
        out[t] = frames[k]
    return out, scale, dur, fps


def lyrics_items(video, c, vdur, vfps):
    with open(c["lyrics"]["path"], encoding="utf-8") as fh:
        L = json.load(fh)
    lines = L["lines"]
    tl = c["text_layer"]["path"]
    linfo, ldur = check._probe(tl)
    lv = next((s for s in linfo["streams"] if s.get("codec_type") == "video"), {})
    lfps = check._fps(lv) or 30.0
    if abs(ldur - vdur) > 1.0 / vfps + 1e-3 or abs(lfps - vfps) > 0.01:
        msg = f"text layer {ldur:.3f}s@{lfps:g} does not match video {vdur:.3f}s@{vfps:g}"
        return [_item("lyrics_on_screen", "fail", msg), _item("lyrics_not_early", "fail", msg)]
    want = []
    for ln in lines:
        d = ln["t1"] - ln["t0"]
        ln["_mid"] = ln["t0"] + d * 0.55
        ln["_pre"] = (max(0.0, ln["t0"] - 0.3), max(0.0, ln["t0"] - 1.0 / lfps))
        want += [ln["t0"] + d * f for f in (0.3, 0.55, 0.8)] + list(ln["_pre"])
    fr, scale, _, _ = _matte_frames(tl, sorted(set(round(t, 4) for t in want)))
    get = lambda t: fr[round(t, 4)]
    ink = lambda t: int((get(t) > INK_TH).sum() * scale)

    def dilate(m, r=3):
        out = m.copy()
        for _ in range(r):
            g = out.copy()
            g[1:, :] |= out[:-1, :]; g[:-1, :] |= out[1:, :]; g[:, 1:] |= out[:, :-1]; g[:, :-1] |= out[:, 1:]
            out = g
        return out
    on_ev, early_ev = [], []
    for i, ln in enumerate(lines):
        n = sum(unicodedata.category(c)[0] in "LN" for c in ln["text"]) or 1   # combining marks (Thai vowels, tone marks) take no width of their own
        need = n * MIN_CHAR_PX
        d = ln["t1"] - ln["t0"]
        hits = sum(ink(ln["t0"] + d * f) >= need for f in (0.3, 0.55, 0.8))
        if hits < 2:
            on_ev.append({"t": [ln["t0"], ln["t1"]], "text": ln["text"], "ink_px": [ink(ln["t0"] + d * f) for f in (0.3, 0.55, 0.8)],
                          "need_px": need, "offending": True})
        # early: ink that grows in the last 0.3 s before this line is sung, inside this line's own glyph area
        # (its mid-line mask) and outside the previous line's glyph area -> works for back-to-back lines too
        cur = dilate(get(ln["_mid"]) > INK_TH)
        prev = dilate(get(lines[i - 1]["_mid"]) > INK_TH, 4) if i else np.zeros_like(cur)
        a, b = ln["_pre"]
        if b - a < 1.0 / lfps:
            continue
        grown = (get(a) <= INK_TH // 2) & (get(b) > INK_TH * 2) & cur & ~prev
        px = int(grown.sum() * scale)
        if px >= need * 0.3:
            early_ev.append({"t": [round(a, 3), round(b, 3)], "text": ln["text"], "new_ink_px": px, "line_t0": ln["t0"],
                             "offending": True})
    return [_item("lyrics_on_screen", "fail" if on_ev else "pass",
                  f"each line has ink in at least 2 frames at 30/55/80% of the line (text-layer pixels >= character count x {MIN_CHAR_PX})", {"lines": len(lines)}, on_ev[:12]),
            _item("lyrics_not_early", "fail" if early_ev else "pass",
                  "appear only when sung: between 0.3 s before the line starts and the previous frame, the glyph region of this line (excluding the previous line's region) must not newly grow ink of 30% or more of this line's size",
                  {"lines": len(lines)}, early_ev[:12])]


def run(video, c, info, dur, fps, items):
    """Add the contract checks to the items of check.run and handle the default profile's absolute loudness / clipping items according to song_level."""
    from .check import _guarded
    _guarded(items, ["contract_files"], lambda: contract_files(c))
    _guarded(items, ["av_duration_match"], lambda: av_duration(info, fps))
    _guarded(items, ["output_format"], lambda: output_format(info, c, fps))
    _guarded(items, ["song_identity"], lambda: song_identity(video, c))
    _guarded(items, ["song_level"], lambda: song_level(video, c))
    if c.get("lyrics"):
        _guarded(items, REQUIRED_LYRICS, lambda: lyrics_items(video, c, dur, fps))
    lv = next((x for x in items if x["check"] == "song_level"), None)
    if lv and lv["status"] == "pass":
        for x in items:
            if (x["check"] == "loudness" and x["status"] == "needs_review") or \
                    (x["check"] == "audio_clipping" and x["status"] in ("needs_review", "fail")):
                x["excused"] = {"from": x["status"], "by_contract": "song_level: the video level matches the original song and the audio track is used unchanged"}
                x["status"] = "pass"
