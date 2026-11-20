#!/usr/bin/env python3
"""MV 契约与歌词时间源的故障注入矩阵（无需 pytest）：python3 tests/test_mv.py

合成一首带内嵌歌词流的「歌」、一支 MV、一份字层；每个检查都注入故障证明会拦，同时跑合格对照证明不误拦。
1 reel lyrics import/verify   2 song_identity / av_duration / song_level   3 lyrics_on_screen / lyrics_not_early
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, traceback
import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REEL = os.path.join(ROOT, "reel.py")
TMP = tempfile.mkdtemp(prefix="reel_mv_")
STATE = os.path.join(TMP, "state")
RESULTS = []
W, H, FPS, DUR = 360, 640, 30, 12.0
LINES = [("[Verse]", None), ("第一句歌词在这里", (1.0, 3.5)), ("第二句也要唱", (3.5, 6.5)),
         ("[Chorus]", None), ("副歌第一句", (7.0, 9.0)), ("副歌最后一句", (9.0, 11.0))]


def case(group):
    def deco(fn):
        try:
            RESULTS.append((group, fn.__name__, True, fn() or ""))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((group, fn.__name__, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"))
        return fn
    return deco


def ff(*a):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *a], check=True)


def reel(*argv):
    p = subprocess.run([sys.executable, REEL, *map(str, argv)], capture_output=True, text=True,
                       env={**os.environ, "REEL_STATE_DIR": STATE})
    return json.loads(p.stdout), p.returncode


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def j(p, obj=None):
    if obj is None:
        return json.load(open(p, encoding="utf-8"))
    json.dump(obj, open(p, "w", encoding="utf-8"), ensure_ascii=False)
    return p


def P(n):
    return os.path.join(TMP, n)


# ---------------- fixtures ----------------
def srt(lines, shift=0.0):
    out, k = [], 1
    fmt = lambda t: f"00:00:{int(t):02d},{int(round((t % 1) * 1000)):03d}"
    for text, span in lines:
        a, b = (span or (0, 0))
        if span is None:  # section cue: zero-length, at the next line's start
            nxt = next(s for _, s in lines[lines.index((text, span)) + 1:] if s)
            a = b = nxt[0]
        out.append(f"{k}\n{fmt(a + shift)} --> {fmt(b + shift)}\n{text}\n"); k += 1
    return "\n".join(out)


def make_song():
    rng = np.random.default_rng(1)
    n = int(DUR * 48000); t = np.arange(n) / 48000
    music = 0.25 * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t)) + 0.05 * rng.standard_normal(n)
    stereo = np.stack([music, np.roll(music, 37)], 1).astype(np.float32)
    stereo.tofile(P("song.f32"))
    open(P("song.srt"), "w", encoding="utf-8").write(srt(LINES))
    ff("-f", "f32le", "-ar", "48000", "-ac", "2", "-i", P("song.f32"), "-i", P("song.srt"),
       "-map", "0:a", "-map", "1:s", "-c:a", "aac", "-b:a", "256k", "-c:s", "mov_text", P("song.m4a"))
    open(P("lyrics.md"), "w", encoding="utf-8").write("# t\n\n## Lyrics\n\n" + "\n".join(t for t, _ in LINES) + "\n[End]\n\n## Notes\n")


def make_mv(name, audio_filter="anull", vdur=DUR, adur=None):
    ff("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={FPS}:d={vdur}", "-i", P("song.m4a"), "-map", "0:v", "-map", "1:a",
       "-af", audio_filter, "-t", str(adur or vdur), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
       "-ar", "48000", "-shortest" if adur is None else "-y", P(name))
    return P(name)


def make_layer(name, lines, shift=0.0, drop=None):
    """字层：每句在 [t0+shift, t1+0.2] 期间，每个字一块白方块（逐字在行内均分出现）。"""
    enc = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{W}x{H}",
                            "-r", str(FPS), "-i", "-", "-c:v", "ffv1", "-pix_fmt", "gray", P(name)], stdin=subprocess.PIPE)
    sung = [(t, s) for t, s in lines if s]
    for f in range(int(DUR * FPS)):
        t = f / FPS
        im = Image.new("L", (W, H), 0); d = ImageDraw.Draw(im)
        for k, (text, (a, b)) in enumerate(sung):
            if k == drop:
                continue
            a2 = a + shift
            nxt = sung[k + 1][1][0] + shift if k + 1 < len(sung) else b + 1
            if not (a2 <= t < min(b + 0.2, nxt)):
                continue
            n = len(text)
            for i in range(n):
                if t >= a2 + (b - a) * 0.85 * i / n:
                    x = 20 + i * 38 + (k % 2) * 6; y = 420 + (k % 2) * 70
                    d.rectangle((x, y, x + 32, y + 40), fill=255)
        enc.stdin.write(im.tobytes())
    enc.stdin.close(); enc.wait()
    return P(name)


def contract(name, mv_layer, song=None, lyrics=None, **over):
    song = song or P("song.m4a")
    c = {"version": "reel.contract.v1", "kind": "mv", "output": {"width": W, "height": H, "fps": FPS},
         "songs": [{"path": song, "sha256": sha(song), "at": 0.0, "policy": "full"}]}
    if lyrics:
        c["lyrics"] = {"path": lyrics, "sha256": sha(lyrics)}
        c["text_layer"] = {"path": mv_layer, "sha256": sha(mv_layer)}
    c.update(over)
    return j(P(name), c)


def items(report_path):
    r = j(report_path); r = r.get("result", r)
    return {it["check"]: it for it in r["items"]}, r["gate"]


def check(video, con):
    rp = P(os.path.basename(video) + os.path.basename(con) + ".rep.json")
    _, rc = reel("check", video, "--contract", con, "--report", rp)
    it, g = items(rp)
    return it, g, rc


# ---------------- 1 lyrics ----------------
def lyrics_control_pass():
    d, rc = reel("lyrics", "import", P("song.m4a"), "--lyrics", P("lyrics.md"), "--out", P("lyrics.json"))
    assert rc == 0 and d["result"]["status"] == "pass", d
    L = j(P("lyrics.json"))
    assert [l["text"] for l in L["lines"]] == [t for t, s in LINES if s] and L["precision"] == "line"
    return f"{len(L['lines'])} lines from {L['source']}"


def lyrics_unsung_line_fails():
    open(P("l_extra.md"), "w", encoding="utf-8").write(open(P("lyrics.md"), encoding="utf-8").read().replace("副歌第一句", "副歌第一句\n这一句没唱"))
    d, rc = reel("lyrics", "import", P("song.m4a"), "--lyrics", P("l_extra.md"), "--out", P("x1.json"))
    assert rc == 1 and d["error"]["code"] == "LYRICS_CHECK_FAILED" and not os.path.exists(P("x1.json")), d


def lyrics_unwritten_line_fails():
    open(P("l_miss.md"), "w", encoding="utf-8").write(open(P("lyrics.md"), encoding="utf-8").read().replace("第二句也要唱\n", ""))
    d, rc = reel("lyrics", "import", P("song.m4a"), "--lyrics", P("l_miss.md"), "--out", P("x2.json"))
    assert rc == 1 and d["error"]["detail"]["coverage"]["extra"], d


def lyrics_changed_word_needs_review():
    open(P("l_diff.md"), "w", encoding="utf-8").write(open(P("lyrics.md"), encoding="utf-8").read().replace("副歌最后一句", "副歌最末一句"))
    d, rc = reel("lyrics", "import", P("song.m4a"), "--lyrics", P("l_diff.md"), "--out", P("x3.json"))
    assert rc == 0 and d["result"]["status"] == "needs_review" and d["result"]["sung_differs"], d
    assert j(P("x3.json"))["lines"][-1]["text"] == "副歌最后一句", "default display must be what is sung"


def lyrics_no_stream_refuses():
    ff("-i", P("song.m4a"), "-map", "0:a", "-c", "copy", P("nolyr.m4a"))
    d, rc = reel("lyrics", "import", P("nolyr.m4a"), "--lyrics", P("lyrics.md"), "--out", P("x4.json"))
    assert rc == 1 and d["error"]["code"] == "LYRICS_NO_TIMING", d


def lyrics_tamper_detected():
    L = j(P("lyrics.json")); L["lines"][1]["t0"] -= 0.4; j(P("tampered.json"), L)
    d, rc = reel("lyrics", "verify", P("tampered.json"))
    assert rc == 1 and d["error"]["code"] == "LYRICS_TAMPERED", d


# ---------------- 2 audio ----------------
def audio_control_pass():
    mv = make_mv("ok.mp4"); con = contract("c_ok.json", None)
    it, g, rc = check(mv, con)
    for k in ("contract_files", "av_duration_match", "song_identity", "song_level"):
        assert it[k]["status"] == "pass", (k, it[k])
    return "AAC re-encode of the untouched song passes"


def audio_truncated_fails():
    mv = make_mv("cut.mp4", adur=9.0, vdur=9.0); it, g, rc = check(mv, contract("c_cut.json", None))
    assert rc == 1 and it["song_identity"]["status"] == "fail", it["song_identity"]


def audio_delay_30ms_fails():
    mv = make_mv("late.mp4", "adelay=30|30"); it, g, rc = check(mv, contract("c_late.json", None))
    assert it["song_identity"]["status"] == "fail" and any(abs(e.get("lag_ms", 0)) >= 25 for e in it["song_identity"]["evidence"])


def audio_gain_minus1db_fails():
    mv = make_mv("quiet.mp4", "volume=-1dB"); it, g, rc = check(mv, contract("c_quiet.json", None))
    assert it["song_identity"]["status"] == "fail" and it["song_level"]["status"] == "fail"


def av_duration_mismatch_fails():
    mv = make_mv("long.mp4", vdur=DUR + 1.0, adur=DUR + 1.0)
    ff("-i", mv, "-i", P("song.m4a"), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", P("avmis.mp4"))
    it, g, rc = check(P("avmis.mp4"), contract("c_avmis.json", None))
    assert it["av_duration_match"]["status"] == "fail", it["av_duration_match"]


def stale_song_hash_fails():
    con = contract("c_stale.json", None); c = j(con); c["songs"][0]["sha256"] = "0" * 64; j(con, c)
    it, g, rc = check(P("ok.mp4"), con)
    assert it["contract_files"]["status"] == "fail" and rc == 1


def _effects(name, kind):
    """An effects layer the length of the song (48 kHz float stereo wav). kind=beeps: 1 kHz bursts unrelated to the song;
    kind=gain: song x (10^(-1/20) - 1), i.e. a -1 dB gain change dressed up as an "effect"."""
    raw = np.fromfile(P("song.f32"), np.float32).reshape(-1, 2); t = np.arange(len(raw)) / 48000
    if kind == "beeps":
        g = ((t % 4.0) < 0.6).astype(np.float32) * 0.2 * np.sin(2 * np.pi * 1000 * t).astype(np.float32)
        e = np.stack([g, g], 1)
    else:
        e = raw * np.float32(10 ** (-1 / 20) - 1)
    e.astype("<f4").tofile(P(name + ".f32"))
    ff("-f", "f32le", "-ar", "48000", "-ac", "2", "-i", P(name + ".f32"), "-c:a", "pcm_f32le", P(name + ".wav"))
    mix = (raw + e).astype("<f4"); mix.tofile(P(name + "_mix.f32"))
    ff("-f", "lavfi", "-i", f"testsrc2=s={W}x{H}:r={FPS}:d={DUR}", "-f", "f32le", "-ar", "48000", "-ac", "2", "-i", P(name + "_mix.f32"),
       "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "320k", "-ar", "48000", "-shortest", P(name + ".mp4"))
    return P(name + ".wav"), P(name + ".mp4")


def audio_effects_declared_pass():
    wav, mv = _effects("fx", "beeps")
    it, g, rc = check(mv, contract("c_fx.json", None, effects={"path": wav, "sha256": sha(wav), "reason": "用户：加音效"}))
    for k in ("contract_files", "song_identity", "song_level"):
        assert it[k]["status"] == "pass", (k, it[k])
    return "song + declared effects layer passes"


def audio_effects_undeclared_fails():
    it, g, rc = check(P("fx.mp4"), contract("c_fx_no.json", None))
    assert rc == 1 and it["song_identity"]["status"] == "fail", it["song_identity"]


def audio_effects_disguised_gain_fails():
    wav, mv = _effects("fxg", "gain")
    it, g, rc = check(mv, contract("c_fxg.json", None, effects={"path": wav, "sha256": sha(wav), "reason": "用户：加音效"}))
    assert it["song_identity"]["status"] == "fail" and any("effects_song_corr" in e for e in it["song_identity"]["evidence"]), it["song_identity"]


def audio_effects_need_reason():
    con = contract("c_fx_r.json", None, effects={"path": P("fx.wav"), "sha256": sha(P("fx.wav")), "reason": " "})
    d, rc = reel("check", P("fx.mp4"), "--contract", con)
    assert rc == 1 and d["error"]["code"] == "CONTRACT_INVALID", d


# ---------------- 3 lyrics on screen ----------------
def text_control_pass():
    lay = make_layer("tl_ok.mkv", LINES); it, g, rc = check(P("ok.mp4"), contract("c_tok.json", lay, lyrics=P("lyrics.json")))
    assert it["lyrics_on_screen"]["status"] == "pass" and it["lyrics_not_early"]["status"] == "pass", (it["lyrics_on_screen"], it["lyrics_not_early"])


def text_missing_line_fails():
    lay = make_layer("tl_miss.mkv", LINES, drop=2); it, g, rc = check(P("ok.mp4"), contract("c_tmiss.json", lay, lyrics=P("lyrics.json")))
    ev = it["lyrics_on_screen"]["evidence"]
    assert it["lyrics_on_screen"]["status"] == "fail" and ev[0]["text"] == "副歌第一句", ev


def text_early_fails():
    lay = make_layer("tl_early.mkv", LINES, shift=-0.3); it, g, rc = check(P("ok.mp4"), contract("c_tearly.json", lay, lyrics=P("lyrics.json")))
    assert it["lyrics_not_early"]["status"] == "fail", it["lyrics_not_early"]
    return f"{len(it['lyrics_not_early']['evidence'])} early lines caught"


def text_layer_wrong_length_fails():
    ff("-i", P("tl_ok.mkv"), "-t", "8", "-c", "copy", P("tl_short.mkv"))
    it, g, rc = check(P("ok.mp4"), contract("c_tshort.json", P("tl_short.mkv"), lyrics=P("lyrics.json")))
    assert it["lyrics_on_screen"]["status"] == "fail"


def contract_required_cannot_be_dropped():
    """profile 去掉不了契约的必查项：lyrics 有、text_layer 没给 → 契约本身不合法。"""
    c = j(P("c_tok.json")); c.pop("text_layer"); j(P("c_nolayer.json"), c)
    d, rc = reel("check", P("ok.mp4"), "--contract", P("c_nolayer.json"))
    assert rc == 1 and d["error"]["code"] == "CONTRACT_INVALID", d


def lyrics_end_fix_only_shortens_with_evidence():
    """行尾更正：只许提前、必须有证据、cue 号和原文都要对；改了更正文件 verify 就作废。"""
    ok = j(P("fix_ok.json"), [{"cue": 2, "text": "第二句也要唱", "t1": 5.5, "evidence": "逐字时间：最后一个字 5.4 s 唱完，之后是器乐"}])
    d, rc = reel("lyrics", "import", P("song.m4a"), "--lyrics", P("lyrics.md"), "--out", P("lyrics_fix.json"), "--end-fixes", ok)
    assert rc == 0 and d["result"]["end_fixes"][0]["t1"] == 5.5, d
    ln = next(x for x in j(P("lyrics_fix.json"))["lines"] if x["cue_id"] == 2)
    assert ln["t1"] == 5.5 and ln["t1_stream"] == 6.5, ln
    d, rc = reel("lyrics", "verify", P("lyrics_fix.json")); assert rc == 0, d
    for name, bad in (("longer", {"t1": 7.0}), ("noev", {"evidence": "短"}), ("text", {"text": "别的句子"}), ("cue", {"cue": 99})):
        f = {"cue": 2, "text": "第二句也要唱", "t1": 5.5, "evidence": "逐字时间：最后一个字 5.4 s 唱完", **bad}
        d, rc = reel("lyrics", "import", P("song.m4a"), "--lyrics", P("lyrics.md"), "--out", P(f"lyrics_bad_{name}.json"),
                     "--end-fixes", j(P(f"fix_{name}.json"), [f]))
        assert rc == 1 and d["error"]["code"] == "LYRICS_END_FIX_INVALID" and not os.path.exists(P(f"lyrics_bad_{name}.json")), (name, d)
    j(ok, [{"cue": 2, "text": "第二句也要唱", "t1": 5.0, "evidence": "改过的证据，时间也改了"}])
    d, rc = reel("lyrics", "verify", P("lyrics_fix.json"))
    assert rc == 1 and d["error"]["code"] == "LYRICS_STALE", d
    return "提前+证据 → 采用并记下原行尾；拉长/没证据/原文不对/cue 不存在 → 拒；更正文件改了 → LYRICS_STALE"


def mv_brief(name):
    pdir = P(name); root = P(name + "-交付"); os.makedirs(pdir, exist_ok=True); os.makedirs(root, exist_ok=True)
    return j(os.path.join(pdir, "brief.json"), {"version": "reel.brief.v1", "name": "测试MV", "kind": "mv", "language": "zh-CN",
                                                "deliverable": "sample", "delivery_root": root, "must_not_change": ["歌"],
                                                "voice": {"mode": "recorded"}})


def gate_mv_brief_requires_contract():
    """brief 标 kind=mv：check、deliver --sample 不带契约都拒；带契约就不再报这个错。"""
    bp = mv_brief("p_mvgate")
    d, rc = reel("check", P("ok.mp4"), "--brief", bp)
    assert rc == 1 and d["error"]["code"] == "MV_CONTRACT_REQUIRED" and "template" in d["error"]["detail"], d
    d, rc = reel("deliver", P("ok.mp4"), "--brief", bp, "--version", "v1", "--sample")
    assert rc == 1 and d["error"]["code"] == "MV_CONTRACT_REQUIRED", d
    assert not os.listdir(P("p_mvgate-交付")), "被拒后交付目录必须是空的"
    d, rc = reel("check", P("ok.mp4"), "--brief", bp, "--contract", P("c_tok.json"))
    assert d.get("error", {}).get("code") != "MV_CONTRACT_REQUIRED" and "result" in d, d
    return "kind=mv 无契约 → check/deliver 都报 MV_CONTRACT_REQUIRED（带模板），交付目录不落文件；带契约正常检查"


def gate_contract_requires_brief_kind():
    """带了契约、brief 却没写 kind=mv：必须报 BRIEF_KIND_MISSING；写了 kind=mv 就不报；没有 brief 只带契约不拦。"""
    bp = mv_brief("p_kind")
    b = j(bp); b.pop("kind"); nk = j(os.path.join(os.path.dirname(bp), "brief_nokind.json"), b)
    d, rc = reel("check", P("ok.mp4"), "--brief", nk, "--contract", P("c_tok.json"))
    assert rc == 1 and d["error"]["code"] == "BRIEF_KIND_MISSING" and "kind" in d["error"]["message"], d
    d, rc = reel("check", P("ok.mp4"), "--brief", bp, "--contract", P("c_tok.json"))
    assert d.get("error", {}).get("code") != "BRIEF_KIND_MISSING" and "result" in d, d
    d, rc = reel("check", P("ok.mp4"), "--contract", P("c_tok.json"))
    assert d.get("error", {}).get("code") != "BRIEF_KIND_MISSING" and "result" in d, d
    return "同一份契约：brief 去掉 kind → BRIEF_KIND_MISSING；带 kind=mv、或没有 brief → 不报"


def gate_handwritten_lyrics_refused():
    """契约里的 lyrics 必须是 reel lyrics import 的输出，且对应契约里的歌。"""
    L = j(P("lyrics.json")); L["version"] = "my.timing"; j(P("lyrics_hand.json"), L)
    d, rc = reel("check", P("ok.mp4"), "--contract", contract("c_hand.json", P("tl_ok.mkv"), lyrics=P("lyrics_hand.json")))
    assert rc == 1 and d["error"]["code"] == "CONTRACT_INVALID" and "reel.lyrics.v1" in d["error"]["message"], d
    L = j(P("lyrics.json")); L["song"]["sha256"] = "1" * 64; j(P("lyrics_other.json"), L)
    d, rc = reel("check", P("ok.mp4"), "--contract", contract("c_other.json", P("tl_ok.mkv"), lyrics=P("lyrics_other.json")))
    assert rc == 1 and d["error"]["code"] == "CONTRACT_INVALID" and "song.sha256" in d["error"]["message"], d
    return "手写时间表 / 别的歌的歌词 → CONTRACT_INVALID"


if __name__ == "__main__":
    try:
        make_song()
    except Exception as e:  # noqa: BLE001
        print("fixture failed:", e); sys.exit(2)
    order = [lyrics_control_pass, lyrics_unsung_line_fails, lyrics_unwritten_line_fails, lyrics_changed_word_needs_review,
             lyrics_no_stream_refuses, lyrics_tamper_detected, audio_control_pass, audio_truncated_fails,
             audio_delay_30ms_fails, audio_gain_minus1db_fails, av_duration_mismatch_fails, stale_song_hash_fails,
             audio_effects_declared_pass, audio_effects_undeclared_fails, audio_effects_disguised_gain_fails, audio_effects_need_reason,
             text_control_pass, text_missing_line_fails, text_early_fails, text_layer_wrong_length_fails,
             contract_required_cannot_be_dropped, gate_mv_brief_requires_contract, gate_contract_requires_brief_kind, gate_handwritten_lyrics_refused,
             lyrics_end_fix_only_shortens_with_evidence]
    for fn in order:
        grp = "1 lyrics" if fn.__name__.startswith("lyrics") else ("2 audio" if fn.__name__.startswith(("audio", "av_", "stale"))
                                                                   else ("4 gate" if fn.__name__.startswith("gate") else "3 text"))
        try:
            RESULTS.append((grp, fn.__name__, True, fn() or ""))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((grp, fn.__name__, False, f"{type(e).__name__}: {e}"))
    bad = [r for r in RESULTS if not r[2]]
    for g, n, ok, note in RESULTS:
        print(f"{'PASS' if ok else 'FAIL'}  [{g}] {n}  {str(note)[:160]}")
    print(f"{len(RESULTS) - len(bad)}/{len(RESULTS)} passed")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if bad else 0)
