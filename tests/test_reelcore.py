#!/usr/bin/env python3
"""reelcore regression tests (no pytest needed): python3 tests/test_reelcore.py
Covers the class-A issues found in code review plus geometry / gates. Needs ffmpeg; does not connect to a remote host (gpu_gate uses fake data)."""
import json, math, os, re, shutil, subprocess, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reelcore import camera, plan as P, remote, render, subtitles, verify  # noqa: E402
from reelcore.errors import ReelError  # noqa: E402

TMP = tempfile.mkdtemp(prefix="reel_test_")
RESULTS = []
SKIPPED = []


class Skip(Exception):
    """Optional dependency missing: reported as SKIPPED, never as passed."""


def case(fn):
    try:
        fn(); RESULTS.append((fn.__name__, True, ""))
    except Skip as e:
        SKIPPED.append((fn.__name__, str(e)))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((fn.__name__, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
    return fn


def raises(code, f, *a, **k):
    try:
        f(*a, **k)
    except ReelError as e:
        assert e.code == code, f"expected {code}, got {e.code}: {e.message} {e.detail}"
        return e
    raise AssertionError(f"expected {code} to be raised, but nothing was raised")


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


# ---------- media ----------
IMG = os.path.join(TMP, "img.png"); VOICE = os.path.join(TMP, "voice.wav"); VTT = os.path.join(TMP, "v.vtt")
ff("-f", "lavfi", "-i", "testsrc2=s=1600x1000:d=1", "-frames:v", "1", IMG)
ff("-f", "lavfi", "-i", "sine=f=300:d=3.0", "-ar", "48000", VOICE)
open(VTT, "w").write("WEBVTT\n\n00:00.000 --> 00:01.500\n第一句话。\n\n00:01.500 --> 00:03.000\n2024\n")
META = {"width": 1600, "height": 1000}


def base_plan(**over):
    p = {"version": P.VERSION, "src_dir": TMP, "output": {"width": 540, "height": 960, "fps": 30},
         "duration_policy": "require_match",
         "shots": [{"id": "s1", "asset": "img.png", "duration": 1.5, "camera": []},
                   {"id": "s2", "asset": "img.png", "duration": 1.5, "camera": []}],
         "audio": {"voice": "voice.wav", "subtitles": "v.vtt"}, "look": {}}
    p.update(over)
    return p


# ---------- A: still is truly static / unknown camera move ----------
@case
def still_is_static():
    vp, cam = P.legacy_camera("still", 2.0)
    assert vp is None and cam == []
    c = camera.ShotCamera({"id": "s", "duration": 2, "camera": []}, META, 540, 960, 30)
    assert c.static and "perspective" not in c.filter(540, 960)[1]
    assert c.at(0) == c.at(59) == c.start


@case
def unknown_move_rejected():
    raises("PLAN_INVALID", P.legacy_camera, "tpyo", 2.0)


# ---------- A: single segment + transition / transition too long / the last shot cannot have a transition ----------
@case
def single_shot_with_xfade_has_no_transition():
    txt = os.path.join(TMP, "one.txt"); open(txt, "w").write("img.png|0|3|in\n")
    p = P.from_legacy(txt, TMP, "voice.wav", "v.vtt", 540, 960, 30, xfade=0.4)
    assert "transition_out" not in p["shots"][0]
    P.validate(p)


@case
def transition_rules():
    p = base_plan(); p["shots"][0]["transition_out"] = {"type": "fade", "duration": 1.0}
    e = raises("PLAN_INVALID", P.validate, p)
    assert any("adjacent shot" in x for x in e.detail["errors"])
    p = base_plan(); p["shots"][1]["transition_out"] = {"type": "fade", "duration": 0.2}
    e = raises("PLAN_INVALID", P.validate, p)
    assert any("last shot" in x for x in e.detail["errors"])


# ---------- A: a transition eats the end of the voice-over ----------
@case
def transition_duration_mismatch_detected_and_fitted():
    p = base_plan(); p["shots"][0]["transition_out"] = {"type": "fade", "duration": 0.5}
    e = raises("DURATION_MISMATCH", render.compile_plan, p)
    assert e.detail["transitions_s"] == 0.5
    p = base_plan(duration_policy="fit_last_shot"); p["shots"][0]["transition_out"] = {"type": "fade", "duration": 0.5}
    c = render.compile_plan(p)
    assert c["visual_f"] == c["voice_f"] == 90 and c["frames"][-1] == 60, (c["visual_f"], c["frames"])


# ---------- A: subtitle parsing ----------
@case
def subtitles_parse_short_timestamps_and_numeric_text():
    ev = subtitles.parse(VTT)
    assert ev == [(0.0, 1.5, "第一句话。"), (1.5, 3.0, "2024")], ev


@case
def subtitles_empty_is_error():
    f = os.path.join(TMP, "empty.vtt"); open(f, "w").write("WEBVTT\n\n")
    raises("SUBTITLES_EMPTY", subtitles.parse, f)


@case
def subtitle_chunks_keep_english_words_whole():
    for line in ("Did Claude Code just ask you to paste the page again?",
                 "While it works, it briefly takes over your mouse and keyboard, and shows every step on screen."):
        chunks = subtitles.chunk_text(line)
        assert " ".join(chunks).replace("  ", " ") == line, chunks
        words = set(line.replace(",", " ").replace(".", " ").replace("?", " ").split())
        for c in chunks:
            for w in c.replace(",", " ").replace(".", " ").replace("?", " ").split():
                assert w in words, (w, chunks)
            assert len(c) <= 46, chunks
    assert subtitles.chunk_text("想停？连按两次 Esc（退出键）") == ["想停？", "连按两次 Esc（退出键）"], subtitles.chunk_text("想停？连按两次 Esc（退出键）")


@case
def subtitle_time_carry():
    assert subtitles._fmt(1.9996, 1000) == (0, 0, 2, 0)
    assert subtitles._fmt(59.996, 100) == (0, 1, 0, 0)
    srt = subtitles.write_srt([(0.0, 1.9996, "测试")], os.path.join(TMP, "c.srt"))
    assert ",1000" not in open(srt).read()


# ---------- plan validation ----------
@case
def plan_validation_reports_all():
    p = base_plan(); p["shots"][0]["camera"] = [
        {"op": "zoom_to", "bbox": [0.5, 0.5, 0.4, 0.6], "t0": 0, "t1": 1},
        {"op": "move_to", "viewport": [0, 0, 1, 1], "t0": 0.5, "t1": 1.4}]
    p["shots"][0]["bogus"] = 1
    e = raises("PLAN_INVALID", P.validate, p)
    errs = " ".join(e.detail["errors"])
    assert "unknown field 'bogus'" in errs and "bbox is missing or invalid" in errs and "overlaps" in errs, errs


@case
def observation_mismatch_rejected():
    obs = [{"id": "obs-1", "asset": "img.png", "bbox": [0.1, 0.1, 0.2, 0.2]}]
    p = base_plan(); p["shots"][0]["camera"] = [{"op": "zoom_to", "observation_id": "obs-1",
                                                 "bbox": [0.6, 0.6, 0.8, 0.8], "t0": 0, "t1": 1}]
    e = raises("PLAN_INVALID", P.validate, p, obs)
    assert any("IoU" in x for x in e.detail["errors"])
    p["shots"][0]["camera"][0].pop("bbox")
    P.validate(p, obs)
    assert p["shots"][0]["camera"][0]["bbox"] == [0.1, 0.1, 0.2, 0.2]


# ---------- geometry ----------
@case
def zoom_target_contains_subject_with_aspect():
    b = (1200, 100, 1400, 300)
    vp = camera.zoom_target(b, 0.2, 5, 1600, 1000, 540 / 960)
    assert vp[0] <= b[0] - 40 + 1e-6 and vp[2] >= b[2] + 40 - 1e-6 and vp[1] <= b[1] - 40 + 1e-6
    assert abs((vp[2] - vp[0]) / (vp[3] - vp[1]) - 540 / 960) < 1e-9
    assert 0 <= vp[0] and vp[2] <= 1600 and 0 <= vp[1] and vp[3] <= 1000


@case
def max_zoom_respected():
    vp = camera.zoom_target((800, 500, 810, 510), 0.1, 2.0, 1600, 1000, 540 / 960)
    cover_w = 1000 * 540 / 960
    assert abs(cover_w / (vp[2] - vp[0]) - 2.0) < 1e-9


@case
def subject_too_large():
    raises("SUBJECT_TOO_LARGE", camera.zoom_target, (0, 0, 1600, 300), 0.1, 3, 1600, 1000, 540 / 960)
    # a portrait output asks for the whole landscape picture: it must not be cropped silently, it must raise an error
    shot = {"id": "s", "duration": 1, "camera": [{"op": "move_to", "viewport": [0, 0, 1, 1], "t0": 0, "t1": 1,
                                                  "easing": "linear"}]}
    raises("SUBJECT_TOO_LARGE", camera.ShotCamera, shot, META, 540, 960, 30)


def _eval_ffexpr(expr, on):
    env = {"on": on, "PI": math.pi, "cos": math.cos, "sin": math.sin, "pow": math.pow,
           "clip": lambda x, a, b: min(max(x, a), b), "lt": lambda a, b: 1.0 if a < b else 0.0,
           "iff": lambda c, a, b: a if c else b}
    return eval(re.sub(r"\bif\(", "iff(", expr), env)  # noqa: S307  the test evaluates expressions it generated itself


@case
def ffmpeg_expression_matches_python_trajectory():
    shot = {"id": "s", "duration": 3.0, "camera": [
        {"op": "zoom_to", "bbox": [0.7, 0.1, 0.8, 0.3], "t0": 0.5, "t1": 1.5, "easing": "ease_in_out",
         "padding": 0.2, "max_zoom": 3},
        {"op": "move_to", "viewport": [0.3, 0.0, 0.65, 1.0], "t0": 2.0, "t1": 3.0, "easing": "ease_out"}]}
    c = camera.ShotCamera(shot, META, 540, 960, 30)
    cx_e, cy_e, w_e = c._expr()
    for f in range(c.n):
        r = c.at(f)
        assert abs(_eval_ffexpr(cx_e, f + 1) - (r[0] + r[2]) / 2) < 1e-6, f
        assert abs(_eval_ffexpr(cy_e, f + 1) - (r[1] + r[3]) / 2) < 1e-6, f
        assert abs(_eval_ffexpr(w_e, f + 1) - (r[2] - r[0])) < 1e-6, f
    rx0, ry0, rx1, ry1 = c.region()
    for f in range(c.n):
        r = c.at(f)
        assert rx0 <= r[0] + 1e-6 and ry0 <= r[1] + 1e-6 and r[2] <= rx1 + 1e-6 and r[3] <= ry1 + 1e-6


@case
def track_expression_matches_python_and_follows():
    obs = [{"id": f"o{i}", "asset": "img.png", "t": 1.0 + i * 0.5,
            "bbox": [0.60 - i * 0.08, 0.40, 0.66 - i * 0.08, 0.48]} for i in range(4)]
    p = base_plan(); p["shots"][0].update(source_in=0, duration=3.0)
    p["shots"][0]["camera"] = [{"op": "zoom_to", "track": ["o0", "o1", "o2", "o3"], "t0": 0.2, "t1": 1.0,
                                "padding": 0.3, "max_zoom": 3}]
    P.validate(p, obs)
    c = camera.ShotCamera(p["shots"][0], META, 540, 960, 30)
    cx_e, cy_e, w_e = c._expr()
    for f in range(c.n):
        r = c.at(f)
        assert abs(_eval_ffexpr(cx_e, f + 1) - (r[0] + r[2]) / 2) < 1e-6, f
        assert abs(_eval_ffexpr(w_e, f + 1) - (r[2] - r[0])) < 1e-6, f
    # follow: at every keyframe the subject centre is inside the framing window and the window size is unchanged
    ws = set()
    for o in obs:
        f = round(o["t"] * 30)
        r = c.at(f)
        bx = (o["bbox"][0] + o["bbox"][2]) / 2 * 1600
        assert r[0] < bx < r[2], (f, r, bx)
        if f >= 30:
            ws.add(round(r[2] - r[0], 6))
    assert len(ws) == 1, ws
    p["shots"][0]["camera"][0]["track"] = ["o1", "o0"]
    e = raises("PLAN_INVALID", P.validate, p, obs)
    assert any("increasing" in x for x in e.detail["errors"])


@case
def mixed_cut_and_xfade_three_shots():
    """Codex A1: a hard cut followed by xfade used to fail outright because the timebases differed."""
    ff("-f", "lavfi", "-i", "sine=f=300:d=4.2", "-ar", "48000", os.path.join(TMP, "v42.wav"))
    p = base_plan(audio={"voice": "v42.wav"})
    p["shots"] = [{"id": "a", "asset": "img.png", "duration": 1.5, "camera": []},
                  {"id": "b", "asset": "img.png", "duration": 1.5, "camera": [],
                   "transition_out": {"type": "fade", "duration": 0.3}},
                  {"id": "c", "asset": "img.png", "duration": 1.5, "camera": []}]
    out = os.path.join(TMP, "mixed.mp4")
    r = render.render(p, out, "cpu", log=lambda *a: None)
    assert r["ok"], [c for c in r["checks"] if not c["ok"]]
    nf = next(c for c in r["checks"] if c["check"] == "frames")["actual"]
    assert abs(nf - 126) <= 1, nf


@case
def fit_shrink_that_breaks_camera_is_rejected():
    """Codex A3: after shortening the last shot, a plan the validator itself rejects must not be returned."""
    ff("-f", "lavfi", "-i", "sine=f=300:d=1.6", "-ar", "48000", os.path.join(TMP, "v16.wav"))
    p = base_plan(duration_policy="fit_last_shot", audio={"voice": "v16.wav"})
    p["shots"][0]["duration"] = p["shots"][1]["duration"] = 2.0
    p["shots"][0]["transition_out"] = {"type": "fade", "duration": 0.5}
    p["shots"][1]["camera"] = [{"op": "move_to", "viewport": [0.3, 0.0, 0.65, 1.0], "t0": 1.0, "t1": 2.0}]
    e = raises("DURATION_MISMATCH", render.compile_plan, p)
    assert e.detail["errors"], e.detail


@case
def geometry_never_trims_subject():
    """Codex A7: even 1 px too much must not be cropped silently."""
    raises("SUBJECT_TOO_LARGE", camera.fit_viewport, (0, 0, 1000, 1004), 1000, 1600, 1.0, "x")
    assert camera.fit_viewport((0, 0, 1000, 1000), 1000, 1600, 1.0, "x") == (0, 0, 1000, 1000)


@case
def subtitle_blocks_strict():
    """Codex A6: text beginning with NOTEBOOK/WEBVTT is kept; a bad block raises an error instead of being dropped silently."""
    f = os.path.join(TMP, "nb.vtt")
    open(f, "w").write("WEBVTT\n\n00:00.000 --> 00:01.000\nNOTEBOOK\n\nNOTE 注释\n\n00:01.000 --> 00:02.000\n"
                       "WEBVTT 开头\n")
    assert [e[2] for e in subtitles.parse(f)] == ["NOTEBOOK", "WEBVTT 开头"]
    open(f, "w").write("WEBVTT\n\n00:00.000 --> 00:01.000\n好\n\n0:0x --> 00:02\n坏\n")
    raises("SUBTITLES_INVALID", subtitles.parse, f)


@case
def codex_round3_regressions():
    # A-new-1: externally submitted track_boxes must be discarded and recomputed
    p = base_plan(); p["shots"][0]["camera"] = [{"op": "zoom_to", "bbox": [0.6, 0.2, 0.7, 0.4], "t0": 0, "t1": 1,
                                                 "track_boxes": [[0, [0.2, 0.2, 0.4, 0.4]], [1, [0.5, 0.5]]]}]
    P.validate(p)
    assert "track_boxes" not in p["shots"][0]["camera"][0]
    camera.ShotCamera(p["shots"][0], META, 540, 960, 30)
    # A-new-2: missing blank line / NOTE+tab / empty text with reversed time
    f = os.path.join(TMP, "r3.vtt")
    open(f, "w").write("1\n00:00.000 --> 00:01.000\nfirst\n2\n00:01.000 --> 00:02.000\nsecond\n")
    raises("SUBTITLES_INVALID", subtitles.parse, f)
    open(f, "w").write("WEBVTT\n\nNOTE\tcomment\n\n00:00.000 --> 00:01.000\nok\n")
    assert subtitles.parse(f) == [(0.0, 1.0, "ok")]
    open(f, "w").write("WEBVTT\n\n00:00.000 --> 00:01.000\nok\n\n00:03.000 --> 00:02.000\n\n")
    raises("SUBTITLES_INVALID", subtitles.parse, f)
    # B: an observation at t=0 must also be checked against the source range
    obs = [{"id": "o", "asset": "img.png", "t": 0.0, "bbox": [0.6, 0.2, 0.7, 0.4]}]
    p = base_plan(); p["shots"][0].update(source_in=10)
    p["shots"][0]["camera"] = [{"op": "zoom_to", "observation_id": "o", "t0": 0, "t1": 1}]
    e = raises("PLAN_INVALID", P.validate, p, obs)
    assert any("source range" in x for x in e.detail["errors"])
    # A-new-3: trailing whitespace in a path
    from reelcore.render import _fesc
    os.makedirs(os.path.join(TMP, "trail dir "), exist_ok=True)      # lut3d decides the type by extension, so the whitespace goes at the end of the directory name
    lut = os.path.join(TMP, "trail dir ", "a.cube")
    open(lut, "w").write("LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1)))
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x64:d=0.1", "-vf",
                        f"lut3d=file={_fesc(lut)},format=yuv420p", "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-200:]
    # whitespace at the end of the whole value (lut3d goes by extension, so test with a movie source that detects the format by content)
    tail = os.path.join(TMP, "tail.png ")
    shutil.copy(IMG, tail)
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"movie=filename={_fesc(tail)}", "-frames:v", "1",
                        "-f", "null", "-"], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-200:]


@case
def subtitles_after_end_fail_acceptance():
    out = os.path.join(TMP, "s3.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=3", "-f", "lavfi", "-i", "sine=d=3", "-pix_fmt", "yuv420p",
       "-shortest", out)
    ok = verify.check(out, 3.0, 30, 540, 960, subs_cues=[(0.5, 1.0, "a")])
    assert ok["ok"], ok
    bad = verify.check(out, 3.0, 30, 540, 960, subs_cues=[(0.5, 1.0, "a"), (3.5, 4.0, "b")])
    assert not bad["ok"] and any(c["check"] == "subtitles_in_range" and not c["ok"] for c in bad["checks"])
    long_ = verify.check(out, 3.0, 30, 540, 960, subs_cues=[(1.0, 100.0, "拖到片尾之后")])
    assert not long_["ok"]


@case
def lut_path_with_special_chars():
    """Codex A8: paths containing ' , [ ] ; : and spaces must also work."""
    d = os.path.join(TMP, "John's, [x]; y"); os.makedirs(d, exist_ok=True)
    lut = os.path.join(d, "a:b.cube")
    open(lut, "w").write("LUT_3D_SIZE 2\n" + "".join(f"{r} {g} {b}\n" for b in (0, 1) for g in (0, 1) for r in (0, 1)))
    p = base_plan(look={"lut": lut})
    r = render.render(p, os.path.join(TMP, "lut.mp4"), "cpu", log=lambda *a: None)
    assert r["ok"], [c for c in r["checks"] if not c["ok"]]


# ---------- web shot (no OCR, positions read from the DOM) ----------
@case
def web_find_matches_dom_exactly():
    import importlib.util
    if importlib.util.find_spec("playwright") is None:
        raise Skip("playwright not installed (./install.sh --web)")
    from reelcore import web
    html = os.path.join(TMP, "w.html")
    open(html, "w").write("<!doctype html><meta charset=utf-8><body style='font:18px sans-serif;margin:20px'>"
                          "<p>前面<span id=t>一段<b>跨越</b>元素<a href=#>并且会换行的目标文字，继续写长一点让它折到第二行去</a>"
                          "</span>后面</p></body>")
    pg = web.Page(html, 540, 960, 300)
    try:
        chars, text = pg.find("一段跨越元素并且会换行的目标文字，继续写长一点让它折到第二行去")
        ub = pg.page.evaluate("(()=>{const r=[...document.getElementById('t').getClientRects()];"
                              "return [Math.min(...r.map(x=>x.left)),Math.min(...r.map(x=>x.top)),"
                              "Math.max(...r.map(x=>x.right)),Math.max(...r.map(x=>x.bottom))]})()")
        assert max(abs(a - b) for a, b in zip(web.bbox_of(chars), ub)) < 0.5, (web.bbox_of(chars), ub)
        first, last = pg.page.evaluate("(()=>{const r=[...document.getElementById('t').getClientRects()];"
                                       "return [r[0].left, r[r.length-1].right]})()")
        assert abs(chars[0][0] - first) < 0.5 and abs(chars[-1][2] - last) < 0.5, (chars[0], chars[-1], first, last)
        assert len(web.lines_of(chars)) >= 2 and "，" in text
        raises("TEXT_NOT_FOUND", pg.find, "页面里没有这句")
    finally:
        pg.close()


@case
def web_highlight_progress_is_smooth():
    from reelcore import web
    page_text = "你好，世界。再见！"
    words = [("你", 0.0, 0.2), ("好", 0.2, 0.4), ("世", 0.4, 0.4), ("界", 0.4, 0.7), ("再", 1.2, 1.4), ("见", 1.4, 1.6)]
    times = web.spread_to_page(page_text, web.align(page_text, [(c, a, b) for c, a, b in words]))
    assert [t is None for t in times] == [False, False, True, False, False, True, False, False, True]
    prog = web.progress_curve(times, 60, 120, 0.0, lead=0.0)
    import numpy as np
    d = np.diff(prog)
    assert (d >= -1e-9).all() and abs(prog[-1] - len(page_text)) < 1e-6
    assert np.abs(np.diff(d)).max() < 0.12, np.abs(np.diff(d)).max()      # no sudden jump at punctuation
    assert abs(prog[18] - 2.0) < 0.25, prog[18]      # 0.3s: half of the second character is spoken and the comma is interpolated between the 2nd and 3rd characters, giving about 2.0, which keeps up with the speech
    # it still matches when whisper gets one character wrong
    t2 = web.align("关键在于平滑", web.char_timeline([(0, 1.2, "关键再于平滑")]))
    assert len(t2) == 6


@case
def web_follow_camera_locks_column_and_caps_speed():
    from reelcore import web
    col = (100, 0, 450, 5000)                                          # page width 600, the text column in the middle, framing width 378 leaves room
    boxes = [None] * 10 + [(100, 1500, 450, 1530)] * 50 + [(100, 3000, 450, 3030)] * 200
    rects = web.follow_camera(boxes, col, (0, 0, 600, 1067), 9 / 16, 300, 600, 5000, 60, max_speed=0.7)
    import numpy as np
    r = np.array(rects); cx = (r[:, 0] + r[:, 2]) / 2; cy = (r[:, 1] + r[:, 3]) / 2; h = r[:, 3] - r[:, 1]
    assert np.ptp(cx) < 1e-6 and abs(cx[0] - 275) < 1e-6               # locked horizontally on the centre of the text column
    assert (r[:, 0] <= 100 + 1e-6).all() and (r[:, 2] >= 450 - 1e-6).all()  # whole lines are not cut
    assert abs(cy[0] - 1515) < 1                                      # starts at the first line instead of swinging down from the page top
    assert (np.abs(np.diff(cy)) / h[1:] * 60).max() <= 0.7 * 1.05     # peak speed <= max_speed picture-heights per second


@case
def padding_shrinks_before_subject_too_large():
    """Real project 2026-09-30: the subject fits and only the padding pushes it out, so the padding should shrink automatically instead of raising an error."""
    b = (100, 100, 640, 400)                     # width 540; a portrait window 1000 high is at most 562.5 wide
    vp = camera.zoom_target(b, 0.3, 3, 1600, 1000, 540 / 960)
    assert vp[0] <= b[0] + 1e-6 and vp[2] >= b[2] - 1e-6 and vp[1] <= b[1] + 1e-6 and vp[3] >= b[3] - 1e-6
    raises("SUBJECT_TOO_LARGE", camera.zoom_target, (0, 0, 700, 300), 0.3, 3, 1600, 1000, 540 / 960)


@case
def one_frame_short_source_is_padded():
    """Real project 2026-09-30: need 7.817s vs source 7.8s, a difference of only 1 frame, still raised SOURCE_TOO_SHORT."""
    vid = os.path.join(TMP, "v3.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=3", "-pix_fmt", "yuv420p", vid)
    ff("-f", "lavfi", "-i", "sine=f=300:d=3.034", "-ar", "48000", os.path.join(TMP, "v3034.wav"))
    p = base_plan(duration_policy="require_match", audio={"voice": "v3034.wav"})
    p["shots"] = [{"id": "s1", "asset": "v3.mp4", "duration": 3.034, "camera": []}]   # 91 frames vs 90 frames in the media: 1 frame short
    c = render.compile_plan(json.loads(json.dumps(p)))
    assert c["frames"][0] == 91 and "s1" in c["pads"], (c["frames"], c["pads"])
    out = os.path.join(TMP, "pad.mp4")
    r = render.preview_shot(p, "s1", out)
    assert r["ok"] and next(x for x in r["checks"] if x["check"] == "frames")["actual"] == 91, r["checks"]
    ff("-f", "lavfi", "-i", "sine=f=300:d=3.1", "-ar", "48000", os.path.join(TMP, "v31.wav"))
    p = base_plan(duration_policy="require_match", audio={"voice": "v31.wav"})
    p["shots"] = [{"id": "s1", "asset": "v3.mp4", "duration": 3.1, "camera": []}]   # 3 frames over: should still raise an error
    raises("SOURCE_TOO_SHORT", render.preview_shot, p, "s1", out)


@case
def sheet_parses_trailing_comma():
    from reelcore import cli
    import subprocess as sp
    orig = sp.run
    class R:  # ffprobe prints "180," for some files
        stdout = "180,\n"; returncode = 0
    def fake(cmd, *a, **k):
        if cmd[0] == "ffprobe":
            return R()
        return orig(cmd, *a, **k)
    vid = os.path.join(TMP, "sh.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=6", "-pix_fmt", "yuv420p", vid)
    sp.run = fake
    try:
        r = cli._sheet(vid, os.path.join(TMP, "sh.png"))
    finally:
        sp.run = orig
    assert r["frames"][-1] == 179 and os.path.exists(os.path.join(TMP, "sh.png")), r


# ---------- remote GPU gate ----------
@case
def gpu_gate_80_percent():
    info = {"host": "x", "gpu": {"name": "GPU-32G", "used_mib": 20000, "total_mib": 32607, "util_pct": 2}}
    raises("NEED_USER_APPROVAL", remote.gpu_gate, info, 8000)
    assert remote.gpu_gate(info, 8000, allow_over=True)["need_mib"] == 8000
    assert remote.gpu_gate(info, 3000)["cap_mib"] == int(32607 * 0.8)
    info["gpu"]["util_pct"] = 90
    raises("GPU_BUSY", remote.gpu_gate, info, 100)


@case
def remote_host_only_from_environment():
    """The remote host only comes from REEL_REMOTE / REEL_REMOTES; when neither is set, probe does not run ssh and returns ok=False directly."""
    saved = {k: os.environ.pop(k, None) for k in ("REEL_REMOTE", "REEL_REMOTES")}
    try:
        assert remote.candidates() == []
        orig = remote.subprocess.run
        def no_ssh(*a, **k):
            raise AssertionError("probe must not run ssh when no host is configured")
        remote.subprocess.run = no_ssh
        try:
            info = remote.probe()
        finally:
            remote.subprocess.run = orig
        assert info["ok"] is False and info["configured"] is False and info["tried"] == [], info
        os.environ["REEL_REMOTE"] = "user@gpu-box"
        os.environ["REEL_REMOTES"] = "gpu-a, user@gpu-box ,,gpu-b"
        assert remote.candidates() == ["user@gpu-box", "gpu-a", "gpu-b"], remote.candidates()
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


# ---------- render + acceptance (including BGM + colour grade together) ----------
@case
def render_end_to_end_with_bgm_and_grade():
    p = base_plan(duration_policy="fit_last_shot",
                  audio={"voice": "voice.wav", "subtitles": "v.vtt", "bgm": "voice.wav", "bgm_gain": 0.1},
                  look={"grade": True})
    p["shots"][0]["camera"] = [{"op": "zoom_to", "bbox": [0.7, 0.1, 0.8, 0.4], "t0": 0, "t1": 1.5}]
    p["shots"][0]["transition_out"] = {"type": "fade", "duration": 0.3}
    out = os.path.join(TMP, "out.mp4")
    r = render.render(p, out, "cpu", log=lambda *a: None)
    assert r["ok"], json.dumps([c for c in r["checks"] if not c["ok"]], ensure_ascii=False)
    c = render.compile_plan(base_plan(look={"grade": True}))
    assert c["plan"]["look"]["grade"]


@case
def video_source_in_starts_camera_at_frame_zero():
    """Regression: with a video in-point plus a camera move, output frame 0 must be the source in-point frame and in the initial framing (full picture)."""
    vid = os.path.join(TMP, "count.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=8", "-pix_fmt", "yuv420p", vid)
    p = base_plan(duration_policy="fit_last_shot")
    p["shots"] = [{"id": "s1", "asset": "count.mp4", "source_in": 5.0, "duration": 3.0,
                   "camera": [{"op": "move_to", "viewport": [0.25, 0.25, 0.75, 0.75], "t0": 1.0, "t1": 2.9}]}]
    out = os.path.join(TMP, "seek.mp4")
    r = render.preview_shot(p, "s1", out)
    assert r["ok"], r
    ref = os.path.join(TMP, "ref.png"); got = os.path.join(TMP, "got.png")
    ff("-ss", "5.0", "-i", vid, "-frames:v", "1", ref)
    ff("-i", out, "-frames:v", "1", got)
    e = subprocess.run(["ffmpeg", "-hide_banner", "-i", ref, "-i", got, "-filter_complex", "[0:v][1:v]ssim", "-f",
                        "null", "-"], capture_output=True, text=True).stderr
    sim = float(re.search(r"All:([0-9.]+)", e).group(1))
    assert sim > 0.9, f"output first frame does not match source 5.0s (SSIM={sim:.3f}); camera move / in-point misaligned"


@case
def verify_fails_on_bad_output():
    bad = os.path.join(TMP, "bad.mp4"); open(bad, "wb").write(b"not a video")
    r = verify.check(bad, 3.0, 30, 540, 960)
    assert not r["ok"]
    short = os.path.join(TMP, "short.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=1", "-pix_fmt", "yuv420p", short)
    r = verify.check(short, 3.0, 30, 540, 960, expect_audio=False)
    assert not r["ok"] and any(c["check"] == "frames" and not c["ok"] for c in r["checks"])


if __name__ == "__main__":
    fails = [r for r in RESULTS if not r[1]]
    for name, ok, msg in RESULTS:
        print(("✅ " if ok else "❌ ") + name + ("" if ok else "\n   " + msg.replace("\n", "\n   ")))
    for name, why in SKIPPED:
        print(f"⏭️  {name} — SKIPPED: {why}")
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed" + (f", {len(SKIPPED)} skipped (optional dependency missing)" if SKIPPED else ""))
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
