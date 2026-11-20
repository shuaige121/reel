#!/usr/bin/env python3
"""Fault injection for the delivery-directory Bash guard + the completion gate (Stop hook). python3 tests/test_hooks.py [--log FILE]

The hooks run as /usr/bin/python3 subprocesses (hooks.json also uses the system python3) and the state directory is a temporary REEL_STATE_DIR.
"""
import datetime, json, os, shutil, subprocess, sys, tempfile, traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GUARD = os.path.join(ROOT, "hooks", "guard_delivery.py")
GATE = os.path.join(ROOT, "hooks", "deliver_gate.py")
REEL = os.path.join(ROOT, "reel.py")
HOOK_PY = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
TMP = tempfile.mkdtemp(prefix="reel_hooks_")
STATE = os.path.join(TMP, "state")
ENV = {**os.environ, "REEL_STATE_DIR": STATE}
RESULTS = []


def case(group):
    def deco(fn):
        try:
            RESULTS.append((group, fn.__name__, True, fn() or ""))
        except Exception as e:  # noqa: BLE001
            RESULTS.append((group, fn.__name__, False, f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=4)}"))
        return fn
    return deco


def ff(*a):
    subprocess.run([shutil.which("ffmpeg"), "-y", "-v", "error", *a], check=True)


# ---------- a registered task (really runs reel register) ----------
PROJ = os.path.join(TMP, "proj")
ROOTDIR = os.path.join(TMP, "iCloud 交付", "客户A")          # with a space and Chinese, like an iCloud path
OTHER = os.path.join(TMP, "iCloud 交付", "别的项目")
for d in (PROJ, ROOTDIR, OTHER, os.path.join(PROJ, "素材")):
    os.makedirs(d, exist_ok=True)
BRIEF = os.path.join(PROJ, "brief.json")
json.dump({"version": "reel.brief.v1", "name": "片", "language": "zh-CN", "deliverable": "final",
           "delivery_root": ROOTDIR, "must_not_change": [], "voice": {"mode": "none"}},
          open(BRIEF, "w", encoding="utf-8"), ensure_ascii=False)
r = subprocess.run([sys.executable, REEL, "register", BRIEF], capture_output=True, text=True, env=ENV)
assert r.returncode == 0, r.stdout + r.stderr
LINK = os.path.join(TMP, "link-to-delivery")
os.symlink(ROOTDIR, LINK)


def guard(cmd, cwd=PROJ, state=STATE):
    p = subprocess.run([HOOK_PY, GUARD], input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                                                           "cwd": cwd, "permission_mode": "bypassPermissions"}),
                       capture_output=True, text=True, env={**os.environ, "REEL_STATE_DIR": state})
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return None
    o = json.loads(p.stdout)["hookSpecificOutput"]
    assert o["permissionDecision"] == "deny", o
    return o["permissionDecisionReason"]


R = ROOTDIR
Q = lambda s: "'" + s + "'"         # noqa: E731

DENY = [
    f"cp out/final.mp4 {Q(R)}/",
    f"cp -f out/final.mp4 {Q(R + '/片-v9.mp4')}",
    f"mv out/final.mp4 {Q(R)}",
    f"rsync -a out/ {Q(R)}/",
    f"ditto out/final.mp4 {Q(R + '/x.mp4')}",
    f"cp -t {Q(R)} out/final.mp4",
    f"ffmpeg -y -i in.mp4 -c copy {Q(R + '/out.mp4')}",
    f"ffmpeg -i in.mp4 -vf scale=1080:1920 -c:a aac {Q(R + '/out.mp4')} 2>&1 | tail -3",
    f"cat out/final.mp4 > {Q(R + '/y.mp4')}",
    f"tee {Q(R + '/a.txt')} < notes.txt",
    f"cd {Q(R)} && cp ~/projects/x/final.mp4 .",
    f"reel deliver out/final.mp4 --brief brief.json --version v2; cp out/bad.mp4 {Q(R)}/",
    f"reel deliver out/final.mp4 --brief brief.json --version v2 && mv out/bad.mp4 {Q(R + '/片-v2.mp4')}",
    f"DEST={Q(R)}; cp out/final.mp4 \"$DEST/\"",
    f"python3 -c \"import shutil; shutil.copy('out/final.mp4', '{R}/x.mp4')\"",
    f"python3 - <<'EOF'\nimport shutil\nshutil.copy('out/final.mp4', '{R}/x.mp4')\nEOF",
    f"cp out/final.mp4 {Q(LINK)}/",
    f"rm {Q(R + '/片-v1.mp4')}",
    f"touch {Q(R + '/x')}",
    f"reel render plan.json --out {Q(R + '/成片.mp4')}",
    f"cd ~/projects\ncp out/final.mp4 {Q(R)}/",
    f"find {Q(R)} -name '*.mp4' -delete",
    f"ssh host \"cat > {R}/x.mp4\" < out/final.mp4",
    f"if [ -f out/final.mp4 ]; then cp out/final.mp4 {Q(R)}/; fi",
    f"for f in out/*.mp4; do cp \"$f\" {Q(R)}/; done",
    f"cp out/final.mp4 \"$(echo {R})/\"",
    f"D={Q(R)}\nffmpeg -i in.mp4 \"$D/o.mp4\"",
    f"rsync -an out/ {Q(R)}/ > {Q(R + '/diff.txt')}",
    f"tar -xf a.tar -C {Q(R)}",
    f"tar -cf {Q(R + '/a.tar')} out/",
]
ALLOW = [
    f"ls -la {Q(R)}",
    f"shasum -a 256 {Q(R + '/片-v1.mp4')}",
    f"ffprobe -v error {Q(R + '/片-v1.mp4')}",
    f"open -R {Q(R + '/片-v1.mp4')}",
    f"cp {Q(R + '/片-v1.mp4')} ~/projects/proj/ref.mp4",
    f"ffmpeg -i {Q(R + '/片-v1.mp4')} -frames:v 1 preview/f.png",
    f"cp ~/Downloads/raw/*.mov 素材/",
    f"cp out/final.mp4 {Q(OTHER)}/",
    "reel deliver out/final.mp4 --brief brief.json --version v2",
    f"python3 ~/src/reel/reel.py receipt .reel/receipts/片-v1.json",
    ': "${REEL_ENGINE_ROOT:=$HOME/src/reel}"\nreel() { python3 "$REEL_ENGINE_ROOT/reel.py" "$@"; }\n'
    f"reel check {Q(R + '/片-v1.mp4')} --brief brief.json",
    f"find {Q(R)} -name '*.mp4'",
    ': "${REEL_ENGINE_ROOT:=$HOME/src/reel}"\n'
    'test -f "$REEL_ENGINE_ROOT/reel.py" || { echo "reel.py missing" >&2; return 1 2>/dev/null || exit 1; }\n'
    'command -v ffmpeg >/dev/null || { echo "ffmpeg not found" >&2; return 1 2>/dev/null || exit 1; }\n'
    'reel() { python3 "$REEL_ENGINE_ROOT/reel.py" "$@"; }\n: "${REEL_PROJECT:?set REEL_PROJECT}"\n'
    'P="$HOME/reel-projects/$REEL_PROJECT"; mkdir -p "$P/preview" && cd "$P"\n'
    f"reel deliver 成片.mp4 --brief brief.json --version v4 && ls -la {Q(R)}",
    "ffmpeg -y -i in.mp4 -c copy out/final.mp4",
    "git status --short && python3 tests/test_gate.py",
    f"python3 - <<'EOF'\nprint('改代码，不碰交付目录')\nEOF\nls -la {Q(R)}",
    f"python3 tests/test_gate.py; shasum -a 256 {Q(R + '/片-v1.mp4')}",
    f"rsync -an --delete out/ {Q(R)}/",
    f"rsync --dry-run -av out/ {Q(R)}/",
    f"tar -tf {Q(R + '/archive.tar')}",
    f"tar --list -f {Q(R + '/archive.tar')}",
]


@case("7a Bash guard")
def denies_writes_into_registered_root():
    missed = [c for c in DENY if not guard(c)]
    assert not missed, "not blocked: \n" + "\n".join(missed)
    return f"all {len(DENY)} forms are refused (cp/mv/rsync/ditto/-t, ffmpeg output, redirects, tee, relative paths after cd, cp/mv after deliver, " \
           "variable targets, python -c / heredoc, symlinks, rm/touch, reel render --out, multi-line scripts, find -delete, ssh)"


@case("7a Bash guard")
def allows_reads_materials_and_deliver():
    wrong = [(c, guard(c)) for c in ALLOW if guard(c)]
    assert not wrong, "wrongly blocked: \n" + "\n".join(f"{c}\n  → {w}" for c, w in wrong)
    return f"all {len(ALLOW)} forms pass: reads / media / unregistered directories / reel deliver / receipt / check (including a PREFLIGHT function definition)"


@case("7a Bash guard")
def silent_without_registry_or_bad_input():
    empty = os.path.join(TMP, "empty-state")
    assert all(guard(c, state=empty) is None for c in DENY)
    p = subprocess.run([HOOK_PY, GUARD], input="not json", capture_output=True, text=True, env=ENV)
    assert p.returncode == 0 and p.stdout == ""
    return "no registered task means everything passes; input that is not JSON passes silently"


@case("7a Bash guard")
def unregister_releases_root():
    r = subprocess.run([sys.executable, REEL, "register", BRIEF, "--remove"], capture_output=True, text=True, env=ENV)
    assert r.returncode == 0, r.stdout
    try:
        assert guard(DENY[0]) is None
    finally:
        r = subprocess.run([sys.executable, REEL, "register", BRIEF], capture_output=True, text=True, env=ENV)
        assert r.returncode == 0, r.stdout
    assert guard(DENY[0])
    return "after reel register --remove it no longer blocks; registering again restores it"


# ---------- completion gate (Stop hook) ----------
def iso(dt):
    return dt.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


T0 = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=2)


def transcript(name, cmds, final, prompt="做个宣传视频，放 iCloud"):
    """cmds: [(command, result_text)]。"""
    es = [{"type": "user", "uuid": "u0", "timestamp": iso(T0 - datetime.timedelta(hours=1)),
           "message": {"role": "user", "content": "早先的一轮"}},
          {"type": "user", "uuid": "u1", "timestamp": iso(T0), "message": {"role": "user", "content": prompt}}]
    for k, c in enumerate(cmds):
        cmd, res = c[0], c[1]
        ts = c[2] if len(c) > 2 else T0 + datetime.timedelta(seconds=k + 1)
        es.append({"type": "assistant", "timestamp": iso(ts), "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": f"t{k}", "name": "Bash", "input": {"command": cmd}}]}})
        es.append({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"t{k}", "content": res}]}})
    es.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": final}]}})
    p = os.path.join(TMP, name + ".jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(json.dumps(e, ensure_ascii=False) for e in es) + "\n")
    return p


def gate(tp, final, cwd="/nonexistent/projects/other", session="s1", state=STATE):
    p = subprocess.run([HOOK_PY, GATE], input=json.dumps({"transcript_path": tp, "last_assistant_message": final,
                                                          "cwd": cwd, "session_id": session,
                                                          "stop_hook_active": False}),
                       capture_output=True, text=True, env={**os.environ, "REEL_STATE_DIR": state})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)["reason"] if p.stdout.strip() else None


PY_BUILD = [("cd ~/projects/promo && python3 build_video.py", "rendering 12 cards...\nwrote /srv/x/projects/promo/out/final.mp4"),
            ("python3 -c \"import shutil; shutil.copy('out/final.mp4', '/srv/x/iCloud/交付/final.mp4')\"", "")]
CLAIM_DONE = "宣传视频已完成，成片在 iCloud 交付夹 final.mp4，可以发了。"


@case("7b completion gate")
def python_bypass_claim_is_blocked():
    tp = transcript("promo", PY_BUILD, CLAIM_DONE)
    why = gate(tp, CLAIM_DONE, session="s-py")
    assert why and "reel deliver" in why, why
    return "a self-written build_video.py output + a Python copy into the delivery folder (invisible to the guard) + 'done' is blocked"


@case("7b completion gate")
def direct_ffmpeg_render_claim_is_blocked():
    msg = "成片已导出好了：~/projects/p/out/v2.mp4"
    for cmd in ("cd ~/projects/p && ffmpeg -y -i a.mov -i voice.m4a -c:v libx264 out/v2.mp4",
                "FOO=1 /opt/homebrew/bin/ffmpeg -i a.mov out/v2.mp4",
                "python3 ~/src/reel/reel.py render plan.json --out out/v2.mp4",
                "python3 -u make_reel_v2.py --out out/v2.mp4"):
        assert gate(transcript("ff", [(cmd, "")], msg), msg, session="s-ff-" + cmd[:6]), cmd
    return "a done claim after ffmpeg (also with an absolute path / environment prefix), reel.py render or make_*.py output without a receipt is blocked"


@case("7b completion gate")
def honest_status_passes():
    out = []
    for msg in ("v1 已导出，这是待审样片，声音还没试听。", "片子已经做好了，但还未试听，状态是待试听。",
                "渲染完成了，但 reel check 没通过（首帧黑），未交付。", "测试都通过了。本轮无视频交付，只改了检查器代码。",
                "v3 做好了。这是待审样片，等你试听。"):
        tp = transcript("honest", PY_BUILD, msg)
        assert gate(tp, msg, session="s-honest") is None, msg
        out.append(msg[:10])
    for msg in ("视频完成，文件：final.mp4", "视频已完成，旧版未通过，新版可以发了。", "旧版待审。新版成片已经做好了，可以发了。"):
        assert gate(transcript("mixed", PY_BUILD, msg), msg, session="s-mixed-" + msg[:4]), msg
    return (f"an honest status (same or next sentence: sample pending review / awaiting audition / not delivered; whole text: no video delivered this turn) passes {len(out)} cases; "
            "a bare \"video is done\", \"old version failed, the new one can be posted\", and a \"pending review\" in a later sentence are blocked")


def deliver_real(version, sample=False):
    """Really run reel deliver (a passing video + valid review records); the receipt is written to this test's STATE."""
    from_dir = os.path.join(TMP, "media")
    os.makedirs(from_dir, exist_ok=True)
    v = os.path.join(from_dir, f"good-{version}.mp4")
    ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=6", "-f", "lavfi", "-i", "sine=f=440:d=6:sample_rate=48000",
       "-af", "volume=2.44", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "48000", "-t", "6", v)
    txt = os.path.join(from_dir, "t.txt")
    open(txt, "w", encoding="utf-8").write("口播文案。\n")
    sys.path.insert(0, ROOT)
    from reelcore import check
    s = check.sha256(v)
    rv = os.path.join(from_dir, f"rv-{version}.json")
    json.dump({"records": [
        {"video_sha256": s, "check": "final_watch", "decision": "accept", "by": "claude", "note": "完整看完一遍",
         "observations": [{"t": 0, "note": "首帧有画面"}]},
        {"video_sha256": s, "check": "final_listen", "decision": "accept", "by": "user", "note": "听完一遍",
         "quote": "可以"}]}, open(rv, "w", encoding="utf-8"), ensure_ascii=False)
    args = [sys.executable, REEL, "deliver", v, "--brief", BRIEF, "--version", version, "--reviews", rv, "--text", txt]
    r = subprocess.run(args + (["--sample"] if sample else []), capture_output=True, text=True, env=ENV)
    out = json.loads(r.stdout)
    assert r.returncode == 0 and out["ok"], out
    return out["result"]


FINAL_STATE = os.path.join(TMP, "state-final")


@case("7b completion gate")
def final_receipt_must_be_this_turns_and_after_last_render():
    shutil.copytree(STATE, FINAL_STATE)
    global ENV
    old_env, ENV = ENV, {**os.environ, "REEL_STATE_DIR": FINAL_STATE}
    try:
        rec = deliver_real("v1")
    finally:
        ENV = old_env
    out = json.dumps({"ok": True, "result": rec}, ensure_ascii=False)
    build = PY_BUILD[0]
    deliv = ("reel deliver out/good.mp4 --brief brief.json --version v1 --reviews rv.json", out)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
    ok_tp = transcript("final-ok", [build, deliv], CLAIM_DONE)
    assert gate(ok_tp, CLAIM_DONE, session="s-f1", state=FINAL_STATE) is None
    v1 = "v1 已交付，可以发了。"
    assert gate(ok_tp, v1, session="s-f1b", state=FINAL_STATE) is None
    v2 = "v2 已交付，可以发了。"
    assert gate(ok_tp, v2, session="s-f1c", state=FINAL_STATE), "claims v2 but only a v1 receipt exists"
    re_tp = transcript("final-rerender", [deliv, (build[0], build[1], later)], CLAIM_DONE)
    assert gate(re_tp, CLAIM_DONE, session="s-f2", state=FINAL_STATE), "video was produced again after deliver, so the old receipt cannot vouch for it"
    other_tp = transcript("final-other", [build], CLAIM_DONE)
    assert gate(other_tp, CLAIM_DONE, session="s-f3", state=FINAL_STATE), "this turn did not deliver, so a receipt elsewhere cannot vouch for it"
    with open(rec["delivered"], "ab") as fh:
        fh.write(b"x")
    assert gate(ok_tp, CLAIM_DONE, session="s-f4", state=FINAL_STATE), "must block after the delivered file was changed"
    with open(rec["delivered"], "r+b") as fh:          # restore it; later cases still need this receipt
        fh.truncate(os.path.getsize(rec["delivered"]) - 1)
    assert gate(ok_tp, CLAIM_DONE, session="s-f5", state=FINAL_STATE) is None
    return ("produce then deliver passes; claiming v2 with only a v1 receipt is blocked; producing again after deliver is blocked; this turn did not deliver and "
            "the state directory holds another receipt is blocked; the delivered file changed afterwards is blocked")


@case("7b completion gate")
def sample_receipt_requires_saying_sample():
    st = os.path.join(TMP, "state-sample")
    shutil.copytree(STATE, st)
    global ENV
    old_env, ENV = ENV, {**os.environ, "REEL_STATE_DIR": st}
    try:
        rec = deliver_real("v5", sample=True)
    finally:
        ENV = old_env
    tp = transcript("sample", [("reel deliver x.mp4 --brief brief.json --version v5 --sample",
                                json.dumps({"ok": True, "result": rec}, ensure_ascii=False))], CLAIM_DONE)
    why = gate(tp, CLAIM_DONE, session="s-sample", state=st)
    assert why and "待审样片" in why, why
    msg = "v5 已交付到 iCloud，是待审样片，等你试听。"
    assert gate(tp, msg, session="s-sample", state=st) is None
    return "claiming 'the video is done' with only a sample receipt is blocked; saying it is a sample pending review passes"


@case("7b completion gate")
def registered_project_without_production_still_needs_receipt():
    msg = "v3 做好了，放在交付夹了。"
    tp = transcript("proj", [("ls out/", "final.mp4")], msg, prompt="好了没有")
    assert gate(tp, msg, cwd=PROJ, session="s-proj")
    assert gate(tp, msg, cwd=os.path.join(TMP, "elsewhere"), session="s-proj2") is None
    where = "成片在交付夹：片-v1.mp4。"
    tp2 = transcript("where", [("ls", "")], where, prompt="成片在哪里")
    assert gate(tp2, where, cwd=PROJ, session="s-proj3", state=FINAL_STATE) is None
    assert gate(tp2, "v2 做好了，在交付夹。", cwd=PROJ, session="s-proj4", state=FINAL_STATE)
    dev = "视频测试都完成了。检查器改好了，视频相关的单测也做完了。"
    assert gate(transcript("dev", [("python3 tests/test_gate.py", "36/36")], dev), dev, cwd=PROJ, session="s-dev") is None
    return ("'it is done' in a registered project without a receipt is blocked; outside a registered project it is ignored; answering 'where is the video' with a v1 receipt of this project passes, "
            "claiming v2 is blocked; saying only that video tests / the checker are done is not blocked")


@case("7b completion gate")
def no_false_block_on_unrelated_or_scratch_work():
    m1 = "部署完成了，网站已经上线。"
    assert gate(transcript("web", [("npm run build", "ok")], m1), m1, session="s-web") is None
    m2 = "检查器改好了，视频测试都完成了。"
    tp = transcript("scratch", [("cd /private/tmp/x && ffmpeg -y -f lavfi -i testsrc2 -t 2 a.mp4", ""),
                                ("python3 tests/test_gate.py", "36/36 通过")], m2)
    assert gate(tp, m2, session="s-scratch") is None
    m4 = "SKILL.md 的交付闸门一节都改好了。"
    doc = "cd ~/src/reel/skills/reel && python3 - <<'EOF'\nnew = 'reel render plan.json --out \"$P/成片.mp4\"'\nEOF"
    assert gate(transcript("doc", [(doc, "ok")], m4), m4, session="s-doc") is None
    m5 = "依赖检查完成了，ffmpeg 和视频解码都正常。"
    look = [("which python3 ffmpeg ffprobe; grep -n libx264 reelcore/render.py", "/opt/homebrew/bin/ffmpeg\nout.mp4")]
    assert gate(transcript("look", look, m5), m5, session="s-look") is None
    m6 = "提交完成了，reel 的交付闸门代码已入库。"
    cm = [("cd ~/src/reel && git commit -q -m \"gate: receipts\n\nreel deliver re-runs the gate; ffmpeg out.mp4\"", "")]
    assert gate(transcript("commit", cm, m6), m6, session="s-commit") is None
    m3 = "我会先渲染，完成后把视频放进交付夹。"
    assert gate(transcript("future", PY_BUILD, m3), m3, session="s-future") is None
    return "'done' of a non-video task, a test render in a temp directory, reel render appearing in a heredoc that edits docs, and 'after it is done I will...' are none blocked"


@case("7b completion gate")
def english_claims_and_honest_status():
    for msg in ("The video is done: final.mp4 is ready to post.", "v2 is finished and delivered.",
                "The final cut is ready. You can post it now."):
        assert gate(transcript("en-claim", PY_BUILD, msg), msg, session="s-en-" + msg[:6]), msg
    for msg in ("v2 is exported. It is a sample pending review; I have not listened to it yet.",
                "The render finished, but reel check failed (black first frame), so it is not delivered.",
                "All tests pass. No video delivered this turn; I only changed the checker code.",
                "The unit tests are done and the docs are updated."):
        assert gate(transcript("en-honest", PY_BUILD, msg), msg, session="s-en-honest") is None, msg
    return "English: bare 'video is done / v2 delivered / final cut ready' is blocked; 'sample pending review', " \
           "'not delivered', 'no video delivered this turn' and dev-only 'tests are done' are allowed"


@case("7b completion gate")
def blocks_at_most_five_times_per_turn():
    tp = transcript("loop", PY_BUILD, CLAIM_DONE)
    got = [bool(gate(tp, CLAIM_DONE, session="s-loop")) for _ in range(7)]
    assert got == [True] * 5 + [False, False], got
    assert "released after 5 blocks" in open(os.path.join(STATE, "stopgate.log"), encoding="utf-8").read()
    p = subprocess.run([HOOK_PY, GATE], input="garbage", capture_output=True, text=True, env=ENV)
    assert p.returncode == 0 and p.stdout == ""
    return "repeated claims in one turn: blocked 5 times, then passed (to avoid an endless loop from a hook misjudgement) and logged in stopgate.log; bad input passes silently"


if __name__ == "__main__":
    log = sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv else None
    lines = [f"{'✅' if ok else '❌'} [{g}] {n}" + (f" — {m}" if ok and m else ("" if ok else "\n   " + m.replace("\n", "\n   ")))
             for g, n, ok, m in RESULTS]
    fails = [r for r in RESULTS if not r[2]]
    lines.append(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed (hook interpreter {HOOK_PY})")
    print("\n".join(lines))
    if log:
        open(log, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(1 if fails else 0)
