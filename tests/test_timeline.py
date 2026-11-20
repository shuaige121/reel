#!/usr/bin/env python3
"""Tests for timeline / scrub / edit / preview / check (no pytest needed): python3 tests/test_timeline.py
All media is synthesised with ffmpeg lavfi / PIL; no external files, no network, no remote host."""
import contextlib, io, json, math, os, re, shutil, subprocess, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from reelcore import cli, edit, render, timeline, visual  # noqa: E402
from reelcore.errors import ReelError  # noqa: E402

TMP = tempfile.mkdtemp(prefix="reel_tl_test_")
RESULTS = []
ONLY = set(sys.argv[1:])


def case(fn):
    if ONLY and fn.__name__ not in ONLY:
        return fn
    try:
        fn(); RESULTS.append((fn.__name__, True, ""))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((fn.__name__, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"))
    return fn


def ff(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def reel(*argv):
    """Run the CLI (in-process) and return the parsed JSON and the exit code."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main([str(x) for x in argv])
    return json.loads(buf.getvalue()), code


# ---------- media: solid-colour images (so a frame's shot can be told by colour) + video + voice / subtitles / per-word / BGM ----------
FPS = 30
SRC = os.path.join(TMP, "src")
os.makedirs(SRC)
ff("-f", "lavfi", "-i", "color=c=red:s=1600x1000", "-frames:v", "1", os.path.join(SRC, "red.png"))
ff("-f", "lavfi", "-i", "color=c=0x00ff00:s=1600x1000", "-frames:v", "1", os.path.join(SRC, "green.png"))
ff("-f", "lavfi", "-i", "testsrc2=s=1600x1000", "-frames:v", "1", os.path.join(SRC, "pattern.png"))
ff("-f", "lavfi", "-i", "color=c=blue:s=540x960:r=30:d=4", "-pix_fmt", "yuv420p", os.path.join(SRC, "blue.mp4"))
ff("-f", "lavfi", "-i", "sine=f=440:d=4.0", "-ar", "48000", "-af", "volume=0.3", os.path.join(SRC, "voice.wav"))
ff("-f", "lavfi", "-i", "sine=f=220:d=1.7", "-ar", "48000", "-af", "volume=0.3", os.path.join(SRC, "bgm.wav"))
open(os.path.join(SRC, "v.vtt"), "w", encoding="utf-8").write(
    "WEBVTT\n\n00:00.000 --> 00:01.200\n第一句去伏见稻荷。\n\n00:01.300 --> 00:02.600\n第二句看小鹿，\n\n"
    "00:02.700 --> 00:04.000\n第三句吃串炸\n")
json.dump([{"t0": 0.0, "t1": 0.4, "text": "第一"}, {"t0": 0.4, "t1": 1.2, "text": "句"},
           {"t0": 1.3, "t1": 2.6, "text": "第二句"}, {"t0": 2.7, "t1": 4.0, "text": "第三句"}],
          open(os.path.join(SRC, "words.json"), "w", encoding="utf-8"), ensure_ascii=False)


def make_plan(path=None, **over):
    """s1 red image pushed in for 1.5s (fade 0.5 into s2) / s2 blue video 1.5s / s3 green image static 1.5s: picture 4.5-0.5=4.0s = the voice-over."""
    p = {"version": "reel.plan.v1", "src_dir": "src", "output": {"width": 270, "height": 480, "fps": FPS},
         "duration_policy": "require_match",
         "shots": [{"id": "s1", "asset": "red.png", "duration": 1.5,
                    "camera": [{"op": "zoom_to", "bbox": [0.4, 0.3, 0.6, 0.7], "t0": 0.1, "t1": 1.4}],
                    "transition_out": {"type": "fade", "duration": 0.5}},
                   {"id": "s2", "asset": "blue.mp4", "source_in": 0.5, "duration": 1.5, "camera": []},
                   {"id": "s3", "asset": "green.png", "duration": 1.5, "camera": []}],
         "audio": {"voice": "voice.wav", "subtitles": "v.vtt", "words": "words.json"}, "look": {}}
    p.update(over)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False, indent=1)
    return p


PLAN = os.path.join(TMP, "plan.json")
make_plan(PLAN)


def resolved(raw, path):
    p = json.loads(json.dumps(raw))
    p["src_dir"] = os.path.join(os.path.dirname(path), p["src_dir"])
    return p


def compiled(path=PLAN):
    raw = json.load(open(path, encoding="utf-8"))
    c = render.compile_plan(resolved(raw, path))
    return c, timeline.build(c, detail="words")


def color_of(img):
    r, g, b = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
    return max((("red", r), ("green", g), ("blue", b)), key=lambda x: x[1])[0]


# ================= 1. timeline =================

@case
def timeline_frames_match_compile():
    c, tl = compiled()
    v1 = tl["tracks"]["V1"]
    assert [e["id"] for e in v1] == ["s1", "s2", "s3"]
    for i, e in enumerate(v1):
        assert e["start_f"] == c["starts"][i], (e["id"], e["start_f"], c["starts"][i])
        assert e["end_f"] == c["starts"][i] + c["frames"][i], (e["id"], e["end_f"])
        assert e["frames"] == c["frames"][i]
        if c["trans"][i]:
            assert e["transition_out"]["overlap_f"] == [c["starts"][i + 1], e["end_f"]], e["transition_out"]
            assert v1[i + 1]["transition_in"]["overlap_f"] == e["transition_out"]["overlap_f"]
    # concrete numbers: 45-frame shots and a 15-frame fade, so s2 starts at 30
    assert [(e["start_f"], e["end_f"]) for e in v1] == [(0, 45), (30, 75), (75, 120)], [(e["start_f"], e["end_f"]) for e in v1]
    assert v1[-1]["end_f"] == c["visual_f"] == c["voice_f"] == 120
    assert v1[1]["source_in"] == 0.5 and v1[1]["source_out"] == 2.0 and v1[0]["camera"] == "zoom_to"


@case
def timeline_fit_last_shot_adjustment_visible():
    p = os.path.join(TMP, "fit.json")
    make_plan(p, duration_policy="fit_last_shot")
    raw = json.load(open(p)); raw["shots"][2]["duration"] = 2.0; json.dump(raw, open(p, "w"))
    c, tl = compiled(p)
    assert tl["adjustments"] and tl["adjustments"][0]["frames_to"] == 45
    assert tl["tracks"]["V1"][2]["end_f"] == c["visual_f"] == 120
    assert "adjust: s3 frames 60→45" in timeline.text(tl)


@case
def timeline_voice_overlap_and_tracks():
    c, tl = compiled()
    v1 = {e["id"]: e for e in tl["tracks"]["V1"]}
    assert [v["id"] for v in v1["s1"]["voice"]] == ["sent-01", "sent-02"]      # [0,45) intersects sent-02 [39,78)
    assert [v["id"] for v in v1["s3"]["voice"]] == ["sent-02", "sent-03"]    # [75,120) intersects sent-02 [39,78)
    cues = tl["tracks"]["C1"]
    assert [x["sentence"] for x in cues] == ["sent-01", "sent-02", "sent-03"] and cues[0]["text"] == "第一句去伏见稻荷"
    a1 = tl["tracks"]["A1"][0]
    assert a1["end_f"] == 120 and len(a1["words"]) == 4 and a1["words"][2]["start_f"] == 39
    assert tl["tracks"]["BGM"] == []


@case
def timeline_cli_text_range_and_json():
    r, code = reel("timeline", PLAN, "--format", "text")
    assert code == 0, r
    t = r["result"]["text"]
    assert re.match(r"revision=\S+  fps=30  frames=120 \(4\.000s\)", t), t[:80]
    assert "V1  s2" in t and "f[30,75)" in t
    assert "tr_out=fade 15f f[30,45)" in t and "BGM none" in t
    r, _ = reel("timeline", PLAN, "--range", "2.6:4")
    ids = [e["id"] for e in r["result"]["tracks"]["V1"]]
    assert ids == ["s3"] or ids == ["s2", "s3"], ids
    assert r["result"]["range_f"] == [78, 120]
    assert [x["id"] for x in r["result"]["tracks"]["A1"][0]["sentences"]] == ["sent-03"]
    r, _ = reel("timeline", PLAN, "--format", "text", "--detail", "words")
    assert "w-0003" in r["result"]["text"] and "[asr_words]" in r["result"]["text"]


# ================= 2. PNG + scrub =================

@case
def timeline_png_and_sidecar():
    out = os.path.join(TMP, "tl.png")
    r, code = reel("timeline", PLAN, "--png", out)
    assert code == 0, r
    side = json.load(open(os.path.splitext(out)[0] + ".json"))
    from PIL import Image
    im = Image.open(out)
    assert im.size == (side["width"], side["height"]) and im.size[0] == 1600
    v1 = next(t for t in side["tracks"] if t["track"] == "V1")["events"]
    # pixel range matches the frame range: x = x0 + (f - fa) * ppf
    xm = side["x_map"]
    for e in v1:
        assert abs(e["x"][0] - (xm["x0"] + e["start_f"] * xm["px_per_frame"])) < 0.2, e
    assert v1[0]["thumb_source"] == "asset_first_frame(plan_crop)"
    assert side["font"] and not side["font_fallback"], side["font"]
    tr = next(t for t in side["tracks"] if t["track"] == "BGM/TR")["events"]
    assert tr and tr[0]["start_f"] == 30 and tr[0]["end_f"] == 45
    cam = next(t for t in side["tracks"] if t["track"] == "CAM")["events"]
    assert cam[0]["id"] == "s1" and cam[0]["max_zoom_rate"] > 0 and cam[1]["max_pan"] == 0
    # the thumbnail was really drawn: there is red inside the s1 box
    x = int((v1[0]["x"][0] + v1[0]["x"][1]) / 2)
    y0, y1 = next(t for t in side["tracks"] if t["track"] == "V1")["y"]
    px = [im.getpixel((x, y)) for y in range(y0 + 30, y1 - 30)]
    assert any(p[0] > 200 and p[1] < 60 for p in px), "the s1 thumbnail has no red"


@case
def font_fallback_is_recorded():
    f = visual.Fonts(["/nonexistent/font.ttc"])
    assert f.fallback and f.info()["font"] is None and f(12) is not None


@case
def scrub_cuts_plan_preview():
    out = os.path.join(TMP, "scrub_plan")
    r, code = reel("scrub", PLAN, "--cuts", "--overlays", "safe,caption,bbox", "--out", out)
    assert code == 0, r
    idx = json.load(open(os.path.join(out, "index.json")))
    frames = [x["frame"] for x in idx["frames"]]
    # s1 to s2 fade (cut 30: 29/30/31 + midpoint 37 + end 45), s2 to s3 hard cut (74/75/76)
    assert frames == [29, 30, 31, 37, 45, 74, 75, 76], frames
    from PIL import Image
    col = [color_of(Image.open(x["clean"])) for x in idx["frames"]]
    assert col[5] == "blue" and col[6] == "green" and col[7] == "green", col
    assert idx["frames"][6]["active"]["V1"] == ["s3"] and idx["frames"][3]["active"]["TR"] == ["s1>fade"]
    assert all(os.path.isfile(x["annotated"]) for x in idx["frames"])


# ================= render once; scrub/preview/check below share it =================
FULL = os.path.join(TMP, "full.mp4")


def full_render():
    if not os.path.isfile(FULL):
        raw = json.load(open(PLAN))
        raw["audio"]["bgm"] = "bgm.wav"; raw["audio"]["bgm_gain"] = 0.5
        p = os.path.join(TMP, "plan_bgm.json"); json.dump(raw, open(p, "w"))
        r = render.render(resolved(raw, p), FULL, "cpu", log=lambda *a: None)
        assert r["ok"], r
    return FULL


@case
def scrub_render_frames_are_exact():
    full_render()
    out = os.path.join(TMP, "scrub_render")
    r, code = reel("scrub", PLAN, "--cuts", "--render", FULL, "--out", out)
    assert code == 0 and not r["result"]["frame_mismatch"], r["result"]["frame_mismatch"]
    idx = json.load(open(os.path.join(out, "index.json")))
    from PIL import Image
    by_f = {x["frame"]: color_of(Image.open(x["clean"])) for x in idx["frames"]}
    assert by_f[74] == "blue" and by_f[75] == "green", by_f          # hard cut: the cut frame is already the next shot
    assert by_f[29] == "red" and by_f[45] == "blue", by_f              # the frame before the fade is still s1 and the end frame is s2
    r, _ = reel("scrub", PLAN, "--range", "0:4", "--samples", "5", "--render", FULL, "--out", out + "_r")
    fr = json.load(open(os.path.join(out + "_r", "index.json")))["frames"]
    assert [x["frame"] for x in fr] == [0, 29, 59, 89, 119], fr
    # the last frame of an mpv subtitle-burned video carries a discard flag and cannot be decoded: step back one frame and report it honestly instead of pretending it is 119
    assert [x["actual_frame"] for x in fr] == [0, 29, 59, 89, 118], [x["actual_frame"] for x in fr]
    assert [x["frame"] for x in r["result"]["frame_mismatch"]] == [119]


# ================= 3. edit / undo / history =================

def fresh(name, **over):
    """Every edit test gets its own directory (.reel-history sits next to the plan) and media points back to the shared media with absolute paths."""
    d = os.path.join(TMP, name)
    os.makedirs(d)
    p = os.path.join(d, "plan.json")
    make_plan(p, src_dir=SRC, duration_policy="fit_last_shot", **over)
    return p


def snap(path):
    with open(path, "rb") as fh:
        return fh.read()


def ops(*o):
    return json.dumps(list(o) if len(o) > 1 else o[0], ensure_ascii=False)


def sig(path):
    c, _ = compiled(path)
    tl = timeline.build(c)
    return c["frames"], c["trans"], c["starts"], [e["id"] for e in tl["tracks"]["V1"]], \
        timeline.text(tl).split("\n", 1)[1]


@case
def edit_trim_commits_revision():
    p = fresh("e_trim")
    before = snap(p)
    r, code = reel("edit", p, "--op", ops({"op": "trim", "id": "s2", "duration": 1.0}))
    assert code == 0, r
    res = r["result"]
    assert res["revision"] == "r1" and res["parent"] == "r0" and res["changed_ids"] == ["s2"], res
    # s2 goes from 45 to 30 frames: s2 runs from 30 to 60; s3 moves forward and fit_last_shot pads it to 60 frames
    assert res["affected_frames"] == [[30, 120]], res["affected_frames"]
    assert res["shifted_ids"] == [] and res["content_changed_ids"] == ["s2", "s3"], res
    assert res["timeline"].startswith("revision=r1") and "V1  s2     video 1.000–2.000  f[30,60)" in res["timeline"]
    assert json.load(open(p))["shots"][1]["duration"] == 1.0
    hd = os.path.join(os.path.dirname(p), ".reel-history")
    assert snap(os.path.join(hd, "r0.json")) != b"" and json.load(open(os.path.join(hd, "r0.json"))) == json.loads(before)
    log = json.load(open(os.path.join(hd, "log.json")))
    assert [x["rev"] for x in log["revisions"]] == ["r0", "r1"] and log["revisions"][1]["op"]["op"] == "trim"
    assert log["revisions"][1]["time"]
    h, _ = reel("history", p)
    assert h["result"]["current"] == "r1" and not h["result"]["dirty"]
    t, _ = reel("timeline", p, "--format", "text")
    assert t["result"]["text"].startswith("revision=r1")


@case
def edit_failure_does_not_touch_disk():
    p = fresh("e_fail")
    before = snap(p)
    hd = os.path.join(os.path.dirname(p), ".reel-history")
    cases = [
        ({"op": "trim", "id": "s1", "duration": 0.5}, "PLAN_INVALID"),       # camera t1=1.4 exceeds the new duration
        ({"op": "move", "id": "s1", "index": 2}, "PLAN_INVALID"),           # a shot with a transition moved to the end
        ({"op": "replace", "id": "s2", "asset": "nope.mp4"}, "PLAN_INVALID"),  # media does not exist
        ({"op": "trim", "id": "s9", "duration": 1}, "SHOT_NOT_FOUND"),
        ({"op": "explode", "id": "s1"}, "OP_INVALID"),
        ({"op": "set-transition", "id": "s1", "type": "fade", "duration": 1.2}, "PLAN_INVALID"),
    ]
    for op, code_ in cases:
        r, code = reel("edit", p, "--op", ops(op))
        assert code == 1 and r["error"]["code"] == code_, (op, r)
        if code_ == "PLAN_INVALID":
            assert r["error"]["detail"]["committed"] is False and r["error"]["detail"]["op"] == op, r
        assert snap(p) == before, f"a failed edit changed plan.json: {op}"
        assert not os.path.exists(hd), f"a failed edit wrote history: {op}"
    # if a later step in a batch is invalid, the earlier step is not written either
    r, code = reel("edit", p, "--op", ops({"op": "trim", "id": "s2", "duration": 1.0},
                                          {"op": "trim", "id": "s1", "duration": 0.5}))
    assert code == 1 and snap(p) == before and not os.path.exists(hd)
    # a successful dry-run writes nothing either
    r, code = reel("edit", p, "--op", ops({"op": "trim", "id": "s2", "duration": 1.0}), "--dry-run")
    assert code == 0 and r["result"]["dry_run"] and r["result"]["revision"] is None
    assert snap(p) == before and not os.path.exists(hd)


@case
def edit_ops_move_insert_replace_transition_camera():
    p = fresh("e_ops")
    r, code = reel("edit", p, "--op", ops({"op": "move", "id": "s3", "before": "s1"}))
    assert code == 0, r
    assert [s["id"] for s in json.load(open(p))["shots"]] == ["s3", "s1", "s2"]
    r, code = reel("edit", p, "--op", ops({"op": "move", "id": "s3", "swap": "s2"}))
    assert code == 0 and [s["id"] for s in json.load(open(p))["shots"]] == ["s2", "s1", "s3"], r
    new = {"id": "n1", "asset": "pattern.png", "duration": 1.0, "space": "cover",
           "camera": [{"op": "move_to", "viewport": [0.2, 0.2, 0.7, 0.7], "t0": 0, "t1": 1.0}]}
    # inserting 1s alone: fit_last_shot would squeeze s3 to 0.5s, so s1's 0.5s fade exceeds half of the adjacent shot and is refused
    r, code = reel("edit", p, "--op", ops({"op": "insert", "shot": new, "after": "s2"}))
    assert code == 1 and r["error"]["code"] == "DURATION_MISMATCH", r
    # atomic batch: insert and shrink s2 to 0.5s at the same time, and the total length still equals the voice-over
    r, code = reel("edit", p, "--op", ops({"op": "insert", "shot": new, "after": "s2"},
                                          {"op": "trim", "id": "s2", "duration": 0.5}))
    assert code == 0, r
    assert [s["id"] for s in json.load(open(p))["shots"]] == ["s2", "n1", "s1", "s3"]
    assert r["result"]["changed_ids"] == ["n1", "s2"] and "V1  n1     image 0.500–1.500  f[15,45)" in r["result"]["timeline"]
    # s2 1.5 to 0.5 + n1 1.0: the start and end of s1/s3 are unchanged afterwards, so only [0,45) is affected
    assert r["result"]["affected_frames"] == [[0, 45]] and r["result"]["adjustments"] == [], r["result"]
    r, code = reel("edit", p, "--op", ops({"op": "replace", "id": "s1", "asset": "pattern.png"}))
    assert code == 0, r
    s1 = next(s for s in json.load(open(p))["shots"] if s["id"] == "s1")
    assert s1["asset"] == "pattern.png" and s1["duration"] == 1.5 and s1["camera"][0]["op"] == "zoom_to"
    assert r["result"]["shifted_ids"] == [] and r["result"]["content_changed_ids"] == ["s1"], r["result"]
    r, code = reel("edit", p, "--op", ops({"op": "set-transition", "id": "n1", "type": "dissolve", "duration": 0.3}))
    assert code == 0 and "tr_out=dissolve 9f" in r["result"]["timeline"], r
    r, code = reel("edit", p, "--op", ops({"op": "set-transition", "id": "s1", "type": None}))
    assert code == 0 and "transition_out" not in next(s for s in json.load(open(p))["shots"] if s["id"] == "s1")
    r, code = reel("edit", p, "--op", ops({"op": "set-camera", "id": "s3",
                                           "space": "cover", "camera": [{"op": "move_to", "viewport": [0, 0, 0.5, 0.5], "t0": 0.2, "t1": 1.0}]}))
    assert code == 0 and "cam=move_to" in [ln for ln in r["result"]["timeline"].splitlines() if "V1  s3" in ln][0], r
    assert r["result"]["revision"] == "r7", r["result"]["revision"]


@case
def edit_base_conflict_and_external_edit():
    p = fresh("e_base")
    r, code = reel("edit", p, "--base", "r0", "--op", ops({"op": "trim", "id": "s2", "duration": 1.2}))
    assert code == 0 and r["result"]["revision"] == "r1", r
    before = snap(p)
    r, code = reel("edit", p, "--base", "r0", "--op", ops({"op": "trim", "id": "s2", "duration": 1.0}))
    assert code == 1 and r["error"]["code"] == "REV_CONFLICT" and r["error"]["detail"]["current"] == "r1", r
    assert snap(p) == before
    raw = json.load(open(p)); raw["shots"][2]["duration"] = 1.4; json.dump(raw, open(p, "w"))  # edited externally by hand
    h, _ = reel("history", p)
    assert h["result"]["current"] == "r1*" and h["result"]["dirty"]
    r, code = reel("edit", p, "--base", "r1", "--op", ops({"op": "trim", "id": "s2", "duration": 1.0}))
    assert code == 1 and r["error"]["code"] == "REV_CONFLICT"
    r, code = reel("edit", p, "--op", ops({"op": "trim", "id": "s2", "duration": 1.0}))
    assert code == 0 and r["result"]["revision"] == "r3" and r["result"]["parent"] == "r2", r
    log = json.load(open(os.path.join(os.path.dirname(p), ".reel-history", "log.json")))
    assert [x["op"]["op"] for x in log["revisions"]] == ["init", "trim", "external_edit", "trim"]
    assert json.load(open(os.path.join(os.path.dirname(p), ".reel-history", "r2.json")))["shots"][2]["duration"] == 1.4


@case
def undo_restores_compiled_result():
    p = fresh("e_undo")
    orig_raw, orig = snap(p), sig(p)
    reel("edit", p, "--op", ops({"op": "trim", "id": "s2", "duration": 1.0}))
    reel("edit", p, "--op", ops({"op": "move", "id": "s3", "before": "s2"},
                                {"op": "set-transition", "id": "s1", "type": "slideleft", "duration": 0.3}))
    assert sig(p) != orig
    r, code = reel("undo", p, "--to", "r0", "--dry-run")
    assert code == 0 and r["result"]["revision"] is None and sig(p) != orig
    r, code = reel("undo", p, "--to", "r0", "--base", "r2")
    assert code == 0 and r["result"]["revision"] == "r3" and r["result"]["parent"] == "r2", r
    assert sig(p) == orig, (sig(p)[:3], orig[:3])
    assert json.loads(snap(p)) == json.loads(orig_raw)
    assert r["result"]["timeline"].split("\n", 1)[1] == orig[4]
    r, code = reel("undo", p, "--to", "r1")
    assert code == 0 and json.load(open(p))["shots"][1]["duration"] == 1.0
    r, code = reel("undo", p, "--to", "r42")
    assert code == 1 and r["error"]["code"] == "REV_NOT_FOUND"
    h, _ = reel("history", p)
    assert [x["rev"] for x in h["result"]["revisions"]] == ["r0", "r1", "r2", "r3", "r4"]
    assert h["result"]["revisions"][3]["op"] == {"op": "undo", "to": "r0"}



# ================= 4. preview --range / check =================

def pcm(path, a=0.0, d=None, rate=48000):
    import array
    args = ["ffmpeg", "-v", "error", "-i", path, "-ss", f"{a}"] + (["-t", f"{d}"] if d else []) + \
        ["-vn", "-ac", "1", "-ar", str(rate), "-f", "f32le", "-"]
    x = array.array("f"); x.frombytes(subprocess.run(args, capture_output=True, check=True).stdout)
    return x


def colors(video, n=None):
    """Dominant colour per frame (read from a PNG sequence)."""
    from PIL import Image
    d = tempfile.mkdtemp(dir=TMP)
    ff("-i", video, os.path.join(d, "%04d.png"))
    fs = sorted(os.listdir(d))
    return [color_of(Image.open(os.path.join(d, f))) for f in fs]


@case
def preview_range_matches_full_render_time_axis():
    full_render()
    pb = os.path.join(TMP, "plan_bgm.json")
    out = os.path.join(TMP, "rng30.mp4")
    r, code = reel("preview", pb, "--range", "1:3", "--fps", "30", "--size", "270x480", "--out", out)
    assert code == 0 and r["result"]["ok"], r
    res = r["result"]
    assert res["range_f"] == [30, 90] and res["shots"] == ["s1", "s2", "s3"] and res["rendered_f"] == [0, 120], res
    pc, fc = colors(out), colors(FULL)
    assert len(pc) == 60 and pc == fc[30:90], (pc, fc[30:90])
    assert pc.index("green") == 45                                          # the hard cut is at f75 of the full video = preview frame 45
    # 15fps sampling: preview frame k = full-video frame 30+2k
    out15 = os.path.join(TMP, "rng15.mp4")
    r, code = reel("preview", pb, "--range", "1:3", "--out", out15, "--size", "270x480")
    assert code == 0 and r["result"]["preview_fps"] == 15, r
    p15 = colors(out15)
    assert len(p15) == 30 and p15 == [fc[30 + 2 * k] for k in range(30)], p15
    # audio (voice-over + looping BGM, the loop point 1.7s lies inside the range) matches the full video at the same moments
    a, b = pcm(out), pcm(FULL, 1.0, 2.0)
    n = min(len(a), len(b)) - 2400
    err = sum((a[k] - b[k]) ** 2 for k in range(1200, n)) / sum(b[k] ** 2 for k in range(1200, n))
    assert len(a) > 0.95 * 96000 and err < 0.02, (len(a), err)
    shifted = sum((a[k] - b[k + 240]) ** 2 for k in range(1200, n)) / sum(b[k] ** 2 for k in range(1200, n))
    assert shifted > 10 * err, (err, shifted)                                 # a 5ms offset is clearly worse: the comparison is discriminating


@case
def preview_range_args_and_old_preview_still_work():
    r, code = reel("preview", PLAN, "--range", "1:3", "--size", "300x300", "--out", os.path.join(TMP, "x.mp4"))
    assert code == 1 and r["error"]["code"] == "ARG_INVALID"
    r, code = reel("preview", PLAN, "--out", os.path.join(TMP, "x.mp4"))
    assert code == 1 and r["error"]["code"] == "ARG_INVALID"
    r, code = reel("preview", PLAN, "s1", "--out", os.path.join(TMP, "s1.mp4"))
    assert code == 0 and r["result"]["ok"], r


# for the self-check: camera-move shots use textured images / moving video (a solid-colour push-in is identical in every frame and would count as a freeze)
CPLAN = os.path.join(TMP, "check_plan.json")
CVID = os.path.join(TMP, "check.mp4")


def check_render():
    if not os.path.isfile(CVID):
        ff("-f", "lavfi", "-i", "testsrc2=s=540x960:r=30:d=4", "-pix_fmt", "yuv420p", os.path.join(SRC, "moving.mp4"))
        ff("-f", "lavfi", "-i", "sine=f=440:d=4.0", "-ar", "48000", "-af", "volume=1.6", os.path.join(SRC, "loud.wav"))
        p = make_plan(CPLAN, audio={"voice": "loud.wav", "subtitles": "v.vtt"})
        p = json.load(open(CPLAN))
        p["shots"][0]["asset"] = "pattern.png"; p["shots"][1]["asset"] = "moving.mp4"
        json.dump(p, open(CPLAN, "w"))
        r = render.render(resolved(p, CPLAN), CVID, "cpu", log=lambda *a: None)
        assert r["ok"], r
    return CVID


def st(rep, name):
    return next(x for x in rep["items"] if x["check"] == name)


@case
def check_clean_render_and_four_states():
    check_render()
    r, code = reel("check", CVID, "--plan", CPLAN)
    assert code == 0, r
    rep = r["result"]
    bad = [(x["check"], x["status"], x["measured"]) for x in rep["items"] if x["status"] in ("fail", "needs_review")]
    assert rep["status"] == "pass" and not bad, bad
    assert st(rep, "black_frames")["status"] == "pass"
    fr = st(rep, "freeze")
    assert fr["status"] == "pass" and fr["evidence"] and all(e["shots"] == ["s3"] for e in fr["evidence"]), fr
    assert st(rep, "audio_clipping")["measured"]["clipped_samples"] == 0
    assert -18 <= st(rep, "loudness")["measured"]["integrated_lufs"] <= -10, st(rep, "loudness")
    assert st(rep, "subtitles_in_range")["status"] == "pass"
    nc = [x for x in rep["items"] if x["status"] == "not_checked"]
    assert {x["check"] for x in nc} >= {"caption_legibility", "pin_occlusion", "voice_visual_alignment"}
    assert rep["counts"]["not_checked"] == len(nc) and "not_checked" in rep["status_scope"]
    # no plan: subtitles out of range gives not_checked; s3's freeze cannot be judged for intent, giving needs_review (not pass)
    r, _ = reel("check", CVID)
    rep = r["result"]
    assert st(rep, "subtitles_in_range")["status"] == "not_checked" and st(rep, "freeze")["status"] == "needs_review"
    assert rep["status"] == "needs_review", rep["status"]


@case
def check_detects_injected_black_frames():
    check_render()
    bad = os.path.join(TMP, "black.mp4")
    ff("-i", CVID, "-vf", "drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill:enable='between(n,60,65)'",
       "-c:a", "copy", bad)
    r, _ = reel("check", bad, "--plan", CPLAN)
    b = st(r["result"], "black_frames")
    assert b["status"] == "fail" and r["result"]["status"] == "fail", b
    assert b["evidence"][0]["frames"][0] == 60 and abs(b["evidence"][0]["t"][0] - 2.0) < 0.01, b["evidence"]


@case
def check_detects_injected_clipping():
    check_render()
    bad = os.path.join(TMP, "clip.mp4")
    ff("-i", CVID, "-c:v", "copy", "-af", "volume=enable='between(t,1,1.5)':volume=8,asoftclip=type=hard",
       "-c:a", "aac", bad)
    r, _ = reel("check", bad, "--plan", CPLAN)
    c = st(r["result"], "audio_clipping")
    assert c["status"] == "fail" and c["measured"]["clipped_samples"] > 100, c
    assert c["evidence"] and all(0.95 <= e["t"][0] <= e["t"][1] <= 1.55 for e in c["evidence"]), c["evidence"]


@case
def check_freeze_in_moving_shot_fails():
    check_render()
    bad = os.path.join(TMP, "freeze.mp4")
    ff("-i", CVID, "-filter_complex", "[0:v]split[a][b];[a][b]freezeframes=first=3:last=40:replace=3[v]",
       "-map", "[v]", "-map", "0:a", "-c:a", "copy", bad)
    r, _ = reel("check", bad, "--plan", CPLAN)
    fr = st(r["result"], "freeze")
    assert fr["status"] == "fail" and any("s1" in e["shots"] and e["verdict"] == "fail" for e in fr["evidence"]), fr



if __name__ == "__main__":
    fails = [r for r in RESULTS if not r[1]]
    for name, ok, msg in RESULTS:
        print(("✅ " if ok else "❌ ") + name + ("" if ok else "\n   " + msg.replace("\n", "\n   ")))
    print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
