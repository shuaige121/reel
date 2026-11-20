"""Final-video acceptance: the artefact itself decides; any failing item gives ok=False (the caller exits non-zero and does not print "done")."""
import json, subprocess


def _probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-show_streams", "-show_format", "-of", "json",
                        path], capture_output=True, text=True)
    if p.returncode != 0:
        return None, p.stderr[-500:]
    return json.loads(p.stdout), ""


def check(path, expect_s, fps, width, height, subs_cues=None, expect_audio=True, tol_s=0.10):
    checks = []

    def add(name, ok, **info):
        checks.append({"check": name, "ok": bool(ok), **info})

    info, err = _probe(path)
    if info is None:
        add("probe", False, stderr=err)
        return {"ok": False, "out": path, "checks": checks}
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    add("video_stream", v is not None)
    if v:
        add("size", (int(v["width"]), int(v["height"])) == (width, height), actual=[v["width"], v["height"]],
            expect=[width, height])
        num, den = (int(x) for x in v.get("r_frame_rate", "0/1").split("/"))   # mpv marks the last frame as discard, which skews avg
        add("fps", den and abs(num / den - fps) < 0.01, actual=round(num / den, 3) if den else None, expect=fps)
        nf = int(v.get("nb_read_frames") or 0)
        exp_f = round(expect_s * fps)
        add("frames", abs(nf - exp_f) <= max(2, round(tol_s * fps)), actual=nf, expect=exp_f)
        vd = float(v.get("duration") or nf / fps)
        add("video_duration", abs(vd - expect_s) <= tol_s, actual=round(vd, 3), expect=round(expect_s, 3))
    if expect_audio:
        add("audio_stream", a is not None)
        if a:
            ad = float(a.get("duration") or 0)
            add("audio_duration", abs(ad - expect_s) <= tol_s, actual=round(ad, 3), expect=round(expect_s, 3))
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "null", "-"], capture_output=True, text=True)
    add("decode_clean", dec.returncode == 0 and not dec.stderr.strip(), stderr=dec.stderr[-300:])
    if subs_cues is not None:
        vd_ = float(v.get("duration") or 0) if v else 0.0
        late = [[round(a, 3), round(b, 3)] for a, b, _ in subs_cues if a >= vd_ - 0.05 or b > vd_ + tol_s]
        add("subtitles_in_range", bool(subs_cues) and not late, cues=len(subs_cues), out_of_range=late,
            note="subtitle timing was checked; readability still needs a visual check on sampled frames")
    return {"ok": all(c["ok"] for c in checks), "out": path, "checks": checks}


def frame_png(path, t, out_png, width=None):
    vf = f"scale={width}:-2" if width else "null"
    p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t}", "-i", path, "-vf", vf, "-frames:v", "1",
                        out_png], capture_output=True, text=True)
    return p.returncode == 0
