"""Compile and execute: EditPlan, per-shot clips, concat/transitions, voice/BGM/colour/subtitles, final video, acceptance.

The timeline follows the voice-over and is quantised to frames:
  visible frames = sum of shot frames - sum of transition overlap frames; it must equal the voice-over frame count (require_match), or the last shot makes up the difference (fit_last_shot).
A backend only decides the encoder and the subtitle burn-in method; none keeps its own timeline arithmetic.
"""
import json, math, os, shutil, subprocess, tempfile, time
from . import camera, manifest, plan as planmod, subtitles, verify
from .errors import ReelError

GRADE = ("eq=contrast=1.10:saturation=1.06:brightness=0.005,"
         "colorbalance=rs=-0.06:gs=-0.01:bs=0.06:rm=0.05:bm=-0.05:rh=0.04:bh=-0.04,vignette=PI/4.6")
TOLERANCE_S = 0.10
SSIM_STATIC = 0.85          # below this the source picture is treated as having camera movement / large subject movement


def _ffmpeg_filters():
    p = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True)
    return {ln.split()[1] for ln in p.stdout.splitlines() if len(ln.split()) > 2 and ln.startswith(" ")}


def _ffmpeg_encoders():
    p = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
    return {ln.split()[1] for ln in p.stdout.splitlines() if len(ln.split()) > 2 and ln.startswith(" ")}


class Backend:
    name = "cpu"
    seg_enc = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p"]
    final_enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]

    def __init__(self):
        self.filters = _ffmpeg_filters()
        self.subs_mode = "ass" if "ass" in self.filters else ("mpv" if shutil.which("mpv") else None)
        # REEL_SUB_FONT overrides (public videos: an OFL font such as Noto Sans CJK SC instead of the system PingFang)
        self.font = os.environ.get("REEL_SUB_FONT") or ("PingFang SC" if self.subs_mode == "mpv" else "Noto Sans CJK SC")

    def describe(self):
        return {"backend": self.name, "subs": self.subs_mode, "encoder": self.final_enc[1]}


class NvencBackend(Backend):
    name = "nvenc"
    seg_enc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "16", "-pix_fmt", "yuv420p"]
    final_enc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", "21", "-pix_fmt", "yuv420p"]

    def __init__(self):
        super().__init__()
        if "h264_nvenc" not in _ffmpeg_encoders():
            raise ReelError("BACKEND_UNAVAILABLE", "the local ffmpeg has no h264_nvenc")


def make_backend(name):
    if name in ("cpu", "mac", "local"):
        return Backend()
    if name == "nvenc":
        return NvencBackend()
    raise ReelError("BACKEND_UNAVAILABLE", f"unknown backend {name!r} (cpu / nvenc; use --where remote for the remote GPU)")


def _fesc(path):
    """Embed a path as a filter option value in a filtergraph: first escape as an option value (\\ ' :), then as a filtergraph (\\ ' [ ] , ;)."""
    v = path
    for ch in ("\\", "'", ":", " ", "\t"):     # whitespace is escaped too: ffmpeg trims unescaped leading and trailing whitespace of option values
        v = v.replace(ch, "\\" + ch)
    for ch in ("\\", "'", "[", "]", ",", ";", " ", "\t"):
        v = v.replace(ch, "\\" + ch)
    return v


def run(cmd, what):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", f"{what} failed", cmd=" ".join(cmd[:12]) + " …",
                        stderr=p.stderr[-1500:])
    return p


# ---------------- compile ----------------

def compile_plan(plan, observations=None):
    """Validate, read the media and build the frame-level timeline. Does not render; returns an inspectable compile result (also used by dry-run)."""
    plan = planmod.validate(plan, observations)
    out = plan["output"]
    W, H, fps = out["width"], out["height"], out["fps"]
    metas, errs = {}, []
    for s in plan["shots"]:
        if s.get("kind") == "web":
            metas[s["id"]] = {"kind": "web", "path": s["web"]["url"]}
            continue
        p = planmod.resolve(plan, s["asset"])
        try:
            metas[s["id"]] = manifest.probe(p)
        except ReelError as e:
            errs.append(e.to_dict()); continue
        m = metas[s["id"]]
        if m["kind"] == "audio":
            errs.append({"code": "ASSET_KIND", "message": f"{s['id']} uses an audio file as picture", "detail": {"path": p}})
        elif m["kind"] == "image" and s["source_in"]:
            errs.append({"code": "PLAN_INVALID", "message": f"{s['id']}: source_in of an image must be 0", "detail": {}})
    au = plan["audio"]
    voice = planmod.resolve(plan, au["voice"])
    for key in ("voice", "subtitles", "bgm"):
        if au.get(key) and not os.path.isfile(planmod.resolve(plan, au[key])):
            errs.append({"code": "ASSET_NOT_FOUND", "message": f"audio.{key} does not exist",
                         "detail": {"path": planmod.resolve(plan, au[key])}})
    if plan["look"].get("lut") and not os.path.isfile(planmod.resolve(plan, plan["look"]["lut"])):
        errs.append({"code": "ASSET_NOT_FOUND", "message": "look.lut does not exist", "detail": {}})
    if errs:
        raise ReelError("PLAN_INVALID", f"media check found {len(errs)} errors", errors=errs)

    frames = [int(round(s["duration"] * fps)) for s in plan["shots"]]
    trans = [int(round(s["transition_out"]["duration"] * fps)) if s.get("transition_out") else 0
             for s in plan["shots"]]
    voice_s = manifest.media_duration(voice)
    voice_f = int(math.ceil(voice_s * fps - 1e-6))
    visual_f = sum(frames) - sum(trans)
    adjustments = []
    if plan["duration_policy"] == "fit_last_shot" and visual_f != voice_f:
        new = frames[-1] + (voice_f - visual_f)
        if new < 2:
            raise ReelError("DURATION_MISMATCH", "the voice-over is much shorter than the picture; the last shot cannot make up the difference",
                            visual_s=visual_f / fps, voice_s=voice_s)
        adjustments.append({"shot": plan["shots"][-1]["id"], "frames_from": frames[-1], "frames_to": new})
        frames[-1] = new
        plan["shots"][-1]["duration"] = new / fps
        visual_f = voice_f
        try:                       # after shortening, the camera move / transition may no longer be valid: re-validate, and never return a plan the validator itself would reject
            planmod.validate(json.loads(json.dumps(plan)), observations)
        except ReelError as e:
            raise ReelError("DURATION_MISMATCH", "the voice-over is shorter than the picture, and shortening the last shot to fit would break its camera move / transition; "
                            "shorten the earlier shots or rewrite the last shot", adjustment=adjustments[-1],
                            errors=e.detail.get("errors"))
    elif abs(visual_f - voice_f) > round(TOLERANCE_S * fps):
        raise ReelError("DURATION_MISMATCH", "total picture length differs from the voice-over (transitions eat the overlap time)",
                        visual_s=round(visual_f / fps, 3), voice_s=round(voice_s, 3),
                        transitions_s=round(sum(trans) / fps, 3),
                        hint="lengthen the shots, or use duration_policy=fit_last_shot")
    pads = {}
    for s, n in zip(plan["shots"], frames):
        m = metas[s["id"]]
        short = s["source_in"] + n / fps - (m.get("duration") or 0) if m["kind"] == "video" else 0
        if short > 1.5 / fps + 1e-6:
            raise ReelError("SOURCE_TOO_SHORT", f"{s['id']}: in-point plus duration exceeds the video length by {short:.3f}s (it would freeze)",
                            source_in=s["source_in"], need_s=round(n / fps, 3), source_s=m["duration"],
                            hint="shorten the shot or move the in-point earlier")
        if short > 1e-6:                   # a difference of at most 1.5 frames (duration rounding error): pad with the last frame, no error
            pads[s["id"]] = short
    for s, n in zip(plan["shots"], frames):
        m = metas[s["id"]]
        if m["kind"] == "video" and any(x["op"] == "zoom_to" for x in s["camera"]):
            a, b = s["source_in"], s["source_in"] + n / fps
            inside = [t for t in manifest.scene_cuts(m["path"]) if a + 0.05 < t < b - 0.05]
            if inside:
                raise ReelError("SCENE_CUT_IN_SHOT", f"{s['id']}: source range [{a:.2f}, {b:.2f}]s contains a scene cut {inside}, "
                                "a push-in on a single-frame box would land on a different picture; split the shot on both sides of the cut", cuts=inside)
            fixed = [x for x in s["camera"] if x["op"] == "zoom_to" and not x.get("track_boxes")]
            if fixed:
                sim = manifest.frame_ssim(m["path"], a, b - 1 / fps)
                if sim < SSIM_STATIC:
                    raise ReelError("SOURCE_CAMERA_MOVING",
                                    f"{s['id']}: the source picture moves by itself in this range (first/last SSIM={sim:.2f}<{SSIM_STATIC}），"
                                    "a single-frame box will lose the target; locate the same target at several times in the range and use zoom_to.track",
                                    ssim=round(sim, 3), source_window=[round(a, 3), round(b, 3)])
    cams = {}
    for s in plan["shots"]:
        if s.get("kind") != "web":
            cams[s["id"]] = camera.ShotCamera(s, metas[s["id"]], W, H, fps)
    starts = [0]
    for i in range(1, len(frames)):
        starts.append(starts[-1] + frames[i - 1] - trans[i - 1])
    return {"plan": plan, "metas": metas, "frames": frames, "trans": trans, "voice_s": voice_s,
            "voice_f": voice_f, "visual_f": visual_f, "adjustments": adjustments, "cams": cams,
            "starts": starts, "pads": pads, "voice_timeline": voice_timeline(plan) if any(
                s.get("kind") == "web" for s in plan["shots"]) else None}


def voice_timeline(plan):
    """Per-word voice timing: audio.words is preferred (whisper per-character, produced by reel.py words), otherwise shared evenly within each subtitle sentence."""
    from . import web
    au = plan["audio"]
    if au.get("words"):
        path = planmod.resolve(plan, au["words"])
        if path.endswith(".json"):
            with open(path, encoding="utf-8") as fh:
                ev = [(w["t0"], w["t1"], w["text"]) for w in json.load(fh)]
        else:
            ev = subtitles.parse(path)
        return web.char_timeline(ev)
    if au.get("subtitles"):
        return web.char_timeline(subtitles.parse(planmod.resolve(plan, au["subtitles"])))
    return None


def summary(c):
    fps = c["plan"]["output"]["fps"]
    shots = []
    for s, n, t in zip(c["plan"]["shots"], c["frames"], c["trans"]):
        if s.get("kind") == "web":
            shots.append({"id": s["id"], "kind": "web", "url": s["web"]["url"], "frames": n, "transition_frames": t,
                          "camera": [m["op"] for m in s["camera"]], "highlights": len(s.get("highlight", []))})
            continue
        cam = c["cams"][s["id"]]
        shots.append({"id": s["id"], "asset": s["asset"], "frames": n, "transition_frames": t,
                      "static": cam.static, "start_viewport_px": [round(v, 1) for v in cam.start],
                      "end_viewport_px": [round(v, 1) for v in cam.end],
                      "zoom_end": round((camera.cover_rect(cam.sw, cam.sh, cam.aspect)[2]
                                         - camera.cover_rect(cam.sw, cam.sh, cam.aspect)[0])
                                        / (cam.end[2] - cam.end[0]), 3)})
    return {"shots": shots, "visual_s": round(c["visual_f"] / fps, 3), "voice_s": round(c["voice_s"], 3),
            "adjustments": c["adjustments"]}


# ---------------- execution ----------------

def _segment(c, i, s, be, tmp):
    fps = c["plan"]["output"]["fps"]
    W, H = c["plan"]["output"]["width"], c["plan"]["output"]["height"]
    n = c["frames"][i]
    m = c["metas"][s["id"]]
    if s.get("kind") == "web":
        from . import web
        out = os.path.join(tmp, f"seg{i:03d}.mp4")
        info = web.render_shot(s, n, fps, W, H, c["starts"][i] / fps, c["voice_timeline"], be.seg_enc, out)
        c.setdefault("web_info", {})[s["id"]] = info
        return out
    pre, post = c["cams"][s["id"]].filter(W, H)
    src = m["path"]
    out = os.path.join(tmp, f"seg{i:03d}.mp4")
    if m["kind"] == "image":
        loop = f"loop=loop={n - 1}:size=1:start=0,settb=AVTB,setpts=N/({fps}*TB),fps={fps}"
        vf = f"{pre},{loop},{post}"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf]
    else:
        # must be an input-side seek: an output-side -ss is implemented as a trim at the end of the filter chain, and perspective's frame counter on would count from source frame 0,
        # so the later the in-point the more the camera move is misaligned. When transcoding, input-side seek is exact (accurate_seek is on by default).
        pad = c.get("pads", {}).get(s["id"])
        tail = f",tpad=stop_mode=clone:stop_duration={pad + 2 / fps:.4f}" if pad else ""
        vf = f"fps={fps},{pre},{post}{tail}"
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", f"{s['source_in']}", "-i", src, "-vf", vf]
    cmd += ["-frames:v", str(n), "-an"] + be.seg_enc + ["-r", str(fps), out]
    run(cmd, f"shot {s['id']}")
    return out


def _join(c, segs, be, tmp):
    fps = c["plan"]["output"]["fps"]
    if len(segs) == 1:
        return segs[0]
    inputs = []
    for sp in segs:
        inputs += ["-i", sp]
    # unify the timebase: the concat output and MP4 input differ in timebase, and xfade refuses when hard cuts and xfade are mixed
    fc = [f"[{i}:v]settb=AVTB,setpts=PTS-STARTPTS[n{i}]" for i in range(len(segs))]
    prev, cum = "n0", c["frames"][0]
    for i in range(1, len(segs)):
        t = c["trans"][i - 1]
        lbl = f"j{i}"
        if t:
            tr = c["plan"]["shots"][i - 1]["transition_out"]["type"]
            fc.append(f"[{prev}][n{i}]xfade=transition={tr}:duration={t / fps!r}:offset={(cum - t) / fps!r},"
                      f"settb=AVTB[{lbl}]")
        else:
            fc.append(f"[{prev}][n{i}]concat=n=2:v=1:a=0,settb=AVTB[{lbl}]")
        cum += c["frames"][i] - t
        prev = lbl
    out = os.path.join(tmp, "silent.mp4")
    run(["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex", ";".join(fc), "-map", f"[{prev}]"]
        + be.seg_enc + ["-r", str(fps), out], "concat/transitions")
    return out


def execute(c, out_path, be, keep_tmp=False, log=print):
    p = c["plan"]
    fps = p["output"]["fps"]
    W, H = p["output"]["width"], p["output"]["height"]
    au, look = p["audio"], p["look"]
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="reel_")
    ok = False
    try:
        segs = []
        for i, s in enumerate(p["shots"]):
            segs.append(_segment(c, i, s, be, tmp))
            log(f"  {s['id']} {c['frames'][i]}f " + ("web" if s.get("kind") == "web" else
                                                    ("static" if c["cams"][s["id"]].static else "camera")))
        silent = _join(c, segs, be, tmp)
        voice = planmod.resolve(p, au["voice"])
        events = None
        if au.get("subtitles"):
            events = subtitles.parse(planmod.resolve(p, au["subtitles"]))
            if be.subs_mode is None:
                raise ReelError("BACKEND_UNAVAILABLE", "no subtitle burn-in method available (ffmpeg has no ass filter and mpv is missing)")
        vchain = []
        if look.get("grade"):
            vchain.append(GRADE)
        if look.get("lut"):
            vchain.append(f"lut3d=file={_fesc(planmod.resolve(p, look['lut']))}:interp=tetrahedral")
        vchain.append("tpad=stop_mode=clone:stop_duration=0.5")    # pad the last frame to cover a quantisation difference of at most 1 frame
        burn_in_ffmpeg = events is not None and be.subs_mode == "ass"
        if burn_in_ffmpeg:
            ass = subtitles.write_ass(events, os.path.join(tmp, "subs.ass"), W, H, be.font, au["keywords"])
            vchain.append(f"ass=filename={_fesc(ass)}")
        vchain.append("format=yuv420p")
        inputs = ["-i", silent, "-i", voice]
        fc = [f"[0:v]{','.join(vchain)}[v]"]
        if au.get("bgm"):
            inputs += ["-stream_loop", "-1", "-i", planmod.resolve(p, au["bgm"])]
            fc.append(f"[2:a]volume={au['bgm_gain']}[b];[1:a][b]amix=inputs=2:duration=first:normalize=0[a]")
            amap = "[a]"
        else:
            amap = "1:a"
        dur = f"{c['voice_s']:.3f}"
        stage = out_path if (events is None or burn_in_ffmpeg) else os.path.join(tmp, "clean.mp4")
        run(["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex", ";".join(fc), "-map", "[v]",
             "-map", amap, "-t", dur] + be.final_enc + ["-r", str(fps), "-c:a", "aac", "-b:a", "192k",
                                                        "-movflags", "+faststart", stage], "audio mix / colour grade")
        if events is not None and not burn_in_ffmpeg:
            srt = subtitles.write_srt(events, os.path.join(tmp, "subs.srt"), au["keywords"])
            fs = max(24, round(42 * min(W, H) / 1080))   # mpv units are per 720 px of height: the short side keeps 16:9 as readable as 9:16
            r = subprocess.run(["mpv", stage, "--no-config", "--sub-file=" + srt, f"--sub-font={be.font}",
                                f"--sub-font-size={fs}", "--sub-bold=yes", "--sub-color=#FFFFFFFF",
                                "--sub-border-color=#FF000000", "--sub-border-size=3.2", "--sub-shadow-offset=0",
                                "--sub-pos=90", "--sub-margin-y=60", "--o=" + out_path, "--of=mp4",
                                "--ovc=libx264", "--ovcopts=preset=medium,crf=20", "--oac=aac",
                                "--oacopts=b=192k", "--vf=format=yuv420p", "--no-terminal"],
                               capture_output=True, text=True)
            if r.returncode != 0 or not os.path.isfile(out_path):
                raise ReelError("SUBTITLE_BURN_FAILED", "mpv subtitle burn-in failed", stderr=(r.stderr or r.stdout)[-1200:])
        res = verify.check(out_path, expect_s=c["voice_s"], fps=fps, width=W, height=H,
                           subs_cues=subtitles.cues(events) if events is not None else None)
        res.update({"backend": be.describe(), "adjustments": c["adjustments"],
                    "elapsed_s": round(time.time() - t0, 1)})
        ok = res["ok"]
        return res
    finally:
        if keep_tmp or not ok:
            log(f"[tmp] intermediate files kept in {tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


def render(plan, out_path, backend="cpu", observations=None, keep_tmp=False, log=print):
    c = compile_plan(plan, observations)
    be = make_backend(backend)
    log(f"[plan] {len(c['plan']['shots'])} shots | picture {c['visual_f'] / c['plan']['output']['fps']:.2f}s"
        f" | voice {c['voice_s']:.2f}s · backend={be.name}/{be.subs_mode}")
    return execute(c, out_path, be, keep_tmp, log)


def preview_shot(plan, shot_id, out_path, observations=None, backend="cpu"):
    """Render a single shot (no audio) so the decision layer can quickly see the camera move."""
    c = compile_plan(plan, observations)
    be = make_backend(backend)
    idx = next((i for i, s in enumerate(c["plan"]["shots"]) if s["id"] == shot_id), None)
    if idx is None:
        raise ReelError("SHOT_NOT_FOUND", f"no shot {shot_id}")
    tmp = tempfile.mkdtemp(prefix="reel_prev_")
    try:
        seg = _segment(c, idx, c["plan"]["shots"][idx], be, tmp)
        shutil.move(seg, out_path)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    fps = c["plan"]["output"]["fps"]
    W, H = c["plan"]["output"]["width"], c["plan"]["output"]["height"]
    return verify.check(out_path, expect_s=c["frames"][idx] / fps, fps=fps, width=W, height=H, expect_audio=False)


def preview_range(plan, a_s, b_s, out_path, size=None, out_fps=15, audio="mix", observations=None,
                  backend="cpu", log=print):
    """Local low-resolution preview: first compile the whole timeline at the plan's target fps and render only the shots that intersect [a_s, b_s) (whole shots are rendered,
    with full handles on both sides of a transition), then cut out the range and resample to out_fps; voice/BGM are mixed from the same times as the full video.
    The time axis matches the full video: preview frame k = full-video time fa/fps + k/out_fps. Subtitles are not burned in."""
    c = compile_plan(plan, observations)
    p = c["plan"]
    fps, W, H = p["output"]["fps"], p["output"]["width"], p["output"]["height"]
    fa = int(math.floor(a_s * fps + 1e-9))
    fb = min(int(math.ceil(b_s * fps - 1e-9)), c["visual_f"])
    if not 0 <= fa < fb:
        raise ReelError("ARG_INVALID", f"range [{a_s}, {b_s}) is not inside the picture [0, {c['visual_f'] / fps:.3f})")
    if size:
        pw, ph = size
    else:
        k = 960 / max(W, H)
        pw, ph = max(2, int(round(W * k / 2)) * 2), max(2, int(round(H * k / 2)) * 2)
    if pw % 2 or ph % 2 or abs(pw / ph - W / H) > 0.01:
        raise ReelError("ARG_INVALID", f"--size {pw}x{ph} must be even and match the output aspect {W}x{H} (the camera geometry is evaluated by ratio)")
    if audio not in ("mix", "voice", "none"):
        raise ReelError("ARG_INVALID", "--audio must be mix / voice / none")
    idx = [i for i in range(len(c["frames"])) if c["starts"][i] < fb and c["starts"][i] + c["frames"][i] > fa]
    shots = [json.loads(json.dumps(p["shots"][i])) for i in idx]
    shots[-1].pop("transition_out", None)
    sp = dict(p, output={"width": pw, "height": ph, "fps": fps}, shots=shots)
    sub = dict(c, plan=sp, frames=[c["frames"][i] for i in idx],
               trans=[c["trans"][i] for i in idx[:-1]] + [0], starts=[c["starts"][i] for i in idx])
    off = sub["starts"][0]
    be = make_backend(backend)
    tmp = tempfile.mkdtemp(prefix="reel_rng_")
    try:
        segs = [_segment(sub, k, s, be, tmp) for k, s in enumerate(shots)]
        silent = _join(sub, segs, be, tmp)
        au = p["audio"]
        dur = (fb - fa) / fps
        t0, t1 = fa / fps, fb / fps
        inputs = ["-i", silent]
        fc = [f"[0:v]trim=start_frame={fa - off}:end_frame={fb - off},setpts=PTS-STARTPTS,fps={out_fps},"
              f"format=yuv420p[v]"]
        maps = ["-map", "[v]"]
        if audio != "none":
            inputs += ["-i", planmod.resolve(p, au["voice"])]
            fc.append(f"[1:a]atrim=start={t0!r}:end={t1!r},asetpts=PTS-STARTPTS[va]")
            if audio == "mix" and au.get("bgm"):
                inputs += ["-stream_loop", "-1", "-i", planmod.resolve(p, au["bgm"])]
                fc.append(f"[2:a]atrim=start={t0!r}:end={t1!r},asetpts=PTS-STARTPTS,volume={au['bgm_gain']}[b];"
                          f"[va][b]amix=inputs=2:duration=first:normalize=0[a]")
            else:
                fc.append("[va]anull[a]")
            maps += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k"]
        run(["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex", ";".join(fc)] + maps
            + ["-t", f"{dur:.4f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
               "-r", str(out_fps), "-movflags", "+faststart", out_path], "range preview")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    res = verify.check(out_path, expect_s=dur, fps=out_fps, width=pw, height=ph, expect_audio=audio != "none")
    res.update({"range_f": [fa, fb], "range_s": [round(t0, 4), round(t1, 4)], "compile_fps": fps,
                "preview_fps": out_fps, "size": [pw, ph], "audio": audio if audio != "mix" or au.get("bgm")
                else "voice (plan has no bgm)", "shots": [s["id"] for s in shots],
                "rendered_f": [off, off + sum(sub["frames"]) - sum(sub["trans"])],
                "subtitles": "not_burned", "time_map": f"preview frame k = full-cut time {t0:.4f}s + k/{out_fps}"})
    return res
