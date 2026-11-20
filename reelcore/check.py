"""成片自检：pass / fail / needs_review / not_checked 四态，每项附测量值、规则与证据时间点。

没实现的检查一律 not_checked，不显示 pass。阈值来自 profile（profiles/*.json），每项都写明 rule。
检测本身出错（缺依赖、ffmpeg 非零退出、输出解析不了、没跑到片尾）→ 该项 not_checked 并带 error，
由 gate.evaluate 计入阻断；这里只产出报告，「能不能交付」由 gate 决定。
"""
import hashlib, math, os, re, shutil, subprocess, json
from . import subtitles, verify, plan as planmod, textscan
from .errors import ReelError

BLACK_PIX_TH = 0.10          # blackdetect 像素阈值
BLACK_PIC_TH = 0.98          # 画面中黑像素比例
FREEZE_NOISE = "-60dB"
FREEZE_MIN_S = 0.5
CLIP_LEVEL = 0.99            # |sample| ≥ 此值视为触顶（AAC 解码后削波的平顶会有振铃，0.999 太严）
CLIP_MIN_SAMPLES = 10        # 削波样本数 ≥ 此值判 fail（约 0.2ms@48k；单个峰值触顶不算）

NOT_IMPLEMENTED = [
    ("caption_legibility", "字幕实际排版 bbox / 安全区 / 可读性：需要排版引擎 sidecar，当前只核对了时间"),
    ("pin_occlusion", "图钉/照片卡遮挡：v1 plan 没有图层数据"),
    ("voice_visual_alignment", "口播锚点与画面出现时刻对齐：v1 plan 没有词锚点"),
    ("content_correspondence", "地点/素材内容与口播对应：需要素材绑定数据与关键帧复核"),
    ("ocr_visible_text", "画面里的字（PNG 字卡、录屏、成片）没有 OCR：文本扫描只覆盖输入文本，不能称「成片无占位符」"),
]


def _item(name, status, rule, measured=None, evidence=None, note=None, **extra):
    return {"check": name, "status": status, "rule": rule, "measured": measured or {}, "evidence": evidence or [],
            **({"note": note} if note else {}), **extra}


def need_tools():
    miss = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if miss:
        raise ReelError("DEPENDENCY_MISSING", "缺少依赖，无法检查", missing=miss)


def _ff(args):
    """一次 ffmpeg 分析：非零退出 → FFMPEG_FAILED；没报告处理到片尾 → OUTPUT_UNPARSABLE。返回 stderr。"""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-progress", "pipe:1"] + args + ["-f", "null", os.devnull],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "检测进程失败", cmd=" ".join(args[:8]), rc=p.returncode,
                        stderr=p.stderr[-600:])
    if "progress=end" not in p.stdout:
        raise ReelError("OUTPUT_UNPARSABLE", "ffmpeg 没有报告处理到片尾，无法确认整片都被检测",
                        cmd=" ".join(args[:8]), stdout=p.stdout[-300:])
    return p.stderr


def _events(err, token, pattern):
    """事件行逐条解析；出现 token 的行数与解析出的条数不一致 → 输出格式变了，不能当作「没有事件」。"""
    found = re.findall(pattern, err)
    n = err.count(token)
    if n != len(found):
        i = err.find(token)
        raise ReelError("OUTPUT_UNPARSABLE", f"{token} 事件解析不了", lines=n, parsed=len(found),
                        sample=err[max(0, i - 40): i + 80] if i >= 0 else "")
    return found


def _probe(video):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", video],
                       capture_output=True, text=True)
    try:
        info = json.loads(p.stdout) if p.returncode == 0 else None
        dur = float(info["format"]["duration"])
    except (ValueError, TypeError, KeyError):
        raise ReelError("ASSET_UNREADABLE", "无法读取成片", path=video, stderr=p.stderr[-300:])
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


# ---------------- 各项 ----------------

def decode_clean(video):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-f", "null", os.devnull], capture_output=True, text=True)
    ok = p.returncode == 0 and not p.stderr.strip()
    return _item("decode_clean", "pass" if ok else "fail", "整片解码无错误（ffmpeg -v error 无输出且退出 0）",
                 {"rc": p.returncode}, [] if ok else [{"stderr": p.stderr.strip()[:300], "offending": True}])


def fmt(info, prof):
    f = prof["format"]
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    bad, m = [], {}
    if v is None:
        bad.append("没有视频流")
    else:
        w, h = v.get("width"), v.get("height")
        rot = next((int(sd.get("rotation", 0)) for sd in v.get("side_data_list", []) if "rotation" in sd), 0)
        if abs(rot) == 90:
            w, h = h, w
        m.update(video_codec=v.get("codec_name"), pix_fmt=v.get("pix_fmt"), width=w, height=h, fps=round(_fps(v), 3))
        if f.get("video_codec") and v.get("codec_name") not in f["video_codec"]:
            bad.append(f"视频编码 {v.get('codec_name')}，要求 {f['video_codec']}")
        if f.get("pix_fmt") and v.get("pix_fmt") not in f["pix_fmt"]:
            bad.append(f"像素格式 {v.get('pix_fmt')}，要求 {f['pix_fmt']}")
        if f.get("width") and (w, h) != (f["width"], f["height"]):
            bad.append(f"画幅 {w}×{h}，要求 {f['width']}×{f['height']}")
        if f.get("fps") and abs(_fps(v) - f["fps"]) >= 0.05:
            bad.append(f"帧率 {_fps(v):.3f}，要求 {f['fps']}")
    if a is None:
        if prof["audio"] == "required":
            bad.append("没有音轨")
    else:
        m.update(audio_codec=a.get("codec_name"), sample_rate=int(a.get("sample_rate") or 0), channels=a.get("channels"))
        if f.get("audio_codec") and a.get("codec_name") not in f["audio_codec"]:
            bad.append(f"音频编码 {a.get('codec_name')}，要求 {f['audio_codec']}")
        if f.get("sample_rate") and int(a.get("sample_rate") or 0) not in f["sample_rate"]:
            bad.append(f"采样率 {a.get('sample_rate')}，要求 {f['sample_rate']}")
    rule = f"profile.format：{ {k: x for k, x in f.items() if x} }；audio={prof['audio']}"
    return _item("format", "fail" if bad else "pass", rule, m, [{"why": b, "offending": True} for b in bad])


def first_frame(video, fps, prof):
    """直接解第 0 个显示帧，不靠 blackdetect 的最短时长（单个黑帧 d=0.1 抓不到）。"""
    c = prof["first_frame"]
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-frames:v", "1", "-vf", "scale=96:-2,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "第 0 帧解码失败", stderr=p.stderr.decode(errors="replace")[-300:])
    b = p.stdout
    if not b:
        raise ReelError("OUTPUT_UNPARSABLE", "第 0 帧没有解出像素")
    th = 16 + c["pix_th"] * 219                 # limited range 的黑电平 16，白 235；全范围的黑（0）同样落在阈值下
    ratio = sum(1 for x in b if x <= th) / len(b)
    black = ratio >= c["black_ratio"]
    return _item("first_frame", "fail" if black else "pass",
                 f"第 0 帧亮度 ≤{th:.0f} 的像素占比 ≥{c['black_ratio']} → 黑屏 fail（第 0 帧必须有画面）",
                 {"dark_ratio": round(ratio, 4)},
                 [{"t": [0.0, round(1 / fps, 4) if fps else 0.0], "frames": [0, 1], "offending": True}] if black else [])


def black(video, dur, fps, c=None):
    err = _ff(["-i", video, "-an", "-vf", f"blackdetect=d={0.5 / fps:.4f}:pix_th={BLACK_PIX_TH}:pic_th={BLACK_PIC_TH}"])
    iv = [(float(a), float(b)) for a, b in _events(err, "black_start", r"black_start:([0-9.]+)\s+black_end:([0-9.]+)")]
    allowed = []
    if c:                       # fadeblack 转场的重叠区是有意的黑
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
    rule = f"blackdetect pix_th={BLACK_PIX_TH} pic_th={BLACK_PIC_TH} d≥{0.5 / fps:.4f}s（单帧也算）；plan 里 fadeblack 重叠区允许"
    return _item("black_frames", "fail" if bad else "pass", rule,
                 {"intervals": len(iv), "black_s": round(sum(b - a for a, b in iv), 3)}, ev)


def _shot_moves(c, i, f0, f1):
    """编译后的运镜在镜头内帧 [f0, f1) 是否在动（取景窗有变化）。"""
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
    rule = (f"freezedetect n={FREEZE_NOISE} d≥{FREEZE_MIN_S}s；有 plan：只落在静止图片镜头（camera=[]）→ pass，"
            "落在运镜正在动的镜头 → fail，视频/网页镜头（源可能本身静止）→ needs_review；无 plan → needs_review")
    ev, worst = [], "pass"
    rank = {"pass": 0, "needs_review": 1, "fail": 2}
    for a, b in iv:
        fa, fb = int(round(a * fps)), int(round(b * fps))
        verdict, why = "needs_review", "无 plan，无法判断是否有意静止"
        shots = []
        if c:
            verdict, why = "pass", "静止图片镜头"
            for i, st in enumerate(c["starts"]):
                en = st + c["frames"][i]
                if st < fb and en > fa:
                    s = c["plan"]["shots"][i]
                    kind = c["metas"][s["id"]]["kind"]
                    mv = _shot_moves(c, i, max(fa, st) - st, min(fb, en) - st)
                    shots.append(s["id"])
                    if mv:
                        v, w = "fail", f"{s['id']} 的运镜在这段应当在动"
                    elif kind == "image":
                        v, w = "pass", "静止图片镜头"
                    else:
                        v, w = "needs_review", f"{s['id']} 是{kind}镜头，源画面可能本身静止"
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
                 f"freezedetect n={c['noise']}：静止超过 {c['max_s']} s（定格补长）→ {c['action']}；"
                 "有意的长停留写进 profile.allow", {"intervals": len(iv)}, ev)


def _low_runs(d, c, sf):
    """相邻差序列 → 低运动连续段 [(起 s, 止 s, 段内最大差)]。孤立的单个抖动样本（< spike_max，前后都低）不打断。"""
    lo, spike = c["diff_max"], c.get("spike_max", c["diff_max"])
    low = [x < lo or (x < spike and 0 < i < len(d) - 1 and d[i - 1] < lo and d[i + 1] < lo) for i, x in enumerate(d)]
    runs, start = [], None
    for i, ok in enumerate(low + [False]):
        if ok:
            start = i if start is None else start
        elif start is not None:
            a_, b_ = start / sf, i / sf          # 差值 i 覆盖样本 i→i+1
            if b_ - a_ >= c["window_s"]:
                runs.append((a_, b_, max(d[start:i])))
            start = None
    return runs


def low_motion(video, dur, prof):
    """低清灰度按 sample_fps 采样，相邻样本平均差 < diff_max 连续 ≥ window_s → 字卡慢推、定格、静态镜头。
    标定（96 px 宽、4 fps）：字卡 1.00→1.08 慢推 ≤0.25；高反差色条 1.00→1.06 慢推 ≤0.65、平移 ≤0.76；
    一条真实口播与一条 278 s 舞蹈实拍，任意 4 s 窗口的最大差 ≥0.91。阈值 0.8 在两者之间，余量小，
    所以只判 needs_review。按全局缩放/平移对齐后再比的办法试过：zoompan 自身有取整抖动，对齐后残差仍 0.6–1.0，不可用。
    同一慢推片在 ffmpeg 6.1（Linux）上有一个样本 0.82，把 4.25 s 的段切成 3.5 s 漏报。改为容忍孤立抖动：
    单个样本 < spike_max（0.9）且前后都 < diff_max 时不打断连续段。中值平滑试过，舞蹈实拍误报 8 段，不可用；
    孤立抖动规则在上述两类实拍（共 4 条）上与原规则结果完全相同。"""
    c = prof["low_motion"]
    sf = c["sample_fps"]
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-an", "-vf", f"fps={sf},scale=96:-2,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "低运动采样失败", stderr=p.stderr.decode(errors="replace")[-300:])
    pr = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                         "-of", "csv=p=0", video], capture_output=True, text=True)
    try:
        W, H = (int(x) for x in pr.stdout.strip().split(",")[:2])
    except ValueError:
        raise ReelError("OUTPUT_UNPARSABLE", "读不到画幅", stdout=pr.stdout[-200:])
    h = max(2, int(round(H * 96 / W / 2)) * 2)
    n = len(p.stdout) // (96 * h)
    if n < 2 or n < dur * sf * 0.8:
        raise ReelError("OUTPUT_UNPARSABLE", "低运动采样帧数不足，无法覆盖整片", frames=n, expect=round(dur * sf))
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
                 f"{sf} fps 低清采样，相邻帧平均差 < {c['diff_max']} 连续 ≥ {c['window_s']} s（长字卡慢推、定格、"
                 f"静态画面）→ {c['action']}：要人确认是有意设计", {"samples": n, "runs": len(runs)}, ev)


def _samples(video):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", video, "-vn", "-f", "f32le", "-acodec", "pcm_f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "音频解码失败", stderr=p.stderr.decode()[-300:])
    ch = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                         "stream=channels,sample_rate", "-of", "json", video], capture_output=True, text=True)
    try:
        st = json.loads(ch.stdout)["streams"][0]
        sr, nch = int(st["sample_rate"]), int(st["channels"])
    except (ValueError, KeyError, IndexError):
        raise ReelError("OUTPUT_UNPARSABLE", "读不到音频采样率/声道", stdout=ch.stdout[-200:])
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
    for t in times:                       # 合并成区间（间隔 >50ms 另起）
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
        raise ReelError("OUTPUT_UNPARSABLE", "ebur128 没有输出 Summary", stderr=err[-300:])
    summ = err[err.rfind("Summary:"):]
    num = lambda s: -math.inf if s == "-inf" else float(s)
    get = lambda pat: (num(m.group(1)) if (m := re.search(pat, summ, re.S)) else None)
    r = {"integrated_lufs": get(r"I:\s*(-?[0-9.]+|-inf)\s*LUFS"), "lra_lu": get(r"LRA:\s*(-?[0-9.]+)\s*LU"),
         "true_peak_dbtp": get(r"True peak:\s*Peak:\s*(-?[0-9.]+|-inf)"),
         "sample_peak_dbfs": get(r"Sample peak:\s*Peak:\s*(-?[0-9.]+|-inf)")}
    miss = [k for k in ("integrated_lufs", "true_peak_dbtp") if r[k] is None]
    if miss:                                  # 静音时是 -inf（解析得到），字段缺失才是 None
        raise ReelError("OUTPUT_UNPARSABLE", f"ebur128 Summary 缺 {miss}", summary=summ[:300])
    return {k: (None if v is not None and math.isinf(v) and k != "integrated_lufs" else v) for k, v in r.items()}


def audio_items(video, prof):
    st, meas, ev = clipping(video)
    lu = loudness(video)
    tp_c, lo_c = prof["true_peak"], prof["loudness"]
    clip = _item("audio_clipping", st, f"|sample|≥{CLIP_LEVEL} 的样本 ≥{CLIP_MIN_SAMPLES} 个 → fail；"
                 f"真峰值 > {tp_c['max_dbtp']} dBTP → {tp_c['action']}", {**meas, "true_peak_dbtp": lu["true_peak_dbtp"]}, ev)
    tp = lu["true_peak_dbtp"]
    if st == "pass" and tp is not None and tp > tp_c["max_dbtp"]:
        clip["status"] = tp_c["action"]
        clip["note"] = f"未削波，但真峰值 {tp} dBTP > {tp_c['max_dbtp']}，转码/平台再压缩可能削波"
    il = lu["integrated_lufs"]
    il_out = None if math.isinf(il) else il
    if prof["audio"] == "silent":
        ls = "pass" if il <= -60 else "fail"
        rule = "静音版：integrated ≤ -60 LUFS"
    else:
        lo, hi = lo_c["range"]
        ls = "pass" if (not math.isinf(il) and lo <= il <= hi) else lo_c["action"]
        rule = f"ebur128 integrated 在 [{lo}, {hi}] LUFS 内 → pass，否则 {lo_c['action']}（响度目标因平台而异，按 profile）"
    return [clip, _item("loudness", ls, rule, {**lu, "integrated_lufs": il_out})]


def silence_items(video, dur, prof):
    c = prof["silence"]
    if prof["audio"] == "silent":
        note = "静音版 profile：首尾无声不查"
        return [_item(n, "pass", "静音版不查", note=note) for n in ("head_silence", "tail_silence", "mid_silence")]
    err = _ff(["-i", video, "-vn", "-af", f"silencedetect=n={c['noise_db']}dB:d={c['min_s']}"])
    ss = [float(x) for x in _events(err, "silence_start", r"silence_start:\s*(-?[0-9.]+)")]
    se = [float(x) for x in _events(err, "silence_end", r"silence_end:\s*([0-9.]+)")]
    sil = [(max(0.0, s), se[i] if i < len(se) else dur) for i, s in enumerate(ss)]
    head = 0.0
    for s, e in sil:                       # 从 0 开始连续的无声（可能被切成相邻几段）
        if s <= head + 0.1:
            head = max(head, e)
    hs = c["head_action"] if head >= c["head_max_s"] else "pass"
    h_item = _item("head_silence", hs, f"silencedetect n={c['noise_db']}dB：开头连续无声 ≥ {c['head_max_s']} s → "
                   f"{c['head_action']}（开头要有声音，配乐别从前奏空白处开始）", {"head_silence_s": round(head, 2)},
                   [{"t": [0.0, round(head, 2)], "offending": True}] if hs != "pass" else [])
    tail = [(s, e) for s, e in sil if e >= dur - 0.05 and e - s >= c["tail_max_s"]]
    tail_s = max((e - s for s, e in sil if e >= dur - 0.05), default=0.0)
    ts = c["tail_action"] if tail else "pass"
    t_item = _item("tail_silence", ts, f"片尾连续无声 ≥ {c['tail_max_s']} s → {c['tail_action']}（音乐/口播比画面短？）",
                   {"tail_silence_s": round(tail_s, 2)},
                   [{"t": [round(s, 2), round(e, 2)], "offending": True} for s, e in tail])
    mid = [(s, e) for s, e in sil if s > head + 0.1 and (s, e) not in tail and e - s >= c["mid_review_s"]]
    m_item = _item("mid_silence", "needs_review" if mid else "pass",
                   f"中间无声 ≥ {c['mid_review_s']} s → needs_review（确认不是掉音）", {"segments": len(mid)},
                   [{"t": [round(s, 2), round(e, 2)], "offending": True} for s, e in mid])
    return [h_item, t_item, m_item]


# ---------------- 汇总 ----------------

def _guarded(items, names, fn):
    """检测出错不中断整份报告：对应各项记 not_checked + error，由 gate 计入阻断。"""
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
        items.append(_item(n, "not_checked", "检测出错，结果未知", note=f"{err['code']}: {err['message']}", error=err))


def _apply_allow(items, prof):
    """profile.allow 登记的例外：该项所有违规证据都落在登记区间内 → pass（记 excused）。"""
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
        items.append(_item("audio_clipping", "pass", "静音版：没有音轨可以"))
        items.append(_item("loudness", "pass", "静音版：没有音轨可以"))
        items.extend(silence_items(video, dur, prof))
    else:
        st = "fail" if prof["audio"] == "required" else "not_checked"
        items.append(_item("audio_clipping", st, "成片应有音轨", {"audio_stream": False}))
        items.append(_item("loudness", "not_checked", "没有音轨"))
        for n in ("head_silence", "tail_silence", "mid_silence"):
            items.append(_item(n, "not_checked", "没有音轨"))
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
            items.append(_item(name, "pass" if ch["ok"] else "fail", "verify.check（与 render 验收同一套）", meas, ev))
        if cues is None:
            items.append(_item("subtitles_in_range", "not_checked", "plan 没有 audio.subtitles"))
    else:
        items.append(_item("subtitles_in_range", "not_checked", "需要 --plan 才知道字幕 cue"))
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
            "status": status, "status_scope": "只代表已检查的项；not_checked 项未验证；能否交付看 gate",
            "counts": count, "items": items}
