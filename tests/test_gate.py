#!/usr/bin/env python3
"""Delivery-gate fault-injection matrix (no pytest needed): python3 tests/test_gate.py [--log FILE]

Every gate gets an injected fault to prove it blocks, plus a passing control run to prove it does not block wrongly. The CLI runs as a real subprocess (so PATH / a fake ffmpeg can be injected).
1 failure semantics  2 technical checks  3 text scan  4 brief / voice / audition prerequisites  5 deliver receipts
"""
import json, os, shutil, stat, subprocess, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
REEL = os.path.join(ROOT, "reel.py")
TMP = tempfile.mkdtemp(prefix="reel_gate_")
STATE = os.path.join(TMP, "state")
REAL_FFMPEG = shutil.which("ffmpeg")
RESULTS = []          # (group, name, ok, description)


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
        raise AssertionError(f"stdout is not JSON (exit {p.returncode}): {p.stdout[-300:]} {p.stderr[-500:]}")
    return out, p.returncode


def item(out, name):
    return next(x for x in out["result"]["items"] if x["check"] == name)


def blocked(out, code, expect_code=None):
    assert code != 0 and out["ok"] is False, f"should have been blocked: exit={code} ok={out.get('ok')}"
    if expect_code:
        assert out["error"]["code"] == expect_code, out["error"]
    return out


def passed(out, code):
    assert code == 0 and out["ok"] is True, f"should have passed: exit={code} {json.dumps(out.get('error'), ensure_ascii=False)[:600]}" \
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


# ---------- media ----------
VOL_DEFAULT, VOL_STRICT = 2.44, 1.94        # 440 Hz sine, about -14 / -16 LUFS (calibrated by measurement)


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


# ================= 1 failure semantics =================

@case("1 failure semantics")
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
    return (f"passing control default / vertical-strict ({lu} LUFS) exit 0; a passing default video is blocked under vertical-strict for loudness -14; "
            f"not_covered={out['result']['gate']['not_covered']}")


@case("1 failure semantics")
def fail_item_blocks():
    bad = make("black_open.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='lt(t,1)'")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code, "CHECK_FAILED")
    return f"first 1 s black -> {out['error']['detail']['blockers']}"


@case("1 failure semantics")
def missing_dependency_blocks():
    nobin = os.path.join(TMP, "empty-path")
    os.makedirs(nobin, exist_ok=True)
    out, code = reel("check", GOOD, *OK_TEXT, env={"PATH": nobin})
    blocked(out, code, "DEPENDENCY_MISSING")
    return f"no ffmpeg on PATH -> {out['error']['code']} {out['error']['detail']}"


SHIM = os.path.join(TMP, "shim")
os.makedirs(SHIM)
with open(os.path.join(SHIM, "ffmpeg"), "w") as fh:
    fh.write(f"""#!{sys.executable}
# fake ffmpeg: injects the fault named by REEL_FAULT and passes everything else to the real ffmpeg
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


@case("1 failure semantics")
def shim_passthrough_is_clean():
    out, code = reel("check", GOOD, *OK_TEXT, env=shim(""))
    passed(out, code)
    return "the fake ffmpeg passes as normal when nothing is injected (proving the blocks below come from the injection)"


@case("1 failure semantics")
def process_nonzero_blocks():
    out, code = reel("check", GOOD, *OK_TEXT, env=shim("exit:ebur128"))
    blocked(out, code, "CHECK_FAILED")
    it = item(out, "loudness")
    assert it["status"] == "not_checked" and it["error"]["code"] == "FFMPEG_FAILED", it
    return "the ebur128 process exits 1, so loudness is not_checked(FFMPEG_FAILED) and the gate blocks"


@case("1 failure semantics")
def unparsable_events_block():
    # 6 black frames at second 3: black_frames fails when untouched; when the event-line format is altered it must not degrade into "no black frames = pass"
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
    return "for a video with black frames / freezes whose event-line format is altered, OUTPUT_UNPARSABLE blocks and it is not treated as \"no events\""


@case("1 failure semantics")
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
    return "ebur128 without Summary, Summary lacking True peak, and freezedetect not reaching the end all block"


@case("1 failure semantics")
def required_check_missing_blocks():
    prof = write("prof_need_subs.json", {"extends": "default", "required": ["decode_clean", "subtitles_in_range"]})
    out, code = reel("check", GOOD, *OK_TEXT, "--profile", prof)
    blocked(out, code, "CHECK_FAILED")
    prof2 = write("prof_need_unknown.json", {"extends": "default", "required": ["decode_clean", "lip_sync"]})
    out2, code2 = reel("check", GOOD, *OK_TEXT, "--profile", prof2)
    blocked(out2, code2, "CHECK_FAILED")
    whys = [b["why"] for b in out2["result"]["gate"]["blockers"]]
    assert any("did not run" in w for w in whys), whys
    out3, code3 = reel("check", GOOD)
    blocked(out3, code3, "CHECK_FAILED")
    assert out3["result"]["gate"]["blockers"][0]["check"] == "text_scan"
    return "a required check being not_checked (subtitles without a plan) / having no result (lip_sync) / a text scan with no input all block"


def needs_review_clip():
    return make("quiet.mp4", vol=0.6)       # about -26 LUFS: only triggers loudness needs_review


def rec(video_sha, check, decision="accept", by="user", note="watched; overall a bit quiet but acceptable", **kw):
    r = {"video_sha256": video_sha, "check": check, "decision": decision, "by": by, "note": note,
         "observations": [{"t": [0, 8], "note": "quiet throughout, dialogue clear"}]}
    r.update(kw)
    return r


def sha(path):
    from reelcore import check
    return check.sha256(path)


@case("1 failure semantics")
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
    rej = write("rev_rej.json", {"records": [rec(sha(q), "loudness", decision="reject", note="too quiet, the voice-over is hard to hear")]})
    out, code = reel("check", q, *OK_TEXT, "--reviews", rej)
    blocked(out, code, "CHECK_FAILED")
    return "unresolved REVIEW / a record for another file / an empty record blocks; a failing verdict gives FAIL; a valid record passes"


# ================= 2 technical checks =================

@case("2 technical checks")
def single_black_first_frame_fails():
    bad = make("bf1.mp4", vf="drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='eq(n,0)'")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code)
    assert item(out, "first_frame")["status"] == "fail", item(out, "first_frame")
    return "only frame 0 black gives first_frame fail (blackdetect's minimum duration cannot catch a single frame, first_frame decodes frame 0 directly)"


@case("2 technical checks")
def low_motion_tolerates_only_isolated_jitter():
    from reelcore.check import _low_runs
    c = {"diff_max": 0.8, "spike_max": 0.9, "window_s": 4.0}
    base = [0.4] * 14 + [None] + [0.5] * 3 + [90.0]           # 4.25 s slow push; the 15th sample is jitter
    seq = lambda *spike: base[:14] + list(spike) + base[14 + len(spike):]
    assert len(_low_runs(seq(0.82), c, 4)) == 1, "an isolated jitter of 0.82 (measured on ffmpeg 6.1) must not break the slow-push run"
    assert not _low_runs(seq(0.95), c, 4), "a sample above spike_max must break the run"
    assert not _low_runs(seq(0.82, 0.82), c, 4), "two consecutive over-threshold samples must break the run"
    assert not _low_runs(seq(0.82), {**c, "spike_max": 0.8}, 4), "with spike_max=diff_max it falls back to the original rule"
    return "an isolated 0.82 jitter does not break a 4.25 s slow-push run; 0.95 or two consecutive 0.82 break it; spike_max=diff_max falls back to the original rule"


@case("2 technical checks")
def head_silence_fails():
    bad = make("head4.mp4", af="volume=enable='lt(t,4)':volume=0")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code)
    it = item(out, "head_silence")
    assert it["status"] == "fail" and it["measured"]["head_silence_s"] >= 3.9, it
    return f"first 4 s silent gives head_silence fail（{it['measured']}）"


@case("2 technical checks")
def tail_silence_fails_by_profile():
    bad = make("tail3.mp4", af="volume=enable='gte(t,5)':volume=0")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code, "CHECK_FAILED")
    assert item(out, "tail_silence")["status"] == "fail"
    rb = make("tail3_strict.mp4", vol=VOL_STRICT, af="volume=enable='gte(t,5)':volume=0")
    out2, code2 = reel("check", rb, *OK_TEXT, "--profile", "vertical-strict")
    blocked(out2, code2)
    assert item(out2, "tail_silence")["status"] == "needs_review", item(out2, "tail_silence")
    return "last 3 s silent gives default fail / vertical-strict needs_review"


@case("2 technical checks")
def long_freeze_fails():
    bad = make("freeze6.mp4", dur=10, vf="loop=loop=179:size=1:start=30")
    out, code = reel("check", bad, *OK_TEXT)
    blocked(out, code, "CHECK_FAILED")
    it = item(out, "long_freeze")
    assert it["status"] == "fail", it
    return f"6 s freeze gives long_freeze fail {it['evidence']}"


@case("2 technical checks")
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
    return "mpeg4 video / 44.1k sample rate / 720x1280 under vertical-strict gives format fail"


@case("2 technical checks")
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
    return (f"corrupted bytes in the middle -> {out['error']['code']} decode_clean={item(out, 'decode_clean')['status']}；"
            f"truncated by half -> {out2['error']['code']}")


@case("2 technical checks")
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
    return "silent-version profile: no audio track / all silent passes; the same video under default blocks; a silent-profile video that has sound blocks"


@case("2 technical checks")
def registered_exception_passes():
    bad = make("freeze6b.mp4", dur=10, vf="loop=loop=179:size=1:start=30")
    prof = write("prof_allow.json", {"extends": "default", "allow": [
        {"check": c, "t": [0.9, 7.1], "reason": "the opening freeze is a designed title hold"} for c in ("long_freeze", "freeze", "low_motion")]})
    out, code = reel("check", bad, *OK_TEXT, "--profile", prof)
    passed(out, code)
    assert item(out, "long_freeze")["excused"]["from"] == "fail"
    short = write("prof_allow_short.json", {"extends": "default", "allow": [
        {"check": c, "t": [0.9, 4.0], "reason": "only the first half was registered"} for c in ("long_freeze", "freeze", "low_motion")]})
    out2, code2 = reel("check", bad, *OK_TEXT, "--profile", short)
    blocked(out2, code2)
    noreason = write("prof_allow_noreason.json", {"extends": "default", "allow": [
        {"check": "long_freeze", "t": [0, 10], "reason": ""}]})
    out3, code3 = reel("check", bad, *OK_TEXT, "--profile", noreason)
    blocked(out3, code3, "PROFILE_INVALID")
    return "an exception covering the whole range passes; covering only half blocks; an exception without a reason gives PROFILE_INVALID"


# ================= 3 text scan =================
PROJ = os.path.join(TMP, "proj")
os.makedirs(PROJ)


def render_plan(subs_text):
    """Really render a small plan (two moving-video shots + sine voice-over + subtitles) and return (plan path, final video)."""
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


@case("3 text scan")
def control_plan_with_clean_subtitles_passes():
    pp, cut = render_plan(VTT_OK)
    out, code = reel("check", cut, "--plan", pp)
    passed(out, code)
    ts = item(out, "text_scan")
    assert "subtitles" in ts["measured"]["kinds"] and "plan" in ts["measured"]["kinds"], ts
    return f"a really rendered plan + clean subtitles passes; scanned {ts['measured']['kinds']}"


@case("3 text scan")
def banned_in_plan_subtitles_blocks():
    pp, cut = render_plan(VTT_BAD)
    out, code = reel("check", cut, "--plan", pp)
    blocked(out, code, "CHECK_FAILED")
    assert ("subtitles", "待定", False) in scan_hits(out), scan_hits(out)
    render_plan(VTT_OK)
    return "the word 待定 (TBD) in the plan subtitles blocks"


@case("3 text scan")
def banned_in_extra_subs_text_and_brief_block():
    srt = write("extra.srt", "1\n00:00:00,000 --> 00:00:02,000\nTODO replace with final copy\n")
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
    return "TODO in --subs / lorem ipsum in --text / the word 待确认 (to be confirmed) in the brief each block"


@case("3 text scan")
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
    return "TBD in the highlight text of a plan web shot fails; fixing it passes (module level; the web shot is not really rendered here)"


@case("3 text scan")
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
    return (f"a specified text / subtitle / brief file is missing, the profile requires subtitles but there are none, or the plan's subtitles were removed ({out['error']['code']}) all block")


@case("3 text scan")
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
    return "the old name Acme Edu is banned and Acme Education passes; both on the same line still blocks"


@case("3 text scan")
def ocr_is_reported_not_covered():
    out, code = reel("check", GOOD, *OK_TEXT)
    passed(out, code)
    o = item(out, "ocr_visible_text")
    assert o["status"] == "not_checked" and "OCR" in o["rule"], o
    assert "ocr_visible_text" in out["result"]["gate"]["not_covered"]
    assert "only input text is covered" in item(out, "text_scan")["rule"]
    return "text in the picture is not OCR-ed: ocr_visible_text is always not_checked and listed in gate.not_covered"


# ================= 4 brief / voice audition / audition prerequisites; 5 deliver receipts =================

def card_push_clip():
    """A 4.5 s text card pushed slowly 1.00 to 1.06 + 3 s of live footage: freeze detection misses it and only low_motion reports it."""
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
    """A registered task: project directory + delivery directory + an approved TTS voice + brief."""
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
          "note": "watched in full, checked section by section against the storyboard",
          "observations": [{"t": 0, "note": "the first frame has a picture"}, {"t": [3, 5], "note": "the middle is normal"}]}
    dl = {"video_sha256": s, "check": "final_listen", "decision": "accept", "by": "user",
          "note": "listened from the start", "quote": "声音没问题，可以发"}
    recs = [dw if watch is None else watch, dl if listen is None else listen] + list(extra)   # pass {} = leave this record out
    return write(name, {"version": "reel.reviews.v1", "records": [r for r in recs if r]})


def receipts(pdir):
    d = os.path.join(pdir, ".reel", "receipts")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def deliver(video, bp, version, *extra):
    return reel("deliver", video, "--brief", bp, "--version", version, *OK_TEXT, *extra)


@case("4 brief and audition prerequisites")
def control_full_final_delivery():
    bp, root, pdir = project("p_ok")
    rv = final_reviews(GOOD, "rv_ok.json")
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    passed(out, code)
    r = out["result"]
    assert r["status"] == "final" and os.path.isfile(os.path.join(root, "测试片-v1.mp4")), r
    return f"complete brief + approved voice + watch / listen records give a final {os.path.basename(r['delivered'])}"


@case("5 deliver receipts")
def delivered_file_mode_follows_umask():
    """A delivered file gets the normal mode (0666 minus umask), not the owner-only mode of mkstemp."""
    modes = {}
    old = os.umask(0o022)
    try:
        for um, ver in ((0o022, "v1"), (0o077, "v2")):
            os.umask(um)
            bp, root, pdir = project("p_mode_" + ver)
            rv = final_reviews(GOOD, "rv_mode_" + ver + ".json")
            out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
            passed(out, code)
            modes[um] = stat.S_IMODE(os.stat(out["result"]["delivered"]).st_mode)
    finally:
        os.umask(old)
    assert modes[0o022] == 0o644, oct(modes[0o022])
    assert modes[0o077] == 0o600, oct(modes[0o077])
    return f"delivered file mode is 0644 under umask 022 and 0600 under umask 077 (it follows the umask; it is no longer always 0600)"


@case("4 brief and audition prerequisites")
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
    return f"lacking {notes} refuses a final while --sample can deliver a sample pending review; a missing delivery directory or one that is too broad (~) refuses even a sample"


@case("4 brief and audition prerequisites")
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
    return f"not approved / a different voice approved / agent-approved without STT evidence / audition sample replaced all refuse a final ({whys}）"


SCRIPT = "今天我们来看一看这座城市的早晨，街口的早餐铺已经开门，豆浆刚刚煮好。"
try:                       # Chinese is compared by pinyin and needs pypinyin (./install.sh --zh-voice); without it the whole group uses English samples, with the same gate logic
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


@case("4 brief and audition prerequisites")
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
    return (f"[{STT_LANG}] agent with STT evidence (the transcript differs by one near-homophone error) gives a final; a transcript that does not match / a self-reported 0 that fails recomputation / a transcript of another sample / "
            f"text too short / no engine named is refused ({whys}）")


@case("4 brief and audition prerequisites")
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
        return "SKIPPED real transcription (one of say / whisper-cli / model / pypinyin is missing): only verified that a missing model gives BACKEND_UNAVAILABLE"
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
    return f"real transcription (say speaks, whisper transcribes): pinyin error rate of the correct text {err:.0%} is written to the brief and a final can be delivered; different text gives VOICE_CHECK_FAILED and the brief is not written; a missing model gives BACKEND_UNAVAILABLE"


@case("4 brief and audition prerequisites")
def final_listen_agent_needs_observations():
    bp, root, pdir = project("p_listen")
    s = sha(GOOD)
    no_listen = final_reviews(GOOD, "rv_nolisten.json", listen={})
    blocked(*deliver(GOOD, bp, "v1", "--reviews", no_listen), "DELIVERY_NOT_FINAL")
    agent_listen_bare = final_reviews(GOOD, "rv_agentlisten.json", listen={
        "video_sha256": s, "check": "final_listen", "decision": "accept", "by": "claude-agent",
        "note": "loudness is within target, should be fine", "quote": "ok"})
    out, code = deliver(GOOD, bp, "v1", "--reviews", agent_listen_bare)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    no_watch = final_reviews(GOOD, "rv_nowatch.json", watch={})
    blocked(*deliver(GOOD, bp, "v1", "--reviews", no_watch), "DELIVERY_NOT_FINAL")
    assert not receipts(pdir)
    agent_listen = final_reviews(GOOD, "rv_agentlisten2.json", listen={
        "video_sha256": s, "check": "final_listen", "decision": "accept", "by": "claude",
        "note": "listened from the start, checked levels and seams section by section",
        "observations": [{"t": [0, 2], "note": "no pop at the start"}, {"t": [4, 6], "note": "the ending closes naturally"}]})
    out, code = deliver(GOOD, bp, "v1", "--reviews", agent_listen)
    passed(out, code)
    assert out["result"]["status"] == "final" and out["result"]["approved_by"] == "agent", out["result"]
    assert "the user has not seen it yet" in out["result"]["statement"], out["result"]["statement"]
    bp2, _, pdir2 = project("p_listen_human")
    out, code = deliver(GOOD, bp2, "v1", "--reviews", final_reviews(GOOD, "rv_humanlisten.json"))
    passed(out, code)
    assert out["result"]["approved_by"] == "human", out["result"]
    return "no listening record / an agent with one line and no timecoded observation / no full-watch record is refused a final; an agent with timecoded listening gives a final with approved_by=agent; a person's quoted words give approved_by=human"


@case("4 brief and audition prerequisites")
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
        "note": "the title hold at the start is designed", "observations": [{"t": [0, 4.25], "note": "slow push on the title card, the user confirmed keeping it"}]}])
    out, code = deliver(cp, bp, "v2", "--reviews", rv2)
    passed(out, code)
    return "a 4.5 s slow-pushed card can only be delivered as a v1 sample pending review; after a per-item review record handles low_motion, v2 is a final"


@case("4 brief and audition prerequisites")
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
    return "brief.profile (a project profile relative to the brief directory, extending vertical-strict) applies to both check and deliver; --profile can override it"


@case("5 deliver receipts")
def receipt_binds_hashes_and_makes_no_viewing_claim():
    bp, root, pdir = project("p_rcpt")
    rv = final_reviews(GOOD, "rv_rcpt.json")
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    passed(out, code)
    r = out["result"]
    assert r["delivered_sha256"] == r["video_sha256"] == sha(GOOD) and r["copy_verified"] is True
    assert "does not mean the user has watched" in r["statement"]
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
    return "the receipt binds the hashes of the video / brief / profile / report / review records; the hash matches after copying; it only says copied; a change after delivery gives RECEIPT_INVALID"


@case("5 deliver receipts")
def approved_sample_promotes_to_final_same_file_only():
    """A sample pending review that the user has approved is promoted to final under the same version: only the same file counts and every final condition still applies."""
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
    return "sample to final: still refused without a listening record; a changed file gives PROMOTE_MISMATCH; the same file plus approval gives a final receipt with promoted_from; it cannot be promoted a second time"


@case("4 brief and audition prerequisites")
def final_reject_overrides_earlier_accept():
    bp, root, pdir = project("p_rej")
    s_ = sha(GOOD)
    rej = {"video_sha256": s_, "check": "final_listen", "decision": "reject", "by": "user",
           "note": "on listening again the voice-over felt too fast", "quote": "this version is not good"}
    rv = final_reviews(GOOD, "rv_rej.json", extra=[rej])
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert any("final_listen did not pass" in x for x in out["error"]["detail"]["final_blockers"]), out["error"]["detail"]
    passed(*deliver(GOOD, bp, "v1", "--reviews", rv, "--sample"))
    return "accept then reject on the same video refuses a final (no accept counts) and only a sample pending review can be delivered"


@case("5 deliver receipts")
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
    return "delivery with --plan: the receipt's plan hash = the sha256 of plan.json, and the profile hash = the sha256 of the resolved default profile"


@case("5 deliver receipts")
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
    return "deliver re-checks by itself every time and has no --report entry; a bad video cannot be delivered even as a sample pending review"


@case("5 deliver receipts")
def cut_replaced_after_review_is_not_final():
    bp, root, pdir = project("p_swap")
    rv = final_reviews(GOOD, "rv_swap.json")
    swapped = P("swapped.mp4")
    ff("-i", GOOD, "-c:v", "libx264", "-crf", "30", "-pix_fmt", "yuv420p", "-c:a", "copy", swapped)
    out, code = deliver(swapped, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert not receipts(pdir)
    return "review records are bound to the original video's hash; a re-encoded version voids them and a final is refused"


@case("5 deliver receipts")
def forged_empty_review_is_rejected():
    bp, root, pdir = project("p_forge")
    s = sha(GOOD)
    rv = final_reviews(GOOD, "rv_forge.json", watch={"video_sha256": s, "check": "final_watch", "decision": "accept",
                                                       "by": "claude", "note": "", "observations": []})
    out, code = deliver(GOOD, bp, "v1", "--reviews", rv)
    blocked(out, code, "DELIVERY_NOT_FINAL")
    assert any("invalid" in x for x in out["error"]["detail"]["final_blockers"]), out["error"]["detail"]
    q = needs_review_clip()
    rv2 = final_reviews(q, "rv_forge2.json", extra=[{"video_sha256": sha(q), "check": "loudness", "decision": "accept",
                                                     "by": "claude", "note": "ok"}])
    blocked(*deliver(q, bp, "v1", "--reviews", rv2), "DELIVERY_NOT_FINAL")
    assert not receipts(pdir)
    return "an empty watch record and a REVIEW handled with only ok and no timecode do not count; a final is refused"


@case("5 deliver receipts")
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
    return "injecting bad bytes while copying gives COPY_MISMATCH; nothing is left in the delivery directory (temp files included) and there is no receipt"


@case("5 deliver receipts")
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
    return "delivering the same version again (final or sample) / a same-name file already in the delivery directory gives VERSION_EXISTS without overwriting; the version must be vN"


if __name__ == "__main__":
    log = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None
    lines = []
    for g, name, ok, msg in RESULTS:
        lines.append(f"{'✅' if ok else '❌'} [{g}] {name}" + (f" — {msg}" if ok and msg else ("" if ok else "\n   " + msg.replace("\n", "\n   "))))
    fails = [r for r in RESULTS if not r[2]]
    lines.append(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
    print("\n".join(lines))
    if log:
        with open(log, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
