#!/usr/bin/env python3
"""节拍分析与卡点：python3 tests/test_beats.py（需要 reel venv：~/.local/share/reel/venv）。
合成已知节拍的音轨，断言拍点/强拍精度、吸附逻辑、成片切点落拍（逐帧检测颜色突变）。"""
import json, os, shutil, subprocess, sys, tempfile, traceback, wave
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reelcore import beats as B  # noqa: E402

TMP = tempfile.mkdtemp(prefix="reel_beats_")
RESULTS = []
SKIPPED = []


class Skip(Exception):
    """Optional dependency or model missing: reported as SKIPPED, never as passed."""


def case(fn):
    try:
        fn(); RESULTS.append((fn.__name__, True, ""))
    except Skip as e:
        SKIPPED.append((fn.__name__, str(e)))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((fn.__name__, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
    return fn


def synth(path, bpm=120, start=0.3, dur=24.0, sr=44100):
    """4/4 节奏：强拍底鼓更响、点击音更高，反拍轻踩镲。返回真实拍点/强拍。"""
    t = np.arange(int(sr * dur)) / sr
    y = np.zeros_like(t)
    ib = 60 / bpm
    beats = np.arange(start, dur - 0.2, ib)

    def hit(at, f, a, dec):
        i = int(at * sr); n = int(0.25 * sr); tt = np.arange(n) / sr
        seg = a * np.sin(2 * np.pi * f * tt) * np.exp(-tt * dec)
        y[i:i + n] += seg[:len(y) - i]
    for k, b in enumerate(beats):
        d = k % 4 == 0
        hit(b, 60 if d else 55, 0.9 if d else 0.45, 18)
        hit(b, 1800 if d else 1200, 0.25 if d else 0.12, 60)
        hit(b + ib / 2, 7000, 0.05, 90)
    y = y / np.abs(y).max() * 0.8
    w = wave.open(path, "wb"); w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    w.writeframes((y * 32767).astype(np.int16).tobytes()); w.close()
    return beats.tolist(), beats[::4].tolist()


WAV = os.path.join(TMP, "click.wav")
TRUE_BEATS, TRUE_DOWNS = synth(WAV)


def errs(det, ref, lo=1.0, hi=22.0):
    det = np.array(det)
    return np.array([np.min(np.abs(det - x)) for x in ref if lo < x < hi])


@case
def librosa_beats_and_downbeats_accurate():
    r = B.analyze(WAV, "librosa")
    e = errs(r["beats"], TRUE_BEATS)
    assert np.median(e) < 0.012 and e.max() < 0.02, (np.median(e), e.max())          # 修正前系统性晚约 30ms
    d = errs(r["downbeats"], TRUE_DOWNS)
    assert (d < 0.02).all(), d                                                         # 强拍相位推断正确
    assert abs(r["bpm"] - 120) < 1.5, r["bpm"]


@case
def auto_backend_falls_back_on_crowded_beats():
    """稀疏鼓点+反拍踩镲的合成节奏上，beat_this 会把反拍也当拍（拍点过密）。
    auto 必须识别并改用起音校正后的 librosa，结果仍准确；显式 beat_this 必须明确报错而不是给错网格。"""
    if not os.path.exists(os.path.expanduser("~/.cache/torch/hub/checkpoints/beat_this-final0.ckpt")):
        raise Skip("beat_this checkpoint not cached (./install.sh --beats-ml, then run reel beats once)")
    r = B.analyze(WAV, "auto")
    assert r["backend"] == "librosa" and "过密" in r.get("fallback_reason", ""), (r["backend"], r.get("fallback_reason"))
    assert (errs(r["beats"], TRUE_BEATS) < 0.02).all() and (errs(r["downbeats"], TRUE_DOWNS) < 0.02).all()
    try:
        B.analyze(WAV, "beat_this"); raise AssertionError("显式 beat_this 应报 BEATS_FAILED")
    except B.ReelError as e:
        assert e.code == "BEATS_FAILED", e.code


@case
def snap_moves_cuts_to_downbeats_with_fallback():
    info = {"beats": TRUE_BEATS, "downbeats": TRUE_DOWNS}
    new, rep = B.snap_boundaries([3.4, 5.0, 7.6], B.grid(info, "downbeat"), tolerance=1.0, min_gap=0.35,
                                 fallback=info["beats"])
    assert abs(new[0] - 4.3) < 1e-9 and abs(new[2] - 8.3) < 1e-9, new
    assert abs(new[1] - 4.8) < 1e-9 and rep[1]["level"] == "fallback", rep[1]         # 强拍被占用 → 退到普通拍
    new2, rep2 = B.snap_boundaries([3.4, 5.0], B.grid(info, "downbeat"), tolerance=1.0, min_gap=0.35)
    assert rep2[1].get("unsnapped"), rep2                                               # 无回退时如实报告未吸附


@case
def snap_accepts_plans_with_observation_camera():
    """zoom_to 引用 observations.json 的 plan 也要能卡点（曾因 snap 编译时没带 observations 报 PLAN_INVALID）。"""
    from PIL import Image
    d = os.path.join(TMP, "obs"); os.makedirs(d)
    for i in range(3):
        Image.new("RGB", (540, 960), (60 * i, 90, 160)).save(os.path.join(d, f"c{i}.png"))
    json.dump([{"id": "obs-1", "asset": "c0.png", "bbox": [0.3, 0.3, 0.6, 0.6]}],
              open(os.path.join(d, "observations.json"), "w"))
    plan = {"version": "reel.plan.v1", "src_dir": ".", "output": {"width": 270, "height": 480, "fps": 30},
            "duration_policy": "fit_last_shot",
            "shots": [{"id": "s0", "asset": "c0.png", "duration": 3.4,
                       "camera": [{"op": "zoom_to", "observation_id": "obs-1", "t0": 0, "t1": 1}]},
                      {"id": "s1", "asset": "c1.png", "duration": 2.0, "camera": []},
                      {"id": "s2", "asset": "c2.png", "duration": 18.6, "camera": []}],
            "audio": {"voice": WAV}, "look": {}}
    pj = os.path.join(d, "plan.json"); json.dump(plan, open(pj, "w"))
    bj = os.path.join(d, "beats.json"); json.dump({"beats": TRUE_BEATS, "downbeats": TRUE_DOWNS}, open(bj, "w"))
    r = json.loads(subprocess.run([sys.executable, os.path.join(ROOT, "reel.py"), "snap", pj, "--beats", bj,
                                   "--mode", "downbeat", "--tolerance", "1.0", "--dry-run"],
                                  capture_output=True, text=True).stdout)
    assert r["ok"], r
    assert r["result"]["report"]["snapped"] == 2, r["result"]["report"]                # 3.4→4.3，5.4→6.3


@case
def snap_keeps_shots_valid():
    """推镜到 t1=3.0 的镜头不能被卡点缩到 3.0 以下：强拍 2.3 被挡掉，退到普通拍 3.3；
    全被挡掉时保持原位并报告 reason=constraint；require_match 时总长不变（否则 DURATION_MISMATCH）。"""
    from PIL import Image
    from reelcore import render
    d = os.path.join(TMP, "cons"); os.makedirs(d)
    for i in range(2):
        Image.new("RGB", (540, 960), (90, 60 * i, 120)).save(os.path.join(d, f"c{i}.png"))

    def mk(policy):
        return {"version": "reel.plan.v1", "src_dir": d, "output": {"width": 270, "height": 480, "fps": 30},
                "duration_policy": policy,
                "shots": [{"id": "s0", "asset": "c0.png", "duration": 3.2,
                           "camera": [{"op": "zoom_to", "bbox": [0.3, 0.3, 0.6, 0.6], "t0": 0.4, "t1": 3.0}]},
                          {"id": "s1", "asset": "c1.png", "duration": 20.8, "camera": []}],
                "audio": {"voice": WAV}, "look": {}}
    info = {"beats": TRUE_BEATS, "downbeats": TRUE_DOWNS}
    p, rep = B.snap_plan(mk("fit_last_shot"), info, "downbeat", tolerance=1.0)
    assert abs(p["shots"][0]["duration"] - 3.3) < 1e-6 and rep["cuts"][0]["level"] == "fallback", rep
    render.compile_plan(json.loads(json.dumps(p)))
    _, rep2 = B.snap_plan(mk("fit_last_shot"), {"beats": [2.3], "downbeats": [2.3]}, "downbeat", tolerance=1.0)
    assert rep2["cuts"][0].get("unsnapped") and rep2["cuts"][0].get("reason") == "constraint", rep2
    p3, _ = B.snap_plan(mk("require_match"), info, "downbeat", tolerance=1.0)
    assert abs(sum(s["duration"] for s in p3["shots"]) - 24.0) < 1e-6, p3["shots"]
    render.compile_plan(json.loads(json.dumps(p3)))


@case
def grid_modes():
    info = {"beats": [0.5, 1.0, 1.5, 2.0, 2.5, 3.0], "downbeats": [0.5, 2.5]}
    assert B.grid(info, "every:2") == [0.5, 1.5, 2.5]
    assert B.grid(info, "half")[:3] == [0.5, 0.75, 1.0]
    try:
        B.grid(info, "bogus"); raise AssertionError("未知模式应报错")
    except B.ReelError:
        pass


@case
def rendered_cuts_land_on_beats():
    from PIL import Image
    d = os.path.join(TMP, "mv"); os.makedirs(d)
    cols = [(220, 60, 60), (60, 160, 220), (240, 200, 40), (60, 190, 90), (150, 80, 200)]
    for i, c in enumerate(cols):
        Image.new("RGB", (540, 960), c).save(os.path.join(d, f"c{i}.png"))
    plan = {"version": "reel.plan.v1", "src_dir": ".", "output": {"width": 270, "height": 480, "fps": 30},
            "duration_policy": "fit_last_shot",
            "shots": [{"id": f"s{i}", "asset": f"c{i}.png", "duration": x, "camera": []}
                      for i, x in enumerate([3.3, 1.7, 2.6, 4.1, 12.3])],
            "audio": {"voice": WAV}, "look": {}}
    pj = os.path.join(d, "plan.json"); json.dump(plan, open(pj, "w"))
    bj = os.path.join(d, "beats.json"); json.dump(B.analyze(WAV, "librosa"), open(bj, "w"))
    cli = [sys.executable, os.path.join(ROOT, "reel.py")]
    r = json.loads(subprocess.run(cli + ["snap", pj, "--beats", bj, "--mode", "downbeat", "--tolerance", "1.0"],
                                  capture_output=True, text=True).stdout)
    assert r["ok"] and r["result"]["revision"], r
    out = os.path.join(d, "mv.mp4")
    r = json.loads(subprocess.run(cli + ["render", pj, "--out", out, "--where", "local"], capture_output=True,
                                  text=True).stdout)
    assert r["ok"], r
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-vf", "scale=8:8,format=rgb24", "-f", "rawvideo", "-"],
                         capture_output=True).stdout
    fr = np.frombuffer(raw, np.uint8).reshape(-1, 8, 8, 3).astype(float).mean(axis=(1, 2))
    cuts = [i / 30 for i in range(1, len(fr)) if np.abs(fr[i] - fr[i - 1]).sum() > 60]
    assert len(cuts) == 4, cuts
    for c in cuts:
        assert min(abs(c - b) for b in TRUE_BEATS) <= 1 / 30 + 1e-6, (c, cuts)          # 每个真实切点离真实拍 ≤1 帧
    # 时间线节拍轨：卡点后全部标为在拍上；把一个切点挪开 0.2s 后必须被标为不在拍上
    png = os.path.join(d, "tl.png")
    r = json.loads(subprocess.run(cli + ["timeline", pj, "--png", png, "--beats", bj], capture_output=True, text=True).stdout)
    beat = [t for t in json.load(open(r["result"]["sidecar"]))["tracks"] if t["track"] == "BEAT"][0]
    assert beat["on_beat"] == beat["total_cuts"] == 4, beat
    raw = json.load(open(pj)); raw["shots"][1]["duration"] += 0.2; json.dump(raw, open(pj, "w"))
    r = json.loads(subprocess.run(cli + ["timeline", pj, "--png", png, "--beats", bj], capture_output=True, text=True).stdout)
    beat = [t for t in json.load(open(r["result"]["sidecar"]))["tracks"] if t["track"] == "BEAT"][0]
    assert beat["on_beat"] < beat["total_cuts"], beat


if __name__ == "__main__":
    fails = [r for r in RESULTS if not r[1]]
    for name, ok, msg in RESULTS:
        print(("✅ " if ok else "❌ ") + name + ("" if ok else "\n   " + msg.replace("\n", "\n   ")))
    for name, why in SKIPPED:
        print(f"⏭️  {name} — SKIPPED: {why}")
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过" + (f"，{len(SKIPPED)} 项跳过（缺可选依赖）" if SKIPPED else ""))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
