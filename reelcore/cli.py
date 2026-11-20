"""LLM 可调用的工具层：每个子命令 = 一个工具。stdout 只输出一个 JSON；失败 exit 1 并给结构化错误。

决策流程（Claude 作为决策层）：
  inspect → frame(看带网格的帧) → observe / locate(登记目标) → 写 plan.json（zoom_to 引用 observation_id）
  → validate → preview <shot> → render --where auto
"""
import argparse, json, os, re, shutil, sys, tempfile
from . import manifest, perceive, plan as P, remote, render, verify
from .errors import ReelError


def _log(*a):
    print(*a, file=sys.stderr, flush=True)


def _floats(s, n=None):
    v = [float(x) for x in s.split(",") if x.strip()]
    if n and len(v) != n:
        raise ReelError("ARG_INVALID", f"需要 {n} 个逗号分隔的数字: {s}")
    return v


def _plan_and_obs(a):
    plan = P.load(a.plan)
    base = os.path.dirname(os.path.abspath(a.plan))
    if not os.path.isabs(plan.get("src_dir") or ""):
        plan["src_dir"] = os.path.normpath(os.path.join(base, plan.get("src_dir") or "."))
    obs_path = a.obs or os.path.join(base, "observations.json")
    return plan, perceive.load_obs(obs_path), obs_path


# ---------------- 工具 ----------------

def t_inspect(a):
    return {"assets": [manifest.probe_full(p) for p in a.paths]}


def t_frame(a):
    r = perceive.frame_grid(a.asset, a.t, a.out)
    if a.obs:
        boxes = [o for o in perceive.load_obs(a.obs) if os.path.basename(a.asset) == os.path.basename(o["asset"])]
        if boxes:
            r["boxes_png"] = perceive.draw_boxes(a.asset, a.t, boxes, a.out.replace(".png", "_boxes.png"))
    return r


def t_observe(a):
    path = a.asset if os.path.isabs(a.asset) else os.path.join(a.src, a.asset)
    return {"observation": perceive.observe(a.obs, a.asset, path, _floats(a.bbox, 4), a.label, a.t)}


def _detect_local(frames, queries, threshold):
    return perceive.detect_owlv2(frames, queries, threshold)


def _detect_remote(frames, queries, threshold, allow_over):
    info = remote.probe()
    if not info["ok"]:
        raise ReelError("REMOTE_OFFLINE", _offline_msg(info), tried=info["tried"])
    if not (info.get("vision") or "").endswith("True"):
        raise ReelError("BACKEND_UNAVAILABLE", "remote vision venv is not ready; run: reel remote setup-vision",
                        vision=info.get("vision"))
    gate = remote.gpu_gate(info, need_mib=2500, allow_over=allow_over)
    job = remote.new_job(info["host"], {**remote.code_files(), **{f"f/{k}.png": p for k, p in frames}})
    args = json.dumps({"frames": [[k, f"f/{k}.png"] for k, _ in frames], "queries": queries,
                       "threshold": threshold, "frac": 1.0 if allow_over else remote.DEFAULT_VRAM_FRAC})
    p = remote.run_in_job(info["host"], job, "-m reelcore _detect " + _shq(args), gpu=True, venv=True,
                          timeout=1800, need_mib=2500, allow_over=allow_over)
    res = _parse_json(p, "远程检测")
    return res["result"], {"host": info["host"], "job": job, "gate": gate}


def _offline_msg(info):
    if info.get("configured") is False:
        return "no remote host configured: set REEL_REMOTE=user@host (see README)"
    return "remote host offline"


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _parse_json(p, what):
    try:
        res = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ReelError("REMOTE_FAILED", f"{what}没有返回 JSON", stderr=p.stderr[-1500:], stdout=p.stdout[-500:])
    if not res.get("ok"):
        raise ReelError(res["error"]["code"], f"[remote] {res['error']['message']}", **res["error"].get("detail", {}))
    return res


def t_locate(a):
    path = a.asset if os.path.isabs(a.asset) else os.path.join(a.src, a.asset)
    queries = [q.strip() for q in a.query.split(",") if q.strip()]
    times = _floats(a.t) if a.t else [0.0]
    td = tempfile.mkdtemp(prefix="reel_loc_")
    try:
        frames = []
        for i, t in enumerate(times):
            png = os.path.join(td, f"t{i}.png")
            perceive.extract_frame(path, t, png)
            frames.append((f"t{i}", png))
        where = a.where
        remote_info = None
        if where == "auto":
            try:
                import torch  # noqa: F401
                where = "local"
            except ImportError:
                where = "remote"
        if where == "local":
            res = _detect_local(frames, queries, a.threshold)
        else:
            res, remote_info = _detect_remote(frames, queries, a.threshold, a.allow_over_80)
        items = []
        for i, t in enumerate(times):
            for d in res.get(f"t{i}", []):
                items.append({"asset": a.asset, "t": t, "label": d["label"], "bbox": d["bbox"], "conf": d["conf"],
                              "source": f"owlv2@{where}"})
        if not items:
            raise ReelError("TARGET_NOT_FOUND", f"没有检出 {queries}（阈值 {a.threshold}）；可降阈值、换描述，"
                                                "或用 frame + observe 人工登记", queries=queries)
        added = perceive.add_obs(a.obs, items)
        out = {"observations": added, "where": where, "remote": remote_info}
        if a.check_png:
            out["check_png"] = perceive.draw_boxes(path, times[0], [o for o in added if o["t"] == times[0]],
                                                   a.check_png)
        return out
    finally:
        shutil.rmtree(td, ignore_errors=True)


def t_words(a):
    """配音 → 逐字时间（whisper-cli，本机 Metal，免费）。结果写 JSON：[{t0,t1,text}]，plan 里 audio.words 引用它。"""
    import subprocess
    model = os.environ.get("REEL_WHISPER_MODEL", os.path.expanduser("~/models/whisper/ggml-large-v3-turbo.bin"))
    if not shutil.which("whisper-cli") or not os.path.isfile(model):
        raise ReelError("BACKEND_UNAVAILABLE", "需要 whisper-cli 和模型（REEL_WHISPER_MODEL）", model=model)
    td = tempfile.mkdtemp(prefix="reel_words_")
    try:
        wav = os.path.join(td, "a.wav")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", a.audio, "-ar", "16000", "-ac", "1", wav],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ReelError("FFMPEG_FAILED", "音频解码失败", stderr=r.stderr[-400:])
        r = subprocess.run(["whisper-cli", "-m", model, "-f", wav, "-l", a.lang, "-ml", "1", "-osrt", "-of",
                            os.path.join(td, "w")], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isfile(os.path.join(td, "w.srt")):
            raise ReelError("WHISPER_FAILED", "whisper 逐字转写失败", stderr=r.stderr[-600:])
        from . import subtitles
        ev = subtitles.parse(os.path.join(td, "w.srt"), allow_zero=True)
    finally:
        shutil.rmtree(td, ignore_errors=True)
    words = [{"t0": round(x, 3), "t1": round(y, 3), "text": t} for x, y, t in ev]
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(words, fh, ensure_ascii=False, indent=0)
    return {"words": a.out, "count": len(words), "text": "".join(w["text"] for w in words)[:200]}


def t_voice(a):
    from . import voicecheck
    return voicecheck.run(a.sample, a.script, a.voice_id, a.brief, a.lang)


def t_from_legacy(a):
    w, h, *f = [int(x) for x in a.size.split("x")]
    plan = P.from_legacy(a.txt, os.path.abspath(a.src), a.voice, a.subs, w, h, f[0] if f else 60, a.xfade,
                         a.bgm or None, a.grade, a.lut or None,
                         [k for k in (a.keywords or "").split(",") if k], a.policy)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False, indent=1)
    return {"plan": a.out, "shots": len(plan["shots"])}


def t_validate(a):
    plan, obs, _ = _plan_and_obs(a)
    P.validate(plan, obs)
    return {"valid": True, "shots": len(plan["shots"])}


def t_compile(a):
    plan, obs, _ = _plan_and_obs(a)
    return render.summary(render.compile_plan(plan, obs))


def _compiled(a):
    """plan → (compile 结果, 时间线)；revision 来自 .reel-history（无历史 = r0）。"""
    from . import edit, timeline
    plan, obs, _ = _plan_and_obs(a)
    c = render.compile_plan(plan, obs)
    rev = edit.state(a.plan)["revision"]
    return c, timeline.build(c, detail=getattr(a, "detail", "events"), revision=rev)


def t_timeline(a):
    from . import timeline, visual
    c, tl = _compiled(a)
    fa, fb = timeline.parse_range(a.range, tl["fps"], c["visual_f"])
    view = timeline.clip(tl, fa, fb) if a.range else tl
    beats = None
    if getattr(a, "beats", None):
        with open(a.beats, encoding="utf-8") as fh:
            beats = json.load(fh)
    if a.png:
        return visual.draw_timeline(c, view, a.png, fa, fb, a.width, a.render, beats=beats, beat_offset=a.beat_offset)
    if a.format == "text":
        return {"revision": tl["revision"], "text": timeline.text(view)}
    return view


def t_scrub(a):
    from . import visual
    c, tl = _compiled(a)
    at = _floats(a.at) if a.at else None
    picks = visual.picks_for(c, tl, at=at, rng=a.range, samples=a.samples, cuts=a.cuts)
    ov = [x for x in (a.overlays or "").split(",") if x]
    bad = [x for x in ov if x not in ("safe", "caption", "bbox")]
    if bad:
        raise ReelError("ARG_INVALID", f"未知 overlay {bad}（safe / caption / bbox）")
    return visual.scrub(c, tl, picks, a.out, a.render, ov)


def _obs_for(a):
    from . import edit
    return edit.default_obs(a.plan, a.obs)


def t_beats(a):
    """音乐 → 节拍/强拍/BPM（beat_this 优先，librosa 兜底），写 JSON。"""
    from . import beats
    r = beats.analyze(a.audio, a.backend)
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(r, fh, ensure_ascii=False)
    return {"out": a.out, "backend": r["backend"], "bpm": r["bpm"], "beats": len(r["beats"]),
            "downbeats": len(r["downbeats"]), "downbeat_method": r.get("downbeat_method"),
            "first_downbeats": [round(x, 3) for x in r["downbeats"][:4]],
            "downbeat_agreement": r.get("downbeat_agreement"),
            "needs_review": (r.get("downbeat_agreement") is not None and r["downbeat_agreement"] < 0.7)
                            or r["backend"] == "librosa" and r.get("fallback_reason") is not None}


def t_snap(a):
    """把镜头切点吸附到拍/强拍；通过 edit 写入（有 revision，可 undo）。"""
    from . import beats, edit
    with open(a.beats, encoding="utf-8") as fh:
        info = json.load(fh)
    raw = P.load(a.plan)
    obs = _obs_for(a)
    snapped, rep = beats.snap_plan(edit.resolved(raw, a.plan), info, a.mode, a.tolerance, a.min_gap, a.offset,
                                   observations=obs)
    ops = [{"op": "trim", "id": s0["id"], "duration": s1["duration"]}
           for s0, s1 in zip(raw["shots"], snapped["shots"]) if abs(s0["duration"] - s1["duration"]) > 1e-6]
    if not ops:
        return {"report": rep, "note": "没有切点需要移动"}
    res = edit.edit(a.plan, ops, base=a.base, dry_run=a.dry_run, obs=obs)
    res["report"] = rep
    return res


def t_mlt(a):
    """读 Shotcut/MLT 工程：文字时间线 + 同一视频素材的重复使用。只读。"""
    from . import mlt
    tl = mlt.load(a.project)
    fps = tl["fps"]
    fa, fb = 0, tl["frames"]
    if a.range:
        x, y = a.range.split(":")
        fa, fb = int(float(x) * fps), int(float(y) * fps)
    out = {"file": tl["file"], "fps": fps, "seconds": tl["seconds"], "tracks": len(tl["tracks"])}
    if a.format == "text":
        out["text"] = mlt.text(tl, fa, fb, show_audio=not a.video_only)
    else:
        out["timeline"] = tl
    if a.dupes:
        out["duplicates"] = mlt.duplicates(tl, a.min_overlap)
    return out


def t_edit(a):
    from . import edit
    src = a.op
    if src.startswith("@"):
        with open(src[1:], encoding="utf-8") as fh:
            src = fh.read()
    try:
        ops = json.loads(src)
    except json.JSONDecodeError as e:
        raise ReelError("OP_INVALID", f"--op 不是合法 JSON: {e}", committed=False)
    return edit.edit(a.plan, ops, base=a.base, dry_run=a.dry_run, obs=_obs_for(a))


def t_undo(a):
    from . import edit
    return edit.undo(a.plan, a.to, base=a.base, dry_run=a.dry_run, obs=_obs_for(a))


def t_history(a):
    from . import edit
    return edit.history(a.plan)


def _compiled_plan(a):
    if not a.plan:
        return None
    plan, obs, _ = _plan_and_obs(a)
    return render.compile_plan(plan, obs)


def t_check(a):
    from . import gate
    from .errors import GateFailed
    r, _ = gate.check_video(a.video, _compiled_plan(a), a.profile, a.brief, a.subs, a.text, a.reviews, a.contract)
    if a.report:
        with open(a.report, "w", encoding="utf-8") as fh:
            json.dump(r, fh, ensure_ascii=False, indent=1, default=float)  # numpy scalars must never truncate a report
        r["report"] = a.report
    g = r["gate"]
    if g["status"] == "fail":
        raise GateFailed("CHECK_FAILED", f"检查不通过：{len(g['blockers'])} 项阻断", r,
                         blockers=[b["check"] for b in g["blockers"]])
    if g["status"] == "needs_review":
        raise GateFailed("CHECK_NEEDS_REVIEW", "有未处理的 REVIEW：逐项看过后写进 --reviews 记录", r,
                         pending=g["pending_review"])
    return r


def t_deliver(a):
    from . import deliver
    return deliver.deliver(a.video, a.brief, a.version, _compiled_plan(a), a.plan, a.profile, a.reviews, a.subs, a.text,
                           a.sample, a.contract)


def t_lyrics(a):
    from . import lyrics
    if a.action == "import":
        if not (a.song and a.lyrics and a.out):
            raise ReelError("ARG_INVALID", "lyrics import 需要 SONG --lyrics 歌词原件 --out lyrics.json")
        return lyrics.import_(a.song, a.lyrics, a.out, a.srt, a.words, a.display, a.stream, a.end_fixes)
    return lyrics.verify(a.song, None, a.lyrics)  # verify: 位置参数是 lyrics.json


def t_register(a):
    from . import deliver
    return deliver.register(a.brief, remove=a.remove)


def t_receipt(a):
    from . import deliver
    r = deliver.verify_receipt(a.receipt)
    if not r["ok"]:
        raise ReelError("RECEIPT_INVALID", r["why"], **{k: v for k, v in r.items() if k not in ("ok", "why")})
    return r


def t_preview(a):
    plan, obs, _ = _plan_and_obs(a)
    if a.range:
        lo, _, hi = a.range.partition(":")
        size = tuple(int(x) for x in a.size.lower().split("x")) if a.size else None
        if size is not None and len(size) != 2:
            raise ReelError("ARG_INVALID", f"--size 需要 WxH: {a.size}")
        r = render.preview_range(plan, float(lo or 0), float(hi) if hi.strip() else float("inf"), a.out, size,
                                 a.fps, a.audio, obs, log=_log)
    elif a.shot:
        r = render.preview_shot(plan, a.shot, a.out, obs)
    else:
        raise ReelError("ARG_INVALID", "需要 shot（单镜头无声预览）或 --range A:B（区间带声预览）")
    if a.sheet:
        r["sheet"] = _sheet(a.out, a.sheet)
    return r


def _sheet(mp4, png, n=5):
    """首/中/末等距 n 帧拼成一张图，供目检运镜。"""
    import subprocess
    raw = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          "stream=nb_read_frames", "-of", "csv=p=0", mp4], capture_output=True, text=True).stdout
    m = re.search(r"\d+", raw or "")          # 有的文件会输出 "180," 这类带尾逗号的格式
    nf = int(m.group(0)) if m else 0
    if nf < 1:
        raise ReelError("FFMPEG_FAILED", "读不到视频帧数，无法生成拼图", out=raw[-200:])
    idx = sorted({round(i * (nf - 1) / (n - 1)) for i in range(n)})
    sel = "+".join(f"eq(n\\,{i})" for i in idx)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", mp4, "-vf", f"select='{sel}',scale=270:-2,tile={len(idx)}x1",
                    "-fps_mode", "vfr", "-frames:v", "1", png], check=True)
    return {"png": png, "frames": idx}


def _render_local(plan, obs, out, backend, keep):
    return render.render(plan, out, backend, obs, keep, log=_log)


def _prerender_web(plan, obs, td):
    """The remote host has no browser: web shots are pre-rendered locally into video clips (camera and highlight baked in)."""
    if not any(s.get("kind") == "web" for s in plan["shots"]):
        return plan
    c = render.compile_plan(json.loads(json.dumps(plan)), obs)
    be = render.make_backend("cpu")
    rp = json.loads(json.dumps(plan))
    for i, s in enumerate(c["plan"]["shots"]):
        if s.get("kind") != "web":
            continue
        _log(f"[web] {s['id']} pre-rendered locally ({c['frames'][i]} frames)")
        seg = render._segment(c, i, s, be, td)
        rs = rp["shots"][i]
        for k in ("kind", "web", "highlight"):
            rs.pop(k, None)
        rs.update(asset=os.path.abspath(seg), source_in=0, duration=c["frames"][i] / c["plan"]["output"]["fps"],
                  camera=[], viewport=None, space="image")
    return rp


def _render_remote(plan, obs, out, allow_over, keep):
    info = remote.probe()
    if not info["ok"]:
        raise ReelError("REMOTE_OFFLINE", _offline_msg(info), tried=info["tried"])
    if not (info["nvenc"] and info["libass"]):
        raise ReelError("BACKEND_UNAVAILABLE", "remote ffmpeg lacks NVENC or libass", **info)
    gate = remote.gpu_gate(info, need_mib=800, allow_over=allow_over)
    webtmp = tempfile.mkdtemp(prefix="reel_web_")
    plan = _prerender_web(plan, obs, webtmp)
    # 素材按引用收集，重写成 job 内相对路径
    files, rp, keys = {}, json.loads(json.dumps(plan)), {}
    def take(rel):
        src = os.path.realpath(P.resolve(plan, rel))
        if not os.path.isfile(src):
            raise ReelError("ASSET_NOT_FOUND", f"素材不存在: {src}")
        if src not in keys:                      # 同一文件只传一次、只有一个 job 路径（observation 映射依赖这点）
            keys[src] = f"a/{len(keys):03d}_{os.path.basename(src)}"
            files[keys[src]] = src
        return keys[src]
    for s in rp["shots"]:
        s["asset"] = take(s["asset"])
    for k in ("voice", "subtitles", "bgm"):
        if rp["audio"].get(k):
            rp["audio"][k] = take(rp["audio"][k])
    if rp["look"].get("lut"):
        rp["look"]["lut"] = take(rp["look"]["lut"])
    rp["src_dir"] = "."
    td = tempfile.mkdtemp(prefix="reel_job_")
    try:
        pj = os.path.join(td, "plan.json")
        with open(pj, "w", encoding="utf-8") as fh:
            json.dump(rp, fh, ensure_ascii=False)
        oj = os.path.join(td, "observations.json")
        # observation 的 asset 字段按新路径改写，校验仍能对上
        amap = {s0["asset"]: s1["asset"] for s0, s1 in zip(plan["shots"], rp["shots"])}
        with open(oj, "w", encoding="utf-8") as fh:
            json.dump([{**o, "asset": amap.get(o.get("asset"), o.get("asset"))} for o in obs], fh, ensure_ascii=False)
        files["plan.json"], files["observations.json"] = pj, oj
        files.update(remote.code_files())
        job = remote.new_job(info["host"], files)
        _log(f"[remote] {info['host']} job={job} gate={gate}")
        p = remote.run_in_job(info["host"], job, "-m reelcore _render plan.json --out out.mp4 --backend nvenc"
                              + (" --keep-tmp" if keep else ""), gpu=True, venv=False, timeout=3600,
                              need_mib=800, allow_over=allow_over)
        for ln in p.stderr.splitlines():
            _log("  [remote] " + ln)
        res = _parse_json(p, "远程渲染")
        remote.fetch(info["host"], f"{job}/out.mp4", out)
    finally:
        shutil.rmtree(td, ignore_errors=True)
        shutil.rmtree(webtmp, ignore_errors=True)
    # 不信远程的 ok：拉回后在本机重新验收
    o = plan["output"]
    cues = None
    if plan["audio"].get("subtitles"):
        from . import subtitles
        cues = subtitles.cues(subtitles.parse(P.resolve(plan, plan["audio"]["subtitles"])))
    local = verify.check(out, expect_s=res["result"]["checks_expect_s"], fps=o["fps"], width=o["width"],
                         height=o["height"], subs_cues=cues)
    return {**local, "backend": res["result"]["backend"], "remote": {"host": info["host"], "job": job,
                                                                     "elapsed_s": res["result"]["elapsed_s"]},
            "adjustments": res["result"]["adjustments"]}


def t_render(a):
    plan, obs, _ = _plan_and_obs(a)
    plan = P.validate(plan, obs)                                 # 规范化（补默认值），本地/远程两条路径用同一份
    render.compile_plan(json.loads(json.dumps(plan)), obs)       # compile locally first: errors surface before any remote work
    where = a.where
    note = None
    if where == "auto":
        info = remote.probe()
        if info["ok"] and info.get("nvenc") and info.get("libass"):
            try:
                remote.gpu_gate(info, 800)
                where = "remote"
            except ReelError as e:
                where, note = "local", f"remote not used: {e.code}"
        elif info.get("configured") is False:
            where = "local"
        else:
            where, note = "local", "remote offline, rendering locally"
        _log(f"[auto] → {where}" + (f"（{note}）" if note else ""))
    if where == "remote":
        r = _render_remote(plan, obs, a.out, a.allow_over_80, a.keep_tmp)
    else:
        r = _render_local(plan, obs, a.out, "cpu", a.keep_tmp)
    r["where"], r["note"] = where, note
    if a.sheet:
        r["sheet"] = _sheet(a.out, a.sheet, 6)
    if not r["ok"]:
        raise ReelError("ACCEPTANCE_FAILED", "成片验收未通过", **r)
    return r


def t_remote(a):
    if a.action == "probe":
        return remote.probe()
    info = remote.probe()
    if not info["ok"]:
        raise ReelError("REMOTE_OFFLINE", _offline_msg(info), tried=info["tried"])
    return {"host": info["host"], "setup": remote.setup_vision(info["host"])}


# ---------------- internal commands run on the remote host ----------------

def t__render(a):
    plan = P.load(a.plan)
    plan["src_dir"] = os.path.dirname(os.path.abspath(a.plan))
    obs = perceive.load_obs(os.path.join(plan["src_dir"], "observations.json"))
    c = render.compile_plan(plan, obs)
    be = render.make_backend(a.backend)
    r = render.execute(c, a.out, be, a.keep_tmp, log=_log)
    r["checks_expect_s"] = c["voice_s"]
    if not r["ok"]:
        raise ReelError("ACCEPTANCE_FAILED", "远程成片验收未通过", **r)
    return r


def t__detect(a):
    spec = json.loads(a.spec)
    return perceive.detect_owlv2([tuple(x) for x in spec["frames"]], spec["queries"], spec["threshold"],
                                 max_vram_frac=spec.get("frac", 0.8))


# ---------------- 入口 ----------------

def build_parser():
    ap = argparse.ArgumentParser(prog="reel", description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)

    x = sp.add_parser("inspect", help="素材清单（尺寸/时长/旋转/类型/视频切镜点）"); x.add_argument("paths", nargs="+")
    x.set_defaults(fn=t_inspect)
    x = sp.add_parser("frame", help="抽帧+坐标网格，给 LLM 读 bbox")
    x.add_argument("asset"); x.add_argument("--t", type=float, default=0.0); x.add_argument("--out", required=True)
    x.add_argument("--obs", help="同时把已有 observation 画上去"); x.set_defaults(fn=t_frame)
    x = sp.add_parser("observe", help="登记 LLM 看图得到的 bbox")
    x.add_argument("--obs", required=True); x.add_argument("--src", default="."); x.add_argument("--asset", required=True)
    x.add_argument("--bbox", required=True, help="x0,y0,x1,y1 归一化"); x.add_argument("--label", required=True)
    x.add_argument("--t", type=float, default=0.0); x.set_defaults(fn=t_observe)
    x = sp.add_parser("locate", help="OWLv2 按文字找目标（本机有 torch 则本机，否则 REEL_REMOTE 远程主机）")
    x.add_argument("--obs", required=True); x.add_argument("--src", default="."); x.add_argument("--asset", required=True)
    x.add_argument("--query", required=True, help="逗号分隔，英文描述效果最好")
    x.add_argument("--t", default="", help="逗号分隔的时间点（视频）"); x.add_argument("--threshold", type=float, default=0.15)
    x.add_argument("--where", choices=["auto", "local", "remote"], default="auto")
    x.add_argument("--allow-over-80", action="store_true", help="已告知用户并获同意后才可用")
    x.add_argument("--check-png", help="把检出框画回帧上供核对"); x.set_defaults(fn=t_locate)
    x = sp.add_parser("words", help="配音 → 逐字时间 JSON（whisper，本机）；网页高亮跟随语速用")
    x.add_argument("audio"); x.add_argument("--out", required=True); x.add_argument("--lang", default="zh")
    x.set_defaults(fn=t_words)
    x = sp.add_parser("voice", help="声音试听的 agent 签法：whisper 转写样本，和试听文本比错误率（中文按拼音），过了写进 brief.voice.approved")
    x.add_argument("action", choices=["check"]); x.add_argument("sample"); x.add_argument("--script", required=True,
                   help="试听样本念的原文（至少 20 字）"); x.add_argument("--voice-id", dest="voice_id")
    x.add_argument("--brief", help="通过后把 voice.approved（by=claude + stt 证据）写进这个 brief")
    x.add_argument("--lang", default="zh"); x.set_defaults(fn=t_voice)
    x = sp.add_parser("from-legacy", help="旧文本 plan → plan.json")
    x.add_argument("txt"); x.add_argument("--src", required=True); x.add_argument("--voice", required=True)
    x.add_argument("--subs"); x.add_argument("--xfade", type=float, default=0.0); x.add_argument("--bgm")
    x.add_argument("--grade", action="store_true"); x.add_argument("--lut"); x.add_argument("--keywords")
    x.add_argument("--size", default="1080x1920x60")
    x.add_argument("--policy", choices=P.POLICIES, default="fit_last_shot"); x.add_argument("--out", required=True)
    x.set_defaults(fn=t_from_legacy)
    for name, fn, help_ in [("validate", t_validate, "只校验 plan"), ("compile", t_compile, "校验+帧级时间线摘要（不渲染）")]:
        x = sp.add_parser(name, help=help_); x.add_argument("plan"); x.add_argument("--obs"); x.set_defaults(fn=fn)
    x = sp.add_parser("preview", help="只渲一个镜头（无声）看运镜；或 --range 区间带声低清预览")
    x.add_argument("plan"); x.add_argument("shot", nargs="?"); x.add_argument("--out", required=True)
    x.add_argument("--obs"); x.add_argument("--range", help="A:B 秒：区间预览（时间轴与全片一致）")
    x.add_argument("--size", help="区间预览尺寸 WxH（默认长边 960）"); x.add_argument("--fps", type=int, default=15)
    x.add_argument("--audio", choices=["mix", "voice", "none"], default="mix")
    x.add_argument("--sheet", help="输出等距抽帧拼图 png"); x.set_defaults(fn=t_preview)
    x = sp.add_parser("timeline", help="编译后的多轨时间线（V1/C1/A1/BGM，半开帧区间）；--png 出多轨图")
    x.add_argument("plan"); x.add_argument("--obs"); x.add_argument("--range", help="A:B 秒")
    x.add_argument("--format", choices=["json", "text"], default="json")
    x.add_argument("--detail", choices=["summary", "events", "words"], default="events")
    x.add_argument("--png", help="输出多轨 PNG（同名 .json sidecar）"); x.add_argument("--width", type=int, default=1600)
    x.add_argument("--render", help="缩略图取自这个成片（否则取素材首帧）")
    x.add_argument("--beats", help="reel beats 输出的 JSON：多轨图加节拍轨并标出每个切点离拍多少 ms")
    x.add_argument("--beat-offset", type=float, default=0.0, help="音乐在时间线上的起点（秒）"); x.set_defaults(fn=t_timeline)
    x = sp.add_parser("scrub", help="按时间点/区间/切点抽帧：干净帧 + 标注帧 + index.json")
    x.add_argument("plan"); x.add_argument("--obs"); x.add_argument("--at", help="逗号分隔的秒数")
    x.add_argument("--range", help="A:B 秒"); x.add_argument("--samples", type=int, default=6)
    x.add_argument("--cuts", action="store_true", help="每个切点取前一帧/切点帧/后一帧（转场另取中点、终点）")
    x.add_argument("--render", help="从成片取帧（否则按编译取景从素材合成）")
    x.add_argument("--overlays", default="", help="safe,caption,bbox"); x.add_argument("--out", required=True)
    x.set_defaults(fn=t_scrub)
    x = sp.add_parser("beats", help="音乐节拍分析：拍/强拍/BPM（卡点用）")
    x.add_argument("audio"); x.add_argument("--out", required=True)
    x.add_argument("--backend", choices=["auto", "beat_this", "librosa"], default="auto"); x.set_defaults(fn=t_beats)
    x = sp.add_parser("snap", help="把镜头切点吸附到拍/强拍（经 edit 写入，可 undo）")
    x.add_argument("plan"); x.add_argument("--beats", required=True)
    x.add_argument("--mode", default="downbeat", help="beat / downbeat / every:N / half")
    x.add_argument("--tolerance", type=float, default=0.30, help="切点最多移动多少秒")
    x.add_argument("--min-gap", type=float, default=0.35, help="最短镜头秒数")
    x.add_argument("--offset", type=float, default=0.0, help="音乐在时间线上的起点（秒）")
    x.add_argument("--obs"); x.add_argument("--base"); x.add_argument("--dry-run", action="store_true")
    x.set_defaults(fn=t_snap)
    x = sp.add_parser("mlt", help="读 Shotcut/MLT 工程文件：文字时间线（轨道/片段/源入出点/滤镜）与重复用料检查")
    x.add_argument("project"); x.add_argument("--format", choices=["text", "json"], default="text")
    x.add_argument("--range", help="A:B 秒"); x.add_argument("--video-only", action="store_true")
    x.add_argument("--dupes", action="store_true", help="同一视频素材源区间重叠 ≥ --min-overlap 秒即报告")
    x.add_argument("--min-overlap", type=float, default=0.3); x.set_defaults(fn=t_mlt)
    x = sp.add_parser("edit", help="小粒度编辑（trim/move/insert/replace/set-transition/set-camera），校验+编译通过才原子写入")
    x.add_argument("plan"); x.add_argument("--op", required=True, help="操作 JSON（对象或数组=原子批量），或 @文件")
    x.add_argument("--base", help="期望的当前 revision；不一致 → REV_CONFLICT")
    x.add_argument("--dry-run", action="store_true"); x.add_argument("--obs"); x.set_defaults(fn=t_edit)
    x = sp.add_parser("undo", help="恢复到某个 revision（提交一个新 revision，历史不删）")
    x.add_argument("plan"); x.add_argument("--to", required=True); x.add_argument("--base")
    x.add_argument("--dry-run", action="store_true"); x.add_argument("--obs"); x.set_defaults(fn=t_undo)
    x = sp.add_parser("history", help="revision 历史"); x.add_argument("plan"); x.set_defaults(fn=t_history)
    gate_args = lambda x: (x.add_argument("--plan"), x.add_argument("--obs"),
                           x.add_argument("--profile", help="内置 profile 名（default / vertical-strict）或项目 profile JSON 路径"),
                           x.add_argument("--reviews", help="审片记录 JSON（逐项处理 needs_review；绑定成片 sha256）"),
                           x.add_argument("--subs", action="append", default=[], help="另外要扫描的字幕文件（可重复）"),
                           x.add_argument("--text", action="append", default=[], help="另外要扫描的文本文件（可重复）"),
                           x.add_argument("--contract", help="MV 验收契约 reel.contract.v1（不依赖 plan；必查项 profile 去不掉）"))
    x = sp.add_parser("check", help="交付闸门检查：技术检测 + 文本禁用词 + 审片记录；不放行时 ok:false、退出码 1")
    x.add_argument("video"); gate_args(x); x.add_argument("--brief", help="brief.json（同时扫描其中文字）")
    x.add_argument("--report", help="报告另存为 JSON"); x.set_defaults(fn=t_check)
    x = sp.add_parser("deliver", help="受控交付：重新检查 → 成片/待审样片 → 复制并校验 hash → 写回执")
    x.add_argument("video"); x.add_argument("--brief", required=True); x.add_argument("--version", required=True)
    gate_args(x); x.add_argument("--sample", action="store_true", help="登记为待审样片（文件名带 sample-pending-review）")
    x.set_defaults(fn=t_deliver)
    x = sp.add_parser("lyrics", help="歌词时间源：import（内嵌歌词流/外部 srt → 校验 → lyrics.json）/ verify")
    x.add_argument("action", choices=["import", "verify"])
    x.add_argument("song", help="import：歌曲文件；verify：lyrics.json")
    x.add_argument("--lyrics", help="歌词原件（.md 取 ## Lyrics 一节，或纯文本）")
    x.add_argument("--out"); x.add_argument("--srt", help="外部逐行时间轴（歌曲没有内嵌歌词流时）")
    x.add_argument("--words", help="whisper 逐字结果（reel words 输出），只做交叉校验")
    x.add_argument("--display", choices=["sung", "lyrics"], default="sung", help="唱的和原件不同时，画面显示哪个（默认唱的）")
    x.add_argument("--stream", type=int, default=0, help="第几个内嵌字幕流")
    x.add_argument("--end-fixes", dest="end_fixes", help="行尾更正 JSON [{cue, text, t1, evidence}]：歌词流行尾跨过器乐时用，只许提前、必须写证据")
    x.set_defaults(fn=t_lyrics)
    x = sp.add_parser("register", help="登记任务：校验 brief，把交付目录交给 Bash guard 守")
    x.add_argument("brief"); x.add_argument("--remove", action="store_true"); x.set_defaults(fn=t_register)
    x = sp.add_parser("receipt", help="核对交付回执：交付文件还在、hash 没变")
    x.add_argument("receipt"); x.set_defaults(fn=t_receipt)
    x = sp.add_parser("render", help="出成片并验收")
    x.add_argument("plan"); x.add_argument("--out", required=True); x.add_argument("--obs")
    x.add_argument("--where", choices=["auto", "local", "remote"], default="auto")
    x.add_argument("--allow-over-80", action="store_true", help="已告知用户并获同意后才可用")
    x.add_argument("--keep-tmp", action="store_true"); x.add_argument("--sheet", help="成片抽帧拼图 png")
    x.set_defaults(fn=t_render)
    x = sp.add_parser("remote", help="远程 GPU 主机（REEL_REMOTE）：probe / setup-vision")
    x.add_argument("action", choices=["probe", "setup-vision"]); x.set_defaults(fn=t_remote)
    x = sp.add_parser("_render"); x.add_argument("plan"); x.add_argument("--out", required=True)
    x.add_argument("--backend", default="nvenc"); x.add_argument("--keep-tmp", action="store_true")
    x.set_defaults(fn=t__render)
    x = sp.add_parser("_detect"); x.add_argument("spec"); x.set_defaults(fn=t__detect)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    try:
        res = a.fn(a)
        print(json.dumps({"ok": True, "result": res}, ensure_ascii=False, default=str))
        return 0
    except ReelError as e:
        out = {"ok": False, "error": e.to_dict()}
        if getattr(e, "result", None) is not None:
            out["result"] = e.result
        print(json.dumps(out, ensure_ascii=False, default=str))
        return 1
    except Exception as e:  # noqa: BLE001  未预期的异常也要给调用方一个 JSON，而不是半截 traceback
        import traceback
        print(json.dumps({"ok": False, "error": {"code": "INTERNAL_ERROR", "message": f"{type(e).__name__}: {e}",
                                                 "detail": {"traceback": traceback.format_exc()[-1500:]}}},
                         ensure_ascii=False, default=str))
        return 1


if __name__ == "__main__":
    sys.exit(main())
