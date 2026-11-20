"""节拍分析 + 卡点吸附。

analyze()：音乐 → 节拍、强拍（每小节第一拍）、BPM。
  - beat_this（2024，CPJKU，同时出拍与强拍，精度高）为默认；
  - librosa 为兜底（只出拍；强拍按 4/4 取重音最强的相位推断，标注 downbeat_method=inferred）。
  两者都跑在 reel 自己的 venv（$REEL_VENV，默认 ~/.local/share/reel/venv；./install.sh --beats 安装），系统 Python 不需要装任何东西。
snap_boundaries()：把镜头切点吸附到最近的拍/强拍（在 tolerance 内），保证最短镜头时长。
"""
import json, os, subprocess
from .errors import ReelError

VENV_PY = os.environ.get("REEL_VENV_PY") or os.path.join(
    os.environ.get("REEL_VENV") or os.path.expanduser("~/.local/share/reel/venv"), "bin", "python")

_WORKER = r'''
import json, sys, numpy as np
path, backend = sys.argv[1], sys.argv[2]
import librosa
y, sr = librosa.load(path, sr=22050, mono=True)
dur = len(y) / sr
out = {"duration": round(dur, 4)}
HOP = 256   # 11.6ms 分辨率（默认 512 为 23ms）
def lib():
    tempo, bt = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP, units="time", trim=False)
    beats = np.atleast_1d(bt).astype(float)
    # 起音强度的峰值总在击打之后；把每个拍点对齐到 ±60ms 内最近的「击打起点」（backtrack 到能量谷）
    ons = librosa.onset.onset_detect(y=y, sr=sr, hop_length=HOP, units="time", backtrack=True)
    if len(ons):
        idx = np.clip(np.searchsorted(ons, beats), 1, len(ons) - 1)
        near = np.where(np.abs(ons[idx - 1] - beats) < np.abs(ons[idx] - beats), ons[idx - 1], ons[idx])
        beats = np.where(np.abs(near - beats) <= 0.06, near, beats)
    beats = [float(b) for b in beats]
    # 强拍推断：4/4 下 4 个相位里，拍点处起音强度总和最大的相位即强拍
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    fr = librosa.time_to_frames(beats, sr=sr, hop_length=HOP)
    fr = np.clip(fr, 0, len(env) - 1)
    score = [env[fr[p::4]].sum() if len(fr[p::4]) else 0 for p in range(4)]
    ph = int(np.argmax(score))
    ibi = np.diff(beats)
    return beats, beats[ph::4], float(60 / np.median(ibi)) if len(ibi) else float(np.atleast_1d(tempo)[0])
if backend in ("auto", "beat_this"):
    try:
        import torch
        from beat_this.inference import File2Beats
        f2b = File2Beats(checkpoint_path="final0", device="cpu", dbn=False)
        b, d = f2b(path)
        beats = np.array([float(x) for x in b]); raw_d = np.array([float(x) for x in d])
        ib = np.diff(beats)
        ref = float(np.median(ib)) if len(ib) else 0.0
        glob_t = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
        if glob_t > 0 and 1.6 <= (60 / glob_t) / max(ref, 1e-6) <= 2.4:   # 多余点多到中位拍距塌成半拍
            ref = 60 / glob_t
        crowd = float(np.mean(ib < 0.6 * ref)) if len(ib) else 0.0
        if crowd > 0.10:
            # 拍点里 >10% 挤在两拍之间（常见于只有鼓点/反拍的稀疏节奏）：beat_this 的拍网格不可信，改用起音校正后的 librosa
            raise RuntimeError(f"beat_this 拍点过密（{crowd:.0%} 间隔 <0.6 拍距）")
        # 去掉挤在正常拍之间的多余点（切分音被误当成拍）：间隔 <0.6 个中位拍距时，只留更贴近均匀网格的那个
        if len(beats) > 4:
            med = float(np.median(np.diff(beats)))
            keep = [beats[0]]
            for x in beats[1:]:
                if x - keep[-1] >= 0.6 * med:
                    keep.append(x)
                else:
                    ref = keep[-2] + med if len(keep) > 1 else keep[-1]
                    if abs(x - ref) < abs(keep[-1] - ref):
                        keep[-1] = x
            beats = np.array(keep)
        # 强拍规整：模型给的强拍常有漏报/多报（合成节奏里第 1、2 拍都会被标）。按「拍序号 mod 4」投票选小节相位，
        # 打平时用起音重音决定，然后每 4 拍取一个 → 规律的小节网格；原始强拍另存
        votes = np.zeros(4)
        for x in raw_d:
            k = int(np.argmin(np.abs(beats - x)))
            if abs(beats[k] - x) < 0.07:
                votes[k % 4] += 1
        env0 = librosa.onset.onset_strength(y=y, sr=sr, hop_length=256)
        # 起音强度峰值在击打之后几十毫秒：取拍点后 ~60ms 窗口内的最大值作为该拍的重音
        env = np.array([env0[i:i + 6].max() if i < len(env0) else 0 for i in range(len(env0))])
        fr = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=256), 0, len(env) - 1)
        accent = np.array([env[fr[p::4]].mean() if len(fr[p::4]) else 0 for p in range(4)])
        best = votes.max()
        cands = [p for p in range(4) if votes[p] >= best - 1e-9]
        ph = max(cands, key=lambda p: accent[p])
        # 分段（每 16 拍）投票：漏拍/多拍后小节相位会漂，逐段重新对齐；段内票不足时沿用全局相位
        near_k = []
        for x in raw_d:
            k = int(np.argmin(np.abs(beats - x)))
            if abs(beats[k] - x) < 0.07:
                near_k.append(k)
        near_k = np.array(near_k, dtype=int)
        downs, last = [], -99
        for s0 in range(0, len(beats), 16):
            ks = near_k[(near_k >= s0 - 4) & (near_k < s0 + 20)]
            v = np.bincount(ks % 4, minlength=4) if len(ks) else np.zeros(4)
            if v.max() >= 2:
                top = [q for q in range(4) if v[q] >= v.max() - 1e-9]
                if len(top) == 1:
                    loc = top[0]
                else:                                   # 打平：比这一段里各相位拍点的起音重音
                    seg = fr[s0:s0 + 16]; idx = np.arange(s0, s0 + len(seg))
                    loc = max(top, key=lambda q: env[seg[idx % 4 == q]].mean() if np.any(idx % 4 == q) else 0)
            else:
                loc = ph
            for k in range(s0, min(s0 + 16, len(beats))):
                if k % 4 == loc and k - last >= 3:          # 相位切换处避免两个强拍挨得太近
                    downs.append(beats[k]); last = k
        downs = np.array(downs)
        ibi = np.diff(beats)
        bpm = float(60 / np.median(ibi)) if len(ibi) else 0.0
        out.update(backend="beat_this", beats=beats.tolist(), downbeats=downs.tolist(), bpm=round(bpm, 2),
                   downbeat_method="model_vote_regularized", raw_downbeats=raw_d.tolist(),
                   downbeat_votes=votes.tolist(), downbeat_agreement=round(float(best / max(1, votes.sum())), 3))
    except Exception as e:
        if backend == "beat_this":
            print(json.dumps({"error": f"beat_this failed: {e}"})); sys.exit(0)
        beats, downs, bpm = lib()
        out.update(backend="librosa", beats=beats, downbeats=downs, bpm=round(bpm, 2),
                   downbeat_method="inferred", fallback_reason=str(e)[:200])
else:
    beats, downs, bpm = lib()
    out.update(backend="librosa", beats=beats, downbeats=downs, bpm=round(bpm, 2), downbeat_method="inferred")
print(json.dumps(out))
'''


def analyze(audio_path, backend="auto", timeout=600):
    if not os.path.isfile(audio_path):
        raise ReelError("ASSET_NOT_FOUND", f"音频不存在: {audio_path}")
    if not os.path.exists(VENV_PY):
        raise ReelError("BACKEND_UNAVAILABLE", "beat analysis needs the reel venv with librosa (and optionally beat_this/torch): run ./install.sh --beats",
                        venv=VENV_PY)
    # beat_this 读取要求 wav/常见格式；统一先转 44.1k 单声道 wav，避免 mp3 解码差异
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio_path, "-ac", "1", "-ar", "44100", wav],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise ReelError("FFMPEG_FAILED", "音频解码失败", stderr=p.stderr[-400:])
        p = subprocess.run([VENV_PY, "-c", _WORKER, wav, backend], capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    if p.returncode != 0 or not lines:
        raise ReelError("BEATS_FAILED", "节拍分析失败", stderr=p.stderr[-800:])
    res = json.loads(lines[-1])
    if "error" in res:
        raise ReelError("BEATS_FAILED", res["error"])
    if len(res.get("beats", [])) < 4:
        raise ReelError("BEATS_FAILED", "检出的拍点太少（音乐太短或没有明显节奏）", beats=len(res.get("beats", [])))
    res["source"] = audio_path
    return res


def grid(beats_info, mode="beat"):
    """mode: beat（每拍）| downbeat（强拍）| every:N（每 N 拍）| half（半拍，拍间插中点）。"""
    b = beats_info["beats"]
    if mode == "beat":
        return list(b)
    if mode == "downbeat":
        return list(beats_info.get("downbeats") or b[::4])
    if mode == "half":
        out = []
        for x, y in zip(b, b[1:]):
            out += [x, (x + y) / 2]
        return out + b[-1:]
    if mode.startswith("every:"):
        n = int(mode.split(":")[1])
        d = beats_info.get("downbeats") or []
        start = b.index(min(b, key=lambda t: abs(t - d[0]))) if d else 0   # 从第一个强拍开始数
        return b[start::n]
    raise ReelError("ARG_INVALID", f"未知卡点模式 {mode!r}（beat / downbeat / every:N / half）")


def snap_boundaries(bounds, targets, tolerance=0.30, min_gap=0.35, offset=0.0, fallback=None, feasible=None):
    """bounds：原切点（秒，严格递增，不含片头 0 和片尾）。targets：拍点（音乐时间）；offset：音乐在时间线上的起点。
    每个切点吸附到 tolerance 内最近且保持 ≥min_gap 间隔的拍点；强拍不行就退到 fallback（普通拍）。
    feasible(i, t, prev)：可选约束（例如镜头时长必须容纳运镜/转场/源长度），不满足的拍点不选。
    找不到就保持原位，报告里 reason=no_beat（容差内没有拍）或 constraint（有拍但会让镜头不合法）。返回 (新切点, 报告)。"""
    tg = sorted(t + offset for t in targets)
    fb = sorted(t + offset for t in (fallback or []))
    ok = feasible or (lambda i, t, prev: True)
    out, report, prev = [], [], 0.0
    for i, b in enumerate(bounds):
        allowed, level, blocked = [], None, False
        for lvl, pool in (("primary", tg), ("fallback", fb)):
            near = [t for t in pool if abs(t - b) <= tolerance and t - prev >= min_gap]
            allowed = [t for t in near if ok(i, t, prev)]
            blocked = blocked or bool(near and not allowed)
            if allowed:
                level = lvl
                break
        if allowed:
            t = min(allowed, key=lambda x: abs(x - b))
            report.append({"cut": i, "from": round(b, 4), "to": round(t, 4), "moved": round(t - b, 4), "level": level})
            prev = t
            out.append(t)
        else:
            report.append({"cut": i, "from": round(b, 4), "to": round(b, 4), "moved": 0.0, "unsnapped": True,
                           "reason": "constraint" if blocked else "no_beat"})
            prev = b
            out.append(b)
    return out, report


def _duration_limits(c, observations, trans):
    """每个镜头允许的时长区间 [lo, hi]（秒），与 plan 校验/编译的规则一致：
    运镜与手动高亮的 t1、两侧转场的 2 倍、视频镜头引用的 observation 时间点、视频源剩余长度。"""
    fps = c["plan"]["output"]["fps"]
    obs = {o["id"]: o for o in (observations or []) if isinstance(o, dict) and "id" in o}
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    lo, hi = [], []
    for k, s in enumerate(c["plan"]["shots"]):
        need = [2 / fps, 2 * trans[k], 2 * (trans[k - 1] if k else 0)]
        need += [op["t1"] for op in s.get("camera") or [] if num(op.get("t1"))]
        need += [h["t1"] for h in s.get("highlight") or [] if h.get("sync") == "manual" and num(h.get("t1"))]
        top = float("inf")
        m = c["metas"].get(s["id"]) or {}
        if m.get("kind") == "video":
            si = s.get("source_in") or 0
            if num(m.get("duration")):
                top = m["duration"] - si
            for op in s.get("camera") or []:
                ids = [op.get("observation_id")] + list(op.get("track") or [])
                need += [obs[x]["t"] - si for x in ids if x in obs and num(obs[x].get("t"))]
        lo.append(max(need) + 1e-3)
        hi.append(top - 1e-3)
    return lo, hi


def snap_plan(plan, beats_info, mode="downbeat", tolerance=0.30, min_gap=0.35, offset=0.0, observations=None):
    """按拍点调整 plan 的镜头时长，使硬切/转场起点落在拍上。只改 duration，不改顺序与运镜。
    切点定义为下一镜头在时间线上的起点（有转场时即转场开始）。observations 供 zoom_to 引用校验。
    只选不会让镜头变得不合法的拍点（运镜 t1 放不下、转场过长、视频源不够长）；选不到就保持原位并在报告里说明。"""
    import copy
    from . import render
    c = render.compile_plan(copy.deepcopy(plan), observations)
    fps = c["plan"]["output"]["fps"]
    starts = [s / fps for s in c["starts"]]
    trans = [t / fps for t in c["trans"]]
    end = (c["starts"][-1] + c["frames"][-1]) / fps
    bounds = starts[1:]
    lo, hi = _duration_limits(c, observations, trans)
    nxt = bounds[1:] + [end]                 # 下一个切点先按原位估计：它没吸附上时镜头 i+1 也必须合法

    def feasible(i, t, prev):
        d_i, d_n = t - prev + trans[i], nxt[i] - t + trans[i + 1]
        return lo[i] <= d_i <= hi[i] and lo[i + 1] <= d_n <= hi[i + 1]

    fb = beats_info["beats"] if mode != "beat" else None
    new_b, report = snap_boundaries(bounds, grid(beats_info, mode), tolerance, min_gap, offset, fallback=fb,
                                    feasible=feasible)
    p = copy.deepcopy(plan)
    edges = [0.0] + new_b
    for i, s in enumerate(p["shots"][:-1]):
        # 镜头 i 在时间线上占 [edges[i], edges[i+1]) 再加上它与下一镜头的转场重叠
        s["duration"] = round(edges[i + 1] - edges[i] + trans[i], 4)
    if c["plan"]["duration_policy"] == "require_match":
        p["shots"][-1]["duration"] = round(end - edges[-1], 4)   # 总长不变，否则 DURATION_MISMATCH
    return p, {"mode": mode, "tolerance": tolerance, "cuts": report,
               "snapped": sum(1 for r in report if not r.get("unsnapped")), "total": len(report)}
