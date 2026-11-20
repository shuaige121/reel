"""MV 验收契约（reel.contract.v1）：不依赖渲染 plan，任何渲染器出的 MV 都按同一套验。

契约（JSON）：
  {"version": "reel.contract.v1", "kind": "mv",
   "output": {"width": 720, "height": 1280, "fps": 30},
   "songs": [{"path": "歌.m4a", "sha256": "...", "at": 0.0, "policy": "full"}],
            # 合集写多首，at=该首在成片里的起点；policy: full（完整播放，默认）| trim（必须带 "range": [a, b] 和 "reason"）
   "lyrics": {"path": "x.lyrics.json", "sha256": "..."},          # reel lyrics import 的输出；可选
   "text_layer": {"path": "字层.mkv", "sha256": "..."},             # 歌词字形的灰度遮罩（亮=有字），与成片同长同帧率；
                                                                   # 只含字形，不含粒子、光环、闪白；有 lyrics 时必填
   "required": [], "release": {"regions": "...", "ai_label": true},  # release 只记录，不判定
   "effects": {"path": "音效层.wav", "sha256": "...", "reason": "用户原话"}}
            # 可选：用户要求叠加的音效。音效层 = 成片音轨减原曲的那一层（从成片 0 s 起、单曲完整播放时用），
            # song_identity / song_level 以「原曲 + 音效层」为参照；音效层每个有声的 5 s 窗与原曲的相关 |r| ≤ 0.3
            # （改原曲的增益、换掉一段都和原曲强相关，不能伪装成音效）

新增检查项（mv 契约下全部是必查项，profile 不能去掉）：
  contract_files     契约里每个文件都在、sha256 没变
  av_duration_match  音视频流时长差 ≤ 1 帧
  output_format      宽高帧率 = 契约
  song_identity      每首歌全长：每 5 s 一窗，偏移 ≤ 1 ms、相关 ≥ 0.95、电平差 ≤ 0.5 dB（静音窗跳过），且覆盖全曲
                     （policy=full 不许截短）；合集逐首按 at 定位；声明了 effects 时参照为原曲 + 音效层，且音效层不得与原曲相关
  song_level         成片真峰值 ≤ 原曲 + 1.0 dB（有损重编码会上冒约 0.5 dB）、整体响度差 ≤ 0.5 LU；通过时，default 的绝对响度/削波
                     needs_review 记为 excused（音轨原样用，削波来自原曲母带）
  lyrics_on_screen   每句在 [t0+30%, t1-0.1] 取 3 帧，字层上有字的像素 ≥ 字数 × 最小字面积 才算上屏（≥2 帧）
  lyrics_not_early   每句在 [max(上一句结束, t0-0.5), t0-1帧] 之间，字层不能新长出该句规模的字
解码一律整段解码后按样本切片（输入端 -ss 在 AAC 上会给出 0.3 ms 的假偏移）。
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
EFF_CORR_MAX = 0.3   # 声明的音效层与原曲的相关上限（每个有声的 5 s 窗）
TP_SLACK_DB = 1.0     # 有损重编码会让样间峰值上冒：黑纹退了 Opus→AAC 实测 +0.5 dB（响度不变）；整体增益改动由 LUFS 差和 song_identity 抓
LOUD_SLACK_LU = 0.5
INK_TH = 64          # 字层灰度 > 64 视为有字
MIN_CHAR_PX = 120    # 每个字至少这么多像素（720 宽画面、40 px 以上字号的保守下限）
REQUIRED = ["contract_files", "av_duration_match", "output_format", "song_identity", "song_level"]
REQUIRED_LYRICS = ["lyrics_on_screen", "lyrics_not_early"]


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            c = json.load(fh)
    except (OSError, ValueError) as e:
        raise ReelError("CONTRACT_INVALID", f"读不了契约：{e}", path=path)
    if c.get("version") != "reel.contract.v1" or c.get("kind") != "mv":
        raise ReelError("CONTRACT_INVALID", "不是 reel.contract.v1 / kind=mv", path=path)
    if not c.get("songs"):
        raise ReelError("CONTRACT_INVALID", "契约没有 songs", path=path)
    base = os.path.dirname(os.path.abspath(path))
    rel = lambda p: p if os.path.isabs(os.path.expanduser(p)) else os.path.join(base, p)
    for s in c["songs"]:
        s["path"] = rel(s["path"])
        s.setdefault("at", 0.0); s.setdefault("policy", "full")
        if s["policy"] == "trim" and not (s.get("range") and s.get("reason")):
            raise ReelError("CONTRACT_INVALID", "policy=trim 必须写 range 和 reason", song=s["path"])
    for k in ("lyrics", "text_layer", "effects"):
        if c.get(k):
            c[k]["path"] = rel(c[k]["path"])
    if c.get("effects"):
        e = c["effects"]
        if not (e.get("path") and e.get("sha256") and (e.get("reason") or "").strip()):
            raise ReelError("CONTRACT_INVALID", "effects 必须写 path、sha256 和 reason（用户要求叠加音效的原话）", path=path)
        if len(c["songs"]) != 1 or c["songs"][0]["policy"] != "full" or float(c["songs"][0]["at"]) != 0.0:
            raise ReelError("CONTRACT_INVALID", "effects 只支持单曲、完整播放、从 0 s 起的 MV", path=path)
    if c.get("lyrics") and not c.get("text_layer"):
        raise ReelError("CONTRACT_INVALID", "有 lyrics 就必须给 text_layer（字层遮罩），否则查不了歌词上屏和早出", path=path)
    if c.get("lyrics"):   # 歌词时间只认 reel lyrics import 的输出，且对应契约里的歌（手写的时间表不算）
        try:
            with open(c["lyrics"]["path"], encoding="utf-8") as fh:
                L = json.load(fh)
        except (OSError, ValueError) as e:
            raise ReelError("CONTRACT_INVALID", f"读不了 lyrics：{e}", path=c["lyrics"]["path"])
        if not isinstance(L, dict) or L.get("version") != "reel.lyrics.v1":
            raise ReelError("CONTRACT_INVALID", "lyrics 必须是 reel lyrics import 的输出（version=reel.lyrics.v1）",
                            path=c["lyrics"]["path"])
        if (L.get("song") or {}).get("sha256") not in {s.get("sha256") for s in c["songs"]}:
            raise ReelError("CONTRACT_INVALID", "lyrics 对应的歌不是契约里的歌（song.sha256 对不上）", path=c["lyrics"]["path"])
    c["_path"] = os.path.abspath(path)
    c["_sha256"] = check.sha256(path)
    return c


def required(c):
    return REQUIRED + (REQUIRED_LYRICS if c.get("lyrics") else []) + list(c.get("required") or [])


def _pcm(path, ch=1):
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", str(ch), "-ar", str(SR), "-f", "f32le", "-"],
                       capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "音频解码失败", path=path, stderr=p.stderr[-300:].decode(errors="ignore"))
    a = np.frombuffer(p.stdout, dtype=np.float32)
    return a.reshape(-1, ch) if ch > 1 else a


def _ebur(path):
    err = check._ff(["-i", path, "-vn", "-af", "ebur128=peak=true"])
    i = re.findall(r"I:\s+(-?[\d.]+) LUFS", err); tp = re.findall(r"Peak:\s+(-?[\d.]+|-inf) dBFS", err)
    if not i or not tp:
        raise ReelError("OUTPUT_UNPARSABLE", "ebur128 输出解析不了", path=path)
    return float(i[-1]), float(tp[-1]) if tp[-1] != "-inf" else -120.0


def _item(*a, **k):
    return check._item(*a, **k)


def contract_files(c):
    bad = []
    deps = [("song", s) for s in c["songs"]] + [(k, c[k]) for k in ("lyrics", "text_layer", "effects") if c.get(k)]
    for kind, d in deps:
        if not os.path.exists(d["path"]):
            bad.append({"which": kind, "path": d["path"], "why": "文件不在"})
        elif d.get("sha256") and check.sha256(d["path"]) != d["sha256"]:
            bad.append({"which": kind, "path": d["path"], "why": "sha256 变了（契约写完后文件被换过）"})
        elif not d.get("sha256"):
            bad.append({"which": kind, "path": d["path"], "why": "契约没写 sha256"})
    return _item("contract_files", "fail" if bad else "pass", "契约里的文件都在、hash 没变",
                 {"files": len(deps)}, [{"offending": True, **b} for b in bad])


def av_duration(info, fps):
    vs = [s for s in info["streams"] if s.get("codec_type") == "video"]
    as_ = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    if not vs or not as_:
        return _item("av_duration_match", "fail", "成片要有视频流和音频流", {"video": bool(vs), "audio": bool(as_)})
    vd, ad = float(vs[0].get("duration") or 0), float(as_[0].get("duration") or 0)
    ok = abs(vd - ad) <= 1.0 / fps + 1e-3
    return _item("av_duration_match", "pass" if ok else "fail", "视频流与音频流时长差 ≤ 1 帧",
                 {"video_s": round(vd, 3), "audio_s": round(ad, 3), "diff_s": round(vd - ad, 3)},
                 [] if ok else [{"t": [round(min(vd, ad), 3), round(max(vd, ad), 3)], "offending": True}])


def output_format(info, c, fps):
    o = c.get("output") or {}
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), {})
    got = {"width": v.get("width"), "height": v.get("height"), "fps": round(fps, 3)}
    bad = {k: (got[k], o[k]) for k in ("width", "height", "fps") if o.get(k) is not None and abs(float(got[k] or 0) - float(o[k])) > 0.01}
    if not o:
        return _item("output_format", "not_checked", "契约没写 output（宽高帧率）", got)
    return _item("output_format", "fail" if bad else "pass", "宽高帧率等于契约", {**got, "mismatch": bad})


def song_identity(video, c):
    mv = _pcm(video)
    ev, meas = [], []
    eff = _pcm(c["effects"]["path"]) if c.get("effects") else None
    for s in c["songs"]:
        song = _pcm(s["path"])
        if eff is not None:   # 参照 = 原曲 + 声明的音效层；音效层先过「不与原曲相关」
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
                               "why": "声明的音效层和原曲相关：这是在改原曲，不是叠加音效", "offending": True})
            song = song + e
            meas.append({"effects": os.path.basename(c["effects"]["path"]), "reason": c["effects"]["reason"], "effect_windows_5s": active})
        if s["policy"] == "trim":
            a, b = s["range"]; song = song[int(a * SR):int(b * SR)]
        i0 = int(round(s["at"] * SR))
        seg = mv[i0:i0 + len(song)]
        if len(song) < SR or len(seg) < SR:
            ev.append({"song": s["path"], "why": "解码不到音频", "offending": True}); continue
        if len(seg) < len(song) - SR / 30:
            ev.append({"song": s["path"], "why": f"成片里这首只有 {len(seg) / SR:.2f}s，原曲 {len(song) / SR:.2f}s（被截短）",
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
            ev.append({"song": s["path"], "why": "没有一个窗口可比（全是静音？）", "offending": True})
        meas.append({"song": os.path.basename(s["path"]), "at": s["at"], "windows": checked, "song_s": round(len(song) / SR, 3)})
    return _item("song_identity", "fail" if ev else "pass",
                 f"每首歌全长每 {WIN_S:.0f}s 一窗：偏移 ≤ {LAG_MAX_S * 1000:.0f} ms、相关 ≥ {CORR_MIN}、电平差 ≤ {GAIN_MAX_DB} dB；不许截短"
                 + ("；参照 = 原曲 + 契约声明的音效层，音效层与原曲 |r| ≤ %.1f" % EFF_CORR_MAX if eff is not None else ""),
                 {"songs": meas}, ev[:12])


def song_level(video, c):
    if len(c["songs"]) != 1 or c["songs"][0]["policy"] != "full":
        return _item("song_level", "not_checked", "只对单曲、完整播放的 MV 做相对电平判定（合集逐首见 song_identity 的电平差）")
    mi, mtp = _ebur(video); ref = c["songs"][0]["path"]; tmp = None
    if c.get("effects"):   # 参照 = 原曲 + 声明的音效层（48 kHz 浮点相加后量）
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
                 f"成片真峰值 ≤ 原曲 + {TP_SLACK_DB} dB、整体响度差 ≤ {LOUD_SLACK_LU} LU（音轨原样用" + ("；参照含契约声明的音效层）" if c.get("effects") else "）"),
                 {"mv_lufs": mi, "song_lufs": si, "mv_peak_dbfs": mtp, "song_peak_dbfs": stp})


def _matte_frames(path, times, w=180):
    """在给定时刻取字层灰度帧（缩到 w 宽），整段解码一次后按帧号取，避免 -ss 的不精确。"""
    info, dur = check._probe(path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise ReelError("ASSET_UNREADABLE", "字层没有视频流", path=path)
    W, H = int(v["width"]), int(v["height"]); h = int(round(H * w / W))
    fps = check._fps(v) or 30.0
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"scale={w}:{h}:flags=area,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "字层解码失败", path=path)
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
        msg = f"字层 {ldur:.3f}s@{lfps:g} 与成片 {vdur:.3f}s@{vfps:g} 对不上"
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
        n = sum(unicodedata.category(c)[0] in "LN" for c in ln["text"]) or 1   # 组合符号（泰文元音、声调）不单独占字宽
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
                  f"每句在行内 30/55/80% 处至少 2 帧有字（字层像素 ≥ 字数×{MIN_CHAR_PX}）", {"lines": len(lines)}, on_ev[:12]),
            _item("lyrics_not_early", "fail" if early_ev else "pass",
                  "唱到才出：起唱前 0.3 s 到前一帧之间，本句字形区域（不含上一句的区域）不能新长出 ≥ 本句规模 30% 的字",
                  {"lines": len(lines)}, early_ev[:12])]


def run(video, c, info, dur, fps, items):
    """往 check.run 的 items 里加契约检查，并按 song_level 处理 default 的绝对响度/削波项。"""
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
                x["excused"] = {"from": x["status"], "by_contract": "song_level：成片电平与原曲一致，音轨原样用"}
                x["status"] = "pass"
