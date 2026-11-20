#!/usr/bin/env python3
"""交付闸门故障注入矩阵（无需 pytest）：python3 tests/test_gate.py [--log FILE]

每个闸门都注入故障，证明它会拦；同时跑合格对照，证明不误拦。CLI 用子进程真跑（PATH/假 ffmpeg 才能注入）。
1 失败语义  2 技术检测  3 文本扫描  4 brief/声音/试听前置  5 deliver 回执
"""
import json, os, shutil, stat, subprocess, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
REEL = os.path.join(ROOT, "reel.py")
TMP = tempfile.mkdtemp(prefix="reel_gate_")
STATE = os.path.join(TMP, "state")
REAL_FFMPEG = shutil.which("ffmpeg")
RESULTS = []          # (组, 名字, ok, 说明)


def case(group):
    def deco(fn):
        try:
            note = fn() or ""
            RESULTS.append((group, fn.__name__, True, note))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((group, fn.__name__, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"))
        return fn
    return deco


def ff(*args):
    subprocess.run([REAL_FFMPEG, "-y", "-v", "error", *args], check=True)


def reel(*argv, env=None):
    e = {**os.environ, "REEL_STATE_DIR": STATE, **(env or {})}
    p = subprocess.run([sys.executable, REEL, *[str(x) for x in argv]], capture_output=True, text=True, env=e)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        raise AssertionError(f"stdout 不是 JSON（exit {p.returncode}）：{p.stdout[-300:]} {p.stderr[-500:]}")
    return out, p.returncode


def item(out, name):
    return next(x for x in out["result"]["items"] if x["check"] == name)


def blocked(out, code, expect_code=None):
    assert code != 0 and out["ok"] is False, f"应当被拦：exit={code} ok={out.get('ok')}"
    if expect_code:
        assert out["error"]["code"] == expect_code, out["error"]
    return out


def passed(out, code):
    assert code == 0 and out["ok"] is True, f"应当放行：exit={code} {json.dumps(out.get('error'), ensure_ascii=False)[:600]}" \
        + (f" blockers={out['result']['gate']['blockers']} pending={out['result']['gate']['pending_review']}"
           if out.get("result") and "gate" in out["result"] else "")
    return out


def P(name):
    return os.path.join(TMP, name)


def write(name, obj):
    with open(P(name), "w", encoding="utf-8") as fh:
        if isinstance(obj, str):
            fh.write(obj)
        else:
            json.dump(obj, fh, ensure_ascii=False, indent=1)
    return P(name)


# ---------- 素材 ----------
VOL_DEFAULT, VOL_STRICT = 2.44, 1.94        # 440 Hz 正弦 → 约 -14 / -16 LUFS（实测标定）


def make(name, dur=8, vol=VOL_DEFAULT, vf=None, af=None, vcodec="libx264", acodec="aac", ar=48000, audio=True,
         size="1080x1920", rate=30, extra=()):
    args = ["-f", "lavfi", "-i", f"testsrc2=s={size}:r={rate}:d={dur}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=f=440:d={dur}:sample_rate=48000"]
    fl = []
    if vf:
        fl += ["-vf", vf]
    if audio:
        fl += ["-af", f"volume={vol}" + (f",{af}" if af else "")]
    args += fl + ["-c:v", vcodec, "-pix_fmt", "yuv420p"] + (["-c:a", acodec, "-ar", str(ar)] if audio else []) \
        + list(extra) + ["-t", str(dur), P(name)]
    ff(*args)
    return P(name)


GOOD = make("good.mp4")
CLEAN_TXT = write("clean.txt", "第一句口播。\n第二句口播，内容完整。\n")
OK_TEXT = ["--text", CLEAN_TXT]


# ================= 1 失败语义 =================

@case("1 失败语义")
def control_good_clip_passes():
    out, code = reel("check", GOOD, *OK_TEXT)
    passed(out, code)
    assert out["result"]["gate"]["status"] == "pass"
    rb = make("good_strict.mp4", vol=VOL_STRICT)
    out2, code2 = reel("check", rb, *OK_TEXT, "--profile", "vertical-strict")
    passed(out2, code2)
    lu = item(out2, "loudness")["measured"]["integrated_lufs"]
    out3, code3 = reel("check", GOOD, *OK_TEXT, "--profile", "vertical-strict")
    blocked(out3, code3, "CHECK_FAILED")
    return (f"合格对照 default / vertical-strict（{lu} LUFS）exit 0；default 的合格片在 vertical-strict 下因响度 -14 被拦；"
            f"not_covered={out['result']['gate']['not_covered']}")


@case("1 失败语义")
def fail_item_blocks():
    bad = make("black_open.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,1)'")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code, "CHECK_FAILED")
    return f"开头 1 s 黑 → {out['error']['detail']['blockers']}"


@case("1 失败语义")
def missing_dependency_blocks():
    nobin = os.path.join(TMP, "empty-path")
    os.makedirs(nobin, exist_ok=True)
    out, code = reel("check", GOOD, *OK_TEXT, env={"PATH": nobin})
    blocked(out, code, "DEPENDENCY_MISSING")
    return f"PATH 里没有 ffmpeg → {out['error']['code']} {out['error']['detail']}"


SHIM = os.path.join(TMP, "shim")
os.makedirs(SHIM)
with open(os.path.join(SHIM, "ffmpeg"), "w") as fh:
    fh.write(f"""#!{sys.executable}
# 假 ffmpeg：按 REEL_FAULT 注入故障，其余透传给真 ffmpeg
import os, subprocess, sys
mode, _, key = os.environ.get("REEL_FAULT", "").partition(":")
args = sys.argv[1:]
hit = key and any(key in a for a in args)
if hit and mode == "exit":
    sys.stderr.write("injected failure\\n"); sys.exit(1)
p = subprocess.run([{REAL_FFMPEG!r}] + args, capture_output=True)
out, err = p.stdout, p.stderr
if hit and mode == "mangle":
    err = err.replace(b"black_start:", b"black_start=").replace(b"freeze_start:", b"freeze_start=")
if hit and mode == "nosummary":
    err = err.replace(b"Summary:", b"Summ4ry:")
if hit and mode == "notp":
    err = err.replace(b"True peak:", b"True p3ak:")
if hit and mode == "noprogress":
    out = out.replace(b"progress=end", b"progress=continue")
sys.stdout.buffer.write(out); sys.stderr.buffer.write(err); sys.exit(p.returncode)
""")
os.chmod(os.path.join(SHIM, "ffmpeg"), 0o755)
SHIM_ENV = {"PATH": SHIM + os.pathsep + os.environ["PATH"]}


def shim(fault):
    return {**SHIM_ENV, "REEL_FAULT": fault}


@case("1 失败语义")
def shim_passthrough_is_clean():
    out, code = reel("check", GOOD, *OK_TEXT, env=shim(""))
    passed(out, code)
    return "假 ffmpeg 不注入时照常放行（证明下面的拦截来自注入）"


@case("1 失败语义")
def process_nonzero_blocks():
    out, code = reel("check", GOOD, *OK_TEXT, env=shim("exit:ebur128"))
    blocked(out, code, "CHECK_FAILED")
    it = item(out, "loudness")
    assert it["status"] == "not_checked" and it["error"]["code"] == "FFMPEG_FAILED", it
    return "ebur128 进程退出 1 → loudness not_checked(FFMPEG_FAILED) → 阻断"


@case("1 失败语义")
def unparsable_events_block():
    # 片中第 3 秒有 6 帧黑：不篡改时 black_frames fail；事件行格式被改时不能退化成「没有黑帧 = pass」
    bad = make("bf_mid.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(n,90,95)'")
    out1, _ = reel("check", bad, *OK_TEXT, env=shim(""))
    assert item(out1, "black_frames")["status"] == "fail", item(out1, "black_frames")
    out2, code2 = reel("check", bad, *OK_TEXT, env=shim("mangle:blackdetect"))
    blocked(out2, code2, "CHECK_FAILED")
    it = item(out2, "black_frames")
    assert it["status"] == "not_checked" and it["error"]["code"] == "OUTPUT_UNPARSABLE", it
    fz = make("fz_mid.mp4", dur=10, vf="loop=loop=179:size=1:start=30")
    out3, code3 = reel("check", fz, *OK_TEXT, env=shim("mangle:freezedetect"))
    blocked(out3, code3, "CHECK_FAILED")
    assert item(out3, "long_freeze")["error"]["code"] == "OUTPUT_UNPARSABLE", item(out3, "long_freeze")
    return "有黑帧/定格的片，事件行格式被改 → OUTPUT_UNPARSABLE 阻断，不会当成「没有事件」"


@case("1 失败语义")
def unparsable_summary_and_truncated_run_block():
    out, code = reel("check", GOOD, *OK_TEXT, env=shim("nosummary:ebur128"))
    blocked(out, code, "CHECK_FAILED")
    assert item(out, "loudness")["error"]["code"] == "OUTPUT_UNPARSABLE"
    out, code = reel("check", GOOD, *OK_TEXT, env=shim("noprogress:freezedetect"))
    blocked(out, code, "CHECK_FAILED")
    assert item(out, "freeze")["error"]["code"] == "OUTPUT_UNPARSABLE"
    out, code = reel("check", GOOD, *OK_TEXT, env=shim("notp:ebur128"))
    blocked(out, code, "CHECK_FAILED")
    assert item(out, "loudness")["error"]["code"] == "OUTPUT_UNPARSABLE", item(out, "loudness")
    return "ebur128 无 Summary、Summary 缺 True peak、freezedetect 没跑到片尾 → 都阻断"


@case("1 失败语义")
def required_check_missing_blocks():
    prof = write("prof_need_subs.json", {"extends": "default", "required": ["decode_clean", "subtitles_in_range"]})
    out, code = reel("check", GOOD, *OK_TEXT, "--profile", prof)
    blocked(out, code, "CHECK_FAILED")
    prof2 = write("prof_need_unknown.json", {"extends": "default", "required": ["decode_clean", "lip_sync"]})
    out2, code2 = reel("check", GOOD, *OK_TEXT, "--profile", prof2)
    blocked(out2, code2, "CHECK_FAILED")
    whys = [b["why"] for b in out2["result"]["gate"]["blockers"]]
    assert any("漏跑" in w for w in whys), whys
    out3, code3 = reel("check", GOOD)
    blocked(out3, code3, "CHECK_FAILED")
    assert out3["result"]["gate"]["blockers"][0]["check"] == "text_scan"
    return "必查项 not_checked（无 plan 的字幕）/ 没有结果（lip_sync）/ 文本扫描没输入 → 都阻断"


def needs_review_clip():
    return make("quiet.mp4", vol=0.6)       # 约 -26 LUFS：只触发 loudness needs_review


def rec(video_sha, check, decision="accept", by="user", note="看过，整体偏轻但可接受", **kw):
    r = {"video_sha256": video_sha, "check": check, "decision": decision, "by": by, "note": note,
         "observations": [{"t": [0, 8], "note": "全程偏轻，对白清楚"}]}
    r.update(kw)
    return r


def sha(path):
    from reelcore import check
    return check.sha256(path)


@case("1 失败语义")
def unhandled_review_blocks_until_recorded():
    q = needs_review_clip()
    out, code = reel("check", q, *OK_TEXT)
    blocked(out, code, "CHECK_NEEDS_REVIEW")
    assert out["result"]["gate"]["pending_review"] == ["loudness"], out["result"]["gate"]
    good_rec = write("rev_ok.json", {"version": "reel.reviews.v1", "records": [rec(sha(q), "loudness")]})
    out, code = reel("check", q, *OK_TEXT, "--reviews", good_rec)
    passed(out, code)
    other = write("rev_other.json", {"records": [rec(sha(GOOD), "loudness")]})
    out, code = reel("check", q, *OK_TEXT, "--reviews", other)
    blocked(out, code, "CHECK_NEEDS_REVIEW")
    assert out["result"]["gate"]["stale_reviews"], out["result"]["gate"]
    empty = write("rev_empty.json", {"records": [{"video_sha256": sha(q), "check": "loudness", "decision": "accept",
                                                  "by": "", "note": ""}]})
    out, code = reel("check", q, *OK_TEXT, "--reviews", empty)
    blocked(out, code, "CHECK_NEEDS_REVIEW")
    assert out["result"]["gate"]["invalid_reviews"], out["result"]["gate"]
    rej = write("rev_rej.json", {"records": [rec(sha(q), "loudness", decision="reject", note="太轻，听不清口播")]})
    out, code = reel("check", q, *OK_TEXT, "--reviews", rej)
    blocked(out, code, "CHECK_FAILED")
    return "未处理 REVIEW / 别的文件的记录 / 空记录 → 拦；判定不通过 → FAIL；有效记录 → 放行"


# ================= 2 技术检测 =================

@case("2 技术检测")
def single_black_first_frame_fails():
    bad = make("bf1.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='eq(n,0)'")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code)
    assert item(out, "first_frame")["status"] == "fail", item(out, "first_frame")
    return "只有第 0 帧黑 → first_frame fail（blackdetect 的最短时长抓不到单帧，first_frame 直接解第 0 帧）"


@case("2 技术检测")
def low_motion_tolerates_only_isolated_jitter():
    from reelcore.check import _low_runs
    c = {"diff_max": 0.8, "spike_max": 0.9, "window_s": 4.0}
    base = [0.4] * 14 + [None] + [0.5] * 3 + [90.0]           # 4.25 s 慢推，第 15 个样本是抖动
    seq = lambda *spike: base[:14] + list(spike) + base[14 + len(spike):]
    assert len(_low_runs(seq(0.82), c, 4)) == 1, "0.82 的孤立抖动（ffmpeg 6.1 实测）不应打断慢推段"
    assert not _low_runs(seq(0.95), c, 4), "超过 spike_max 的样本必须打断"
    assert not _low_runs(seq(0.82, 0.82), c, 4), "连续两个超阈值样本必须打断"
    assert not _low_runs(seq(0.82), {**c, "spike_max": 0.8}, 4), "spike_max=diff_max 时退回原规则"
    return "孤立 0.82 抖动不打断 4.25 s 慢推段；0.95 或连续两个 0.82 → 打断；spike_max=diff_max 退回原规则"


@case("2 技术检测")
def head_silence_fails():
    bad = make("head4.mp4", af="volume=enable='lt(t,4)':volume=0")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code)
    it = item(out, "head_silence")
    assert it["status"] == "fail" and it["measured"]["head_silence_s"] >= 3.9, it
    return f"开头 4 s 无声 → head_silence fail（{it['measured']}）"


@case("2 技术检测")
def tail_silence_fails_by_profile():
    bad = make("tail3.mp4", af="volume=enable='gte(t,5)':volume=0")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code, "CHECK_FAILED")
    assert item(out, "tail_silence")["status"] == "fail"
    rb = make("tail3_strict.mp4", vol=VOL_STRICT, af="volume=enable='gte(t,5)':volume=0")
    out2, code2 = reel("check", rb, *OK_TEXT, "--profile", "vertical-strict")
    blocked(out2, code2)
    assert item(out2, "tail_silence")["status"] == "needs_review", item(out2, "tail_silence")
    return "片尾 3 s 无声 → default fail / vertical-strict needs_review"


@case("2 技术检测")
def long_freeze_fails():
    bad = make("freeze6.mp4", dur=10, vf="loop=loop=179:size=1:start=30")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code, "CHECK_FAILED")
    it = item(out, "long_freeze")
    assert it["status"] == "fail", it
    return f"定格 6 s → long_freeze fail {it['evidence']}"


@case("2 技术检测")
def wrong_codec_and_samplerate_fail():
    hv = make("mpeg4.mp4", vcodec="mpeg4", extra=["-q:v", "3"])
    out, code = reel("check", hv, *OK_TEXT)
    blocked(out, code)
    assert item(out, "format")["status"] == "fail"
    sr = make("sr44.mp4", ar=44100)
    out2, code2 = reel("check", sr, *OK_TEXT)
    blocked(out2, code2)
    f2 = item(out2, "format")
    assert f2["status"] == "fail" and f2["measured"]["sample_rate"] == 44100, f2
    rb = make("strict_wrongsize.mp4", vol=VOL_STRICT, size="720x1280")
    out3, code3 = reel("check", rb, *OK_TEXT, "--profile", "vertical-strict")
    blocked(out3, code3)
    assert item(out3, "format")["status"] == "fail"
    return "mpeg4 视频 / 44.1k 采样 / vertical-strict 下 720×1280 → format fail"


@case("2 技术检测")
def corrupted_video_blocks():
    src = open(GOOD, "rb").read()
    mid = bytearray(src)
    n = len(mid)
    for k in range(n // 3, n // 3 + 200000, 7):
        mid[k] = (mid[k] * 31 + 17) % 256
    bad = P("corrupt_mid.mp4")
    open(bad, "wb").write(bytes(mid))
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code)
    trunc = P("corrupt_trunc.mp4")
    open(trunc, "wb").write(src[: n // 2])
    out2, code2 = reel("check", trunc, *OK_TEXT)
    blocked(out2, code2)
    return (f"中段字节损坏 → {out['error']['code']} decode_clean={item(out, 'decode_clean')['status']}；"
            f"截断一半 → {out2['error']['code']}")


@case("2 技术检测")
def legal_silent_version_passes():
    prof = write("prof_silent.json", {"extends": "default", "audio": "silent"})
    noaud = make("silent_noaudio.mp4", audio=False)
    passed(*reel("check", noaud, *OK_TEXT, "--profile", prof))
    mute = make("silent_mute.mp4", vol=0)
    passed(*reel("check", mute, *OK_TEXT, "--profile", prof))
    out, code = reel("check", mute, *OK_TEXT)
    blocked(out, code)
    loud = make("silent_but_loud.mp4")
    out2, code2 = reel("check", loud, *OK_TEXT, "--profile", prof)
    blocked(out2, code2)
    assert item(out2, "loudness")["status"] == "fail"
    return "静音版 profile：无音轨 / 全静音 → 放行；同一片用 default → 拦；静音版却有声 → 拦"


@case("2 技术检测")
def registered_exception_passes():
    bad = make("freeze6b.mp4", dur=10, vf="loop=loop=179:size=1:start=30")
    prof = write("prof_allow.json", {"extends": "default", "allow": [
        {"check": c, "t": [0.9, 7.1], "reason": "片头定格是设计好的标题停留"} for c in ("long_freeze", "freeze", "low_motion")]})
    out, code = reel("check", bad, *OK_TEXT, "--profile", prof)
    passed(out, code)
    assert item(out, "long_freeze")["excused"]["from"] == "fail"
    short = write("prof_allow_short.json", {"extends": "default", "allow": [
        {"check": c, "t": [0.9, 4.0], "reason": "只登记了前半段"} for c in ("long_freeze", "freeze", "low_motion")]})
    out2, code2 = reel("check", bad, *OK_TEXT, "--profile", short)
    blocked(out2, code2)
    noreason = write("prof_allow_noreason.json", {"extends": "default", "allow": [
        {"check": "long_freeze", "t": [0, 10], "reason": ""}]})
    out3, code3 = reel("check", bad, *OK_TEXT, "--profile", noreason)
    blocked(out3, code3, "PROFILE_INVALID")
    return "登记例外覆盖整段 → 放行；只覆盖一半 → 拦；例外没写理由 → PROFILE_INVALID"


# ================= 3 文本扫描 =================
PROJ = os.path.join(TMP, "proj")
os.makedirs(PROJ)


def render_plan(subs_text):
    """真渲一个小 plan（运动视频两镜 + 正弦配音 + 字幕），返回 (plan 路径, 成片)。"""
    from reelcore import render
    src = os.path.join(PROJ, "src")
    os.makedirs(src, exist_ok=True)
    mv = os.path.join(src, "moving.mp4")
    if not os.path.isfile(mv):
        ff("-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=4", "-pix_fmt", "yuv420p", mv)
        ff("-f", "lavfi", "-i", "sine=f=440:d=4.0:sample_rate=48000", "-af", f"volume={VOL_DEFAULT}", "-ar", "48000",
           os.path.join(src, "voice.wav"))
    with open(os.path.join(src, "v.vtt"), "w", encoding="utf-8") as fh:
        fh.write(subs_text)
    plan = {"version": "reel.plan.v1", "src_dir": "src", "output": {"width": 540, "height": 960, "fps": 30},
            "duration_policy": "require_match",
            "shots": [{"id": "s1", "asset": "moving.mp4", "duration": 2.0, "camera": []},
                      {"id": "s2", "asset": "moving.mp4", "source_in": 2.0, "duration": 2.0, "camera": []}],
            "audio": {"voice": "voice.wav", "subtitles": "v.vtt"}, "look": {}}
    pp = os.path.join(PROJ, "plan.json")
    with open(pp, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)
    out = os.path.join(PROJ, "cut.mp4")
    if not os.path.isfile(out):
        r = render.render({**plan, "src_dir": src}, out, "cpu", log=lambda *a: None)
        assert r["ok"], r
    return pp, out


VTT_OK = "WEBVTT\n\n00:00.000 --> 00:02.000\n第一句口播。\n\n00:02.000 --> 00:04.000\n第二句口播。\n"
VTT_BAD = "WEBVTT\n\n00:00.000 --> 00:02.000\n第一句口播。\n\n00:02.000 --> 00:04.000\n价格待定，敬请期待。\n"


def scan_hits(out):
    return [(e.get("kind"), e.get("word"), bool(e.get("missing"))) for e in item(out, "text_scan")["evidence"]]


@case("3 文本扫描")
def control_plan_with_clean_subtitles_passes():
    pp, cut = render_plan(VTT_OK)
    out, code = reel("check", cut, "--plan", pp)
    passed(out, code)
    ts = item(out, "text_scan")
    assert "subtitles" in ts["measured"]["kinds"] and "plan" in ts["measured"]["kinds"], ts
    return f"真渲的 plan + 干净字幕 → 放行；扫描了 {ts['measured']['kinds']}"


@case("3 文本扫描")
def banned_in_plan_subtitles_blocks():
    pp, cut = render_plan(VTT_BAD)
    out, code = reel("check", cut, "--plan", pp)
    blocked(out, code, "CHECK_FAILED")
    assert ("subtitles", "待定", False) in scan_hits(out), scan_hits(out)
    render_plan(VTT_OK)
    return "plan 字幕里有「待定」→ 阻断"


@case("3 文本扫描")
def banned_in_extra_subs_text_and_brief_block():
    srt = write("extra.srt", "1\n00:00:00,000 --> 00:00:02,000\nTODO 换成正式文案\n")
    out, code = reel("check", GOOD, *OK_TEXT, "--subs", srt)
    blocked(out, code, "CHECK_FAILED")
    assert ("subtitles", "TODO", False) in scan_hits(out), scan_hits(out)
    txt = write("cover.txt", "封面标题：Lorem ipsum dolor\n")
    out, code = reel("check", GOOD, "--text", txt)
    blocked(out, code, "CHECK_FAILED")
    assert ("text", "lorem ipsum", False) in scan_hits(out), scan_hits(out)
    br = write("brief_bad.json", {"version": "reel.brief.v1", "name": "x", "language": "待确认"})
    out, code = reel("check", GOOD, *OK_TEXT, "--brief", br)
    blocked(out, code, "CHECK_FAILED")
    assert ("brief", "待确认", False) in scan_hits(out), scan_hits(out)
    return "--subs 里 TODO / --text 里 lorem ipsum / brief 里「待确认」→ 各自阻断"


@case("3 文本扫描")
def banned_in_plan_visible_text_is_found():
    from reelcore import profiles, textscan
    plan = {"shots": [{"id": "w1", "kind": "web", "web": {"url": "a.html"},
                       "camera": [{"op": "zoom_to", "text": "正文第一段"}],
                       "highlight": [{"text": "报名费 TBD", "sync": "voice"}]}]}
    r = textscan.scan(textscan.collect(plan=plan), profiles.load())
    assert r["status"] == "fail" and any(e["kind"] == "plan" and e["word"] == "TBD" and "highlight" in e["at"]
                                         for e in r["evidence"]), r
    plan["shots"][0]["highlight"][0]["text"] = "报名费 300 元"
    r = textscan.scan(textscan.collect(plan=plan), profiles.load())
    assert r["status"] == "pass", r
    return "plan 网页镜头的 highlight 文字里有 TBD → fail；改掉 → pass（模块层，网页镜头不在这里真渲）"


@case("3 文本扫描")
def missing_required_text_blocks():
    out, code = reel("check", GOOD, *OK_TEXT, "--text", P("no_such_caption.txt"))
    blocked(out, code, "CHECK_FAILED")
    assert ("text", None, True) in scan_hits(out), scan_hits(out)
    out, code = reel("check", GOOD, *OK_TEXT, "--subs", P("deleted.vtt"))
    blocked(out, code, "CHECK_FAILED")
    out, code = reel("check", GOOD, *OK_TEXT, "--brief", P("no_brief.json"))
    blocked(out, code, "CHECK_FAILED")
    prof = write("prof_need_subs_text.json", {"extends": "default", "text": {"require": ["subtitles"]}})
    out, code = reel("check", GOOD, *OK_TEXT, "--profile", prof)
    blocked(out, code, "CHECK_FAILED")
    assert ("subtitles", None, True) in scan_hits(out), scan_hits(out)
    pp, cut = render_plan(VTT_OK)
    os.unlink(os.path.join(PROJ, "src", "v.vtt"))
    out, code = reel("check", cut, "--plan", pp)
    blocked(out, code)
    render_plan(VTT_OK)
    return (f"指定的文本/字幕/brief 文件不存在、profile 要求字幕却没有、plan 的字幕被删（{out['error']['code']}）→ 都阻断")


@case("3 文本扫描")
def allowed_phrase_passes_old_name_blocks():
    prof = write("prof_oldname.json", {"extends": "default",
                                       "text": {"extra_banned": ["Acme Edu"], "allow": ["Acme Education"]}})
    ok_txt = write("name_ok.txt", "Acme Education 招生说明会\n")
    passed(*reel("check", GOOD, "--text", ok_txt, "--profile", prof))
    bad_txt = write("name_bad.txt", "Acme Edu 招生说明会\n")
    out, code = reel("check", GOOD, "--text", bad_txt, "--profile", prof)
    blocked(out, code, "CHECK_FAILED")
    both = write("name_both.txt", "Acme Education 与 Acme Edu\n")
    blocked(*reel("check", GOOD, "--text", both, "--profile", prof))
    return "旧名 Acme Edu 禁用、Acme Education 放行；同一行两者都有 → 仍拦"


@case("3 文本扫描")
def ocr_is_reported_not_covered():
    out, code = reel("check", GOOD, *OK_TEXT)
    passed(out, code)
    o = item(out, "ocr_visible_text")
    assert o["status"] == "not_checked" and "OCR" in o["rule"], o
    assert "ocr_visible_text" in out["result"]["gate"]["not_covered"]
    assert "只覆盖输入文本" in item(out, "text_scan")["rule"]
    return "画面文字没有 OCR：ocr_visible_text 固定 not_checked，列在 gate.not_covered"


# ================= 4 brief / 声音审样 / 试听前置；5 deliver 回执 =================

def card_push_clip():
    """4.5 s 字卡 1.00→1.06 慢推 + 3 s 实拍：定格检测抓不到，只有 low_motion 会报。"""
    out = P("cardpush.mp4")
    if os.path.isfile(out):
        return out
    boxes = ",".join(f"drawbox=x=140:y={700 + k * 110}:w={801 - k * 90}:h=61:color=0xEBEBEB:t=fill" for k in range(5))
    ff("-f", "lavfi", "-i", "color=c=0x1D2A44:s=1080x1920", "-vf", boxes + ",format=rgb24", "-frames:v", "1", P("card.png"))
    ff("-loop", "1", "-i", P("card.png"), "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=3",
       "-f", "lavfi", "-i", "sine=f=440:d=7.5:sample_rate=48000", "-filter_complex",
       "[0:v]scale=2160:3840,zoompan=z='1+0.06*on/135':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=135:s=1080x1920:fps=30,"
       "trim=end_frame=135,setsar=1[c];[1:v]setsar=1[m];[c][m]concat=n=2:v=1[v];[2:a]volume=2.44[a]",
       "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", out)
    return out


def project(name, drop=(), **over):
    """一个登记任务：项目目录 + 交付目录 + 已批准的 TTS 声音 + brief。"""
    pdir = os.path.join(TMP, name)
    root = os.path.join(TMP, name + "-交付")
    os.makedirs(pdir, exist_ok=True)
    os.makedirs(root, exist_ok=True)
    sample = os.path.join(pdir, "voice_sample.wav")
    if not os.path.isfile(sample):
        ff("-f", "lavfi", "-i", "sine=f=330:d=1", sample)
    b = {"version": "reel.brief.v1", "name": "测试片", "language": "zh-CN", "deliverable": "final",
         "delivery_root": root, "must_not_change": ["片尾署名"],
         "voice": {"mode": "tts", "voice_id": "zh-CN-XiaoxiaoNeural",
                   "approved": {"voice_id": "zh-CN-XiaoxiaoNeural", "by": "user", "quote": "这个声音可以",
                                "sample": "voice_sample.wav", "sample_sha256": sha(sample)}}}
    for k, v in over.items():
        b[k] = v
    for k in drop:
        b.pop(k, None)
    bp = os.path.join(pdir, "brief.json")
    with open(bp, "w", encoding="utf-8") as fh:
        json.dump(b, fh, ensure_ascii=False)
    return bp, root, pdir


def final_reviews(video, name, extra=(), watch=None, listen=None):
    s = sha(video)
    dw = {"video_sha256": s, "check": "final_watch", "decision": "accept", "by": "claude",
          "note": "完整看完一遍，逐段对过分镜",
          "observations": [{"t": 0, "note": "首帧有画面"}, {"t": [3, 5], "note": "中段正常"}]}
    dl = {"video_sha256": s, "check": "final_listen", "decision": "accept", "by": "user",
          "note": "从头听完一遍", "quote": "声音没问题，可以发"}
    recs = [dw if watch is None else watch, dl if listen is None else listen] + list(extra)   # 传 {} = 不要这条
    return write(name, {"version": "reel.reviews.v1", "records": [r for r in recs if r]})


def receipts(pdir):
    d = os.path.join(pdir, ".reel", "receipts")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def deliver(video, bp, version, *extra):
    return reel("deliver", video, "--brief", bp, "--version", version, *OK_TEXT, *extra)


@case("4 brief 与试听前置")
def control_full_final_delivery():
    bp, root, pdir = project("p_ok")
    rv = final_reviews(GOOD, "rv_ok.json")
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    passed(out, code)
    r = out["result"]
    assert r["status"] == "final" and os.path.isfile(os.path.join(root, "测试片-v1.mp4")), r
    return f"brief 齐全 + 声音已批准 + 看片/试听记录 → 成片 {os.path.basename(r['delivered'])}"


@case("4 brief 与试听前置")
def missing_brief_fields_only_sample():
    rv = final_reviews(GOOD, "rv_fields.json")
    notes = []
    for field in ("language", "deliverable", "must_not_change"):
        bp, root, pdir = project(f"p_no_{field}", drop=(field,))
        out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
        blocked(out, code, "DELIVERY_NOT_FINAL")
        assert not receipts(pdir) and not os.listdir(root), (receipts(pdir), os.listdir(root))
        out, code = deliver(GOOD, bp, "v1", "--reviews", rv, "--sample")
        passed(out, code)
        assert out["result"]["status"] == "sample_pending_review" and "sample-pending-review" in out["result"]["delivered"]
        notes.append(field)
    bp, root, pdir = project("p_no_root", drop=("delivery_root",))
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv, "--sample")
    blocked(out, code, "BRIEF_INVALID")
    bp, root, pdir = project("p_broad_root", delivery_root="~")
    blocked(*deliver(GOOD, bp, "v1", "--reviews", rv, "--sample"), "BRIEF_INVALID")
    return f"缺 {notes} → 拒绝成片、--sample 可交待审样片；缺交付目录或目录太宽（~）→ 样片也不行"


@case("4 brief 与试听前置")
def unapproved_voice_only_sample():
    rv = final_reviews(GOOD, "rv_voice.json")
    bad_voices = {
        "no_approval": {"mode": "tts", "voice_id": "zh-CN-YunxiNeural"},
        "other_voice": None,
        "agent_approved": None,
        "sample_swapped": None,
    }
    whys = []
    for tag in bad_voices:
        bp, root, pdir = project(f"p_v_{tag}")
        b = json.load(open(bp, encoding="utf-8"))
        if tag == "no_approval":
            b["voice"] = bad_voices[tag]
        elif tag == "other_voice":
            b["voice"]["voice_id"] = "zh-CN-YunxiNeural"
        elif tag == "agent_approved":
            b["voice"]["approved"]["by"] = "claude"
        elif tag == "sample_swapped":
            ff("-f", "lavfi", "-i", "sine=f=500:d=1", os.path.join(pdir, "voice_sample.wav"))
        json.dump(b, open(bp, "w", encoding="utf-8"), ensure_ascii=False)
        out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
        blocked(out, code, "DELIVERY_NOT_FINAL")
        whys.append(tag)
    return f"未批准 / 批的是别的声音 / agent 自己批但没有 STT 证据 / 试听样本被换 → 都拒绝成片（{whys}）"


SCRIPT = "今天我们来看一看这座城市的早晨，街口的早餐铺已经开门，豆浆刚刚煮好。"
try:                       # 中文按拼音比要 pypinyin（./install.sh --zh-voice）；没装时整组改用英文样本，闸门逻辑相同
    import pypinyin  # noqa: F401
    STT_LANG, STT_SCRIPT = "zh", SCRIPT
    STT_OK = SCRIPT.replace("，", " ").replace("我们", "我门")
    STT_WRONG = "今天我们来看一看这座村庄的傍晚，路口的面馆早就关门，米酒还没酿好。"
    STT_OTHER, STT_SHORT = "完全不同的一段话", "你好"
except ImportError:
    STT_LANG = "en"
    STT_SCRIPT = "This morning we walk through the old town while the corner bakery opens its doors."
    STT_OK = STT_SCRIPT.replace("bakery", "bakary")
    STT_WRONG = "Tonight we drive across the quiet village after the riverside restaurant has closed."
    STT_OTHER, STT_SHORT = "a completely different sentence", "Hello"


@case("4 brief 与试听前置")
def agent_voice_approval_needs_stt():
    rv = final_reviews(GOOD, "rv_vstt.json")
    def with_stt(tag, **stt_over):
        bp, root, pdir = project(f"p_vstt_{tag}")
        b = json.load(open(bp, encoding="utf-8"))
        ap = b["voice"]["approved"]
        ap.pop("quote"); ap["by"] = "claude"
        ap["stt"] = {"engine": "whisper-cli ggml-large-v3-turbo.bin", "lang": STT_LANG, "script": STT_SCRIPT,
                     "transcript": STT_OK, "err": 0.0, "sample_sha256": ap["sample_sha256"]}
        ap["stt"].update(stt_over)
        json.dump(b, open(bp, "w", encoding="utf-8"), ensure_ascii=False)
        return deliver(GOOD, bp, "v1", "--reviews", rv)
    whys = []
    for tag, over in {"wrong_words": {"transcript": STT_WRONG},
                      "self_reported_err": {"transcript": STT_OTHER, "err": 0.0},
                      "other_sample": {"sample_sha256": "0" * 64},
                      "short_script": {"script": STT_SHORT, "transcript": STT_SHORT},
                      "no_engine": {"engine": ""}}.items():
        out, code = with_stt(tag, **over)
        blocked(out, code, "DELIVERY_NOT_FINAL")
        whys.append(tag)
    out, code = with_stt("ok")
    passed(out, code)
    assert out["result"]["status"] == "final", out["result"]
    return (f"[{STT_LANG}] agent 带 STT 证据（转写只差一处近音错字）→ 成片；转写对不上 / 自报 0 但重算不过 / 转写的不是这份样本 / "
            f"文本太短 / 没写引擎 → 拒绝（{whys}）")


@case("4 brief 与试听前置")
def voice_check_cli_writes_approval():
    bp, root, pdir = project("p_vcli")
    b = json.load(open(bp, encoding="utf-8")); b["voice"].pop("approved")
    json.dump(b, open(bp, "w", encoding="utf-8"), ensure_ascii=False)
    sp = os.path.join(pdir, "voice_sample.wav")
    script = os.path.join(pdir, "script.txt")
    open(script, "w", encoding="utf-8").write(SCRIPT)
    out, code = reel("voice", "check", sp, "--script", script, "--brief", bp, env={"REEL_WHISPER_MODEL": "/nonexistent.bin"})
    blocked(out, code, "BACKEND_UNAVAILABLE")
    model = os.environ.get("REEL_WHISPER_MODEL") or os.path.expanduser("~/models/whisper/ggml-large-v3-turbo.bin")
    if not (shutil.which("say") and shutil.which("whisper-cli") and os.path.isfile(model) and STT_LANG == "zh"):
        return "SKIPPED 真转写（缺 say / whisper-cli / 模型 / pypinyin 之一）：只验了缺模型时报 BACKEND_UNAVAILABLE"
    aiff = os.path.join(pdir, "say.aiff")
    subprocess.run(["say", "-v", "Tingting", "-o", aiff, SCRIPT], check=True)
    ff("-i", aiff, "-ar", "48000", sp)
    other = os.path.join(pdir, "other.txt")
    open(other, "w", encoding="utf-8").write("这段文字和样本里念的内容完全不一样，用来证明字错率闸门会拦下来。")
    out, code = reel("voice", "check", sp, "--script", other, "--brief", bp)
    blocked(out, code, "VOICE_CHECK_FAILED")
    assert "approved" not in json.load(open(bp, encoding="utf-8"))["voice"]
    out, code = reel("voice", "check", sp, "--script", script, "--brief", bp)
    passed(out, code)
    err = out["result"]["err"]
    ap = json.load(open(bp, encoding="utf-8"))["voice"]["approved"]
    assert ap["by"] == "claude" and ap["stt"]["sample_sha256"] == sha(sp), ap
    out, code = deliver(GOOD, bp, "v1", "--reviews", final_reviews(GOOD, "rv_vcli.json"))
    passed(out, code)
    assert out["result"]["status"] == "final", out["result"]
    return f"真转写（say 念 → whisper）：对的文本拼音错误率 {err:.0%} → 写入 brief，成片可交；换一段文本 → VOICE_CHECK_FAILED，brief 不写；缺模型 → BACKEND_UNAVAILABLE"


@case("4 brief 与试听前置")
def final_listen_agent_needs_observations():
    bp, root, pdir = project("p_listen")
    s = sha(GOOD)
    no_listen = final_reviews(GOOD, "rv_nolisten.json", listen={})
    blocked(*deliver(GOOD, bp, "v1", "--reviews", no_listen), "DELIVERY_NOT_FINAL")
    agent_listen_bare = final_reviews(GOOD, "rv_agentlisten.json", listen={
        "video_sha256": s, "check": "final_listen", "decision": "accept", "by": "claude-agent",
        "note": "响度达标，应该没问题", "quote": "ok"})
    out, code = deliver(GOOD, bp, "v1", "--reviews", agent_listen_bare)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    no_watch = final_reviews(GOOD, "rv_nowatch.json", watch={})
    blocked(*deliver(GOOD, bp, "v1", "--reviews", no_watch), "DELIVERY_NOT_FINAL")
    assert not receipts(pdir)
    agent_listen = final_reviews(GOOD, "rv_agentlisten2.json", listen={
        "video_sha256": s, "check": "final_listen", "decision": "accept", "by": "claude",
        "note": "从头听完，逐段对过电平和接缝",
        "observations": [{"t": [0, 2], "note": "开头无爆音"}, {"t": [4, 6], "note": "结尾自然收"}]})
    out, code = deliver(GOOD, bp, "v1", "--reviews", agent_listen)
    passed(out, code)
    assert out["result"]["status"] == "final" and out["result"]["approved_by"] == "agent", out["result"]
    assert "用户还没看" in out["result"]["statement"], out["result"]["statement"]
    bp2, _, pdir2 = project("p_listen_human")
    out, code = deliver(GOOD, bp2, "v1", "--reviews", final_reviews(GOOD, "rv_humanlisten.json"))
    passed(out, code)
    assert out["result"]["approved_by"] == "human", out["result"]
    return "没有试听记录 / agent 只写一句没带时间码观察 / 没有完整看片记录 → 拒绝成片；agent 带时间码听完 → 成片，approved_by=agent；人记原话 → approved_by=human"


@case("4 brief 与试听前置")
def long_card_slow_push_only_sample_until_reviewed():
    cp = card_push_clip()
    bp, root, pdir = project("p_card")
    rv = final_reviews(cp, "rv_card.json")
    out, code = deliver(cp, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert any("low_motion" in x for x in out["error"]["detail"]["final_blockers"]), out["error"]["detail"]
    out, code = deliver(cp, bp, "v1", "--reviews", rv, "--sample")
    passed(out, code)
    rv2 = final_reviews(cp, "rv_card2.json", extra=[{
        "video_sha256": sha(cp), "check": "low_motion", "decision": "accept", "by": "user",
        "note": "片头标题停留是设计好的", "observations": [{"t": [0, 4.25], "note": "标题卡慢推，用户确认保留"}]}])
    out, code = deliver(cp, bp, "v2", "--reviews", rv2)
    passed(out, code)
    return "4.5 s 字卡慢推 → 只能交 v1 待审样片；逐项审片记录处理 low_motion 后 → v2 成片"


@case("4 brief 与试听前置")
def brief_profile_applies_to_check_and_deliver():
    bp, root, pdir = project("p_prof", profile="proj-profile.json")
    with open(os.path.join(pdir, "proj-profile.json"), "w", encoding="utf-8") as fh:
        json.dump({"extends": "vertical-strict"}, fh)
    rv = final_reviews(GOOD, "rv_prof.json")
    out, code = reel("check", GOOD, *OK_TEXT, "--brief", bp)
    blocked(out, code, "CHECK_FAILED")
    assert out["result"]["profile"]["name"] == "vertical-strict", out["result"]["profile"]
    blocked(*deliver(GOOD, bp, "v1", "--reviews", rv), "DELIVERY_BLOCKED")
    passed(*reel("check", GOOD, *OK_TEXT, "--brief", bp, "--profile", "default"))
    return "brief.profile（相对 brief 目录的项目 profile → vertical-strict）同时作用于 check 和 deliver；--profile 可覆盖"


@case("5 deliver 回执")
def receipt_binds_hashes_and_makes_no_viewing_claim():
    bp, root, pdir = project("p_rcpt")
    rv = final_reviews(GOOD, "rv_rcpt.json")
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    passed(out, code)
    r = out["result"]
    assert r["delivered_sha256"] == r["video_sha256"] == sha(GOOD) and r["copy_verified"] is True
    assert "不代表用户已观看" in r["statement"]
    h = r["hashes"]
    assert h["brief"] == sha(bp) and h["reviews"] == sha(rv) and h["report"] == sha(r["paths"]["report"]) and h["profile"]
    rc = json.load(open(r["receipt"], encoding="utf-8"))
    assert rc["delivered_sha256"] == sha(r["delivered"])
    led = [json.loads(x) for x in open(os.path.join(STATE, "deliveries.jsonl"), encoding="utf-8")]
    assert any(x["receipt"] == r["receipt"] for x in led)
    reg = json.load(open(os.path.join(STATE, "registry.json"), encoding="utf-8"))["tasks"]
    assert any(t["brief"] == os.path.abspath(bp) and t["delivery_root"] == os.path.realpath(root) for t in reg)
    passed(*reel("receipt", r["receipt"]))
    with open(r["delivered"], "ab") as fh:
        fh.write(b"tampered")
    blocked(*reel("receipt", r["receipt"]), "RECEIPT_INVALID")
    return "回执绑定成片/brief/profile/报告/审片记录 hash；复制后 hash 一致；只写「已复制」；交付后被改 → RECEIPT_INVALID"


@case("5 deliver 回执")
def approved_sample_promotes_to_final_same_file_only():
    """待审样片被用户认可后，同版本号升成片：只认同一个文件，成片条件一条不少。"""
    bp, root, pdir = project("p_promote")
    passed(*deliver(GOOD, bp, "v1", "--reviews", final_reviews(GOOD, "rv_pr0.json", listen={}), "--sample"))
    blocked(*deliver(GOOD, bp, "v1", "--reviews", final_reviews(GOOD, "rv_pr1.json", listen={})), "DELIVERY_NOT_FINAL")
    swapped = P("promote_swapped.mp4")
    ff("-i", GOOD, "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "copy", swapped)
    blocked(*deliver(swapped, bp, "v1", "--reviews", final_reviews(swapped, "rv_pr2.json")), "PROMOTE_MISMATCH")
    out, code = deliver(GOOD, bp, "v1", "--reviews", final_reviews(GOOD, "rv_pr3.json"))
    passed(out, code)
    r = out["result"]
    assert r["status"] == "final" and r["promoted_from"].endswith("测试片-v1.json") and r["receipt"].endswith("测试片-v1-final.json"), r
    assert sorted(os.listdir(root)) == ["测试片-v1-sample-pending-review.mp4", "测试片-v1.mp4"], os.listdir(root)
    blocked(*deliver(GOOD, bp, "v1", "--reviews", final_reviews(GOOD, "rv_pr4.json")), "VERSION_EXISTS")
    return "样片→成片：缺试听记录仍拒；换了文件 → PROMOTE_MISMATCH；同一文件+认可 → 成片回执带 promoted_from；不能再升第二次"


@case("4 brief 与试听前置")
def final_reject_overrides_earlier_accept():
    bp, root, pdir = project("p_rej")
    s_ = sha(GOOD)
    rej = {"video_sha256": s_, "check": "final_listen", "decision": "reject", "by": "user",
           "note": "复听后觉得配音太快", "quote": "这版不行"}
    rv = final_reviews(GOOD, "rv_rej.json", extra=[rej])
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert any("final_listen 判定不通过" in x for x in out["error"]["detail"]["final_blockers"]), out["error"]["detail"]
    passed(*deliver(GOOD, bp, "v1", "--reviews", rv, "--sample"))
    return "同一成片先 accept 后 reject → 拒绝成片（任何 accept 都不算），只能交待审样片"


@case("5 deliver 回执")
def receipt_binds_plan_and_profile_hashes():
    from reelcore import profiles
    pp, cut = render_plan(VTT_OK)
    bp, root, pdir = project("p_plan")
    rv = final_reviews(cut, "rv_plan.json")
    out, code = reel("deliver", cut, "--brief", bp, "--version", "v1", "--plan", pp, "--reviews", rv)
    passed(out, code)
    h = out["result"]["hashes"]
    assert h["plan"] == sha(pp), (h["plan"], sha(pp))
    assert h["profile"] == profiles.load("default")["_sha256"], h["profile"]
    return "带 --plan 交付：回执里 plan hash = plan.json 的 sha256，profile hash = 解析后 default profile 的 sha256"


@case("5 deliver 回执")
def stale_report_cannot_vouch_for_bad_cut():
    bp, root, pdir = project("p_stale")
    old = P("old_pass_report.json")
    passed(*reel("check", GOOD, *OK_TEXT, "--report", old))
    bad = make("bad_for_stale.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,1)'")
    rv = final_reviews(bad, "rv_stale.json")
    out, code = deliver(bad, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_BLOCKED")
    p = subprocess.run([sys.executable, REEL, "deliver", bad, "--brief", bp, "--version", "v1", "--report", old],
                       capture_output=True, text=True, env={**os.environ, "REEL_STATE_DIR": STATE})
    assert p.returncode == 2 and "unrecognized arguments: --report" in p.stderr, (p.returncode, p.stderr[-200:])
    blocked(*deliver(bad, bp, "v1", "--reviews", rv, "--sample"), "DELIVERY_BLOCKED")
    assert not receipts(pdir) and not os.listdir(root)
    return "deliver 每次自己重查，没有 --report 入口；坏片连待审样片也不能交"


@case("5 deliver 回执")
def cut_replaced_after_review_is_not_final():
    bp, root, pdir = project("p_swap")
    rv = final_reviews(GOOD, "rv_swap.json")
    swapped = P("swapped.mp4")
    ff("-i", GOOD, "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "copy", swapped)
    out, code = deliver(swapped, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert not receipts(pdir)
    return "审片记录绑定的是原片 hash，换成重编码版本 → 记录作废，拒绝成片"


@case("5 deliver 回执")
def forged_empty_review_is_rejected():
    bp, root, pdir = project("p_forge")
    s = sha(GOOD)
    rv = final_reviews(GOOD, "rv_forge.json", watch={"video_sha256": s, "check": "final_watch", "decision": "accept",
                                                       "by": "claude", "note": "", "observations": []})
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert any("不合格" in x for x in out["error"]["detail"]["final_blockers"]), out["error"]["detail"]
    q = needs_review_clip()
    rv2 = final_reviews(q, "rv_forge2.json", extra=[{"video_sha256": sha(q), "check": "loudness", "decision": "accept",
                                                     "by": "claude", "note": "ok"}])
    blocked(*deliver(q, bp, "v1", "--reviews", rv2), "DELIVERY_NOT_FINAL")
    assert not receipts(pdir)
    return "空的看片记录、只写「ok」不带时间码的 REVIEW 处理 → 都不算，拒绝成片"


@case("5 deliver 回执")
def copy_mismatch_leaves_nothing():
    import contextlib, io
    from reelcore import cli, deliver as D
    bp, root, pdir = project("p_copy")
    rv = final_reviews(GOOD, "rv_copy.json")
    real = D._copy

    def corrupt(src, dst):
        real(src, dst)
        with open(dst, "r+b") as fh:
            fh.seek(1000)
            fh.write(b"\x00\x01\x02")
    os.environ["REEL_STATE_DIR"] = STATE
    D._copy = corrupt
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            code = cli.main(["deliver", GOOD, "--brief", bp, "--version", "v1", "--reviews", rv, *OK_TEXT])
    finally:
        D._copy = real
    out = json.loads(buf.getvalue())
    blocked(out, code, "COPY_MISMATCH")
    assert not os.listdir(root) and not receipts(pdir), (os.listdir(root), receipts(pdir))
    return "复制时注入坏字节 → COPY_MISMATCH；交付目录没有残留（含临时文件），没有回执"


@case("5 deliver 回执")
def version_reuse_is_refused():
    bp, root, pdir = project("p_dup")
    rv = final_reviews(GOOD, "rv_dup.json")
    passed(*deliver(GOOD, bp, "v1", "--reviews", rv))
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    blocked(out, code, "VERSION_EXISTS")
    blocked(*deliver(GOOD, bp, "v1", "--reviews", rv, "--sample"), "VERSION_EXISTS")
    open(os.path.join(root, "测试片-v2.mp4"), "wb").write(b"someone else's file")
    blocked(*deliver(GOOD, bp, "v2", "--reviews", rv), "VERSION_EXISTS")
    assert open(os.path.join(root, "测试片-v2.mp4"), "rb").read() == b"someone else's file"
    blocked(*deliver(GOOD, bp, "final", "--reviews", rv), "ARG_INVALID")
    assert receipts(pdir) == ["测试片-v1.json"], receipts(pdir)
    return "同版本号再交（成片或样片）/ 交付目录里已有同名文件 → VERSION_EXISTS，不覆盖；版本号必须是 vN"


if __name__ == "__main__":
    log = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None
    lines = []
    for g, name, ok, msg in RESULTS:
        lines.append(f"{'✅' if ok else '❌'} [{g}] {name}" + (f" — {msg}" if ok and msg else ("" if ok else "\n   " + msg.replace("\n", "\n   "))))
    fails = [r for r in RESULTS if not r[2]]
    lines.append(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} 通过")
    print("\n".join(lines))
    if log:
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
