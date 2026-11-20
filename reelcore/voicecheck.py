"""声音试听的 agent 签法：STT 代听。

reel voice check 样本 --script 试听文本 [--voice-id X] [--brief brief.json]
- 本机 whisper-cli 转写样本，和试听文本比错误率（去标点、空白、大小写）。
  中文按无声调拼音比，不按汉字比：whisper 常把同音字写错（实测 edge-tts 晓晓念「她戴着白头纱去嫁」，
  转写成「他带着白头杀去架」，汉字错 17%，拼音错 0%），那是转写的错，不是配音念错。
- 错误率 ≤ MAX_ERR 才算过；带 --brief 时把 voice.approved 写进 brief（by=claude，stt 证据随附）。
- 闸门（gate._voice_errors）对 agent 签的批准：样本 sha256 对得上、stt.sample_sha256 一致、
  按 stt.transcript / stt.script 重算错误率 ≤ MAX_ERR（不信记录里自报的数）。
只证明「念的音对、听得清」，不证明音色合适：音色按 brief 选，用户听了不满意就换声音出下一版。
whisper 有时会按上下文把念错的多音字「纠正」回原文，这种错它查不出来。
"""
import json, os, re, shutil, subprocess, tempfile
from . import check
from .errors import ReelError

MAX_ERR = 0.10
MIN_SCRIPT_CHARS = 20
ZH_PROMPT = "以下是普通话的句子，使用简体中文。"


def norm(s):
    return re.sub(r"[^一-鿿A-Za-z0-9]", "", s or "").lower()


def units(s, lang="zh"):
    """比对单位：中文每个汉字转成无声调拼音（pypinyin 按词取多音字读音），其余按字符。"""
    t = norm(s)
    if lang != "zh":
        return list(t)
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        raise ReelError("BACKEND_UNAVAILABLE", "中文试听核对要 pypinyin（pip install pypinyin）")
    return [u.lower() for u in lazy_pinyin(t, errors=lambda x: list(x))]


def err_rate(ref, hyp, lang="zh"):
    """错误率 = 编辑距离 / 参考文本单位数（中文按拼音音节，其余按字符）。"""
    return _lev(units(ref, lang), units(hyp, lang))


def cer(ref, hyp):
    """汉字错误率，只作参考显示。"""
    return _lev(list(norm(ref)), list(norm(hyp)))


def _lev(a, b):
    if not a:
        return 1.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return round(prev[-1] / len(a), 4)


def approval_errors(ap):
    """agent 签的 voice.approved 要带 STT 证据；返回错误列表。"""
    stt = ap.get("stt")
    if not isinstance(stt, dict):
        return ["agent 签的声音批准要带 stt 证据（reel voice check 样本 --script 试听文本 --brief brief.json）"]
    errs = []
    if not isinstance(stt.get("engine"), str) or not stt["engine"].strip():
        errs.append("voice.approved.stt.engine 缺失")
    script, tr = stt.get("script"), stt.get("transcript")
    if not isinstance(script, str) or len(norm(script)) < MIN_SCRIPT_CHARS:
        errs.append(f"voice.approved.stt.script 太短（试听文本至少 {MIN_SCRIPT_CHARS} 个字）")
    elif not isinstance(tr, str):
        errs.append("voice.approved.stt.transcript 缺失")
    else:
        lang = stt.get("lang") or "zh"
        try:
            e = err_rate(script, tr, lang)
        except ReelError as x:
            return errs + [x.message]
        if e > MAX_ERR:
            errs.append(f"STT 转写和试听文本对不上：错误率 {e:.0%} > {MAX_ERR:.0%}（{'拼音' if lang == 'zh' else '字符'}）")
    if stt.get("sample_sha256") != ap.get("sample_sha256"):
        errs.append("voice.approved.stt.sample_sha256 和样本对不上（转写的不是这份样本）")
    return errs


def transcribe(sample, lang="zh"):
    model = os.environ.get("REEL_WHISPER_MODEL", os.path.expanduser("~/models/whisper/ggml-large-v3-turbo.bin"))
    if not shutil.which("whisper-cli") or not os.path.isfile(model):
        raise ReelError("BACKEND_UNAVAILABLE", "需要 whisper-cli 和模型（REEL_WHISPER_MODEL）", model=model)
    td = tempfile.mkdtemp(prefix="reel_voice_")
    try:
        wav = os.path.join(td, "a.wav")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", sample, "-ar", "16000", "-ac", "1", wav],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ReelError("FFMPEG_FAILED", "试听样本解码失败", stderr=r.stderr[-400:])
        cmd = ["whisper-cli", "-m", model, "-f", wav, "-l", lang, "-nt", "-otxt", "-of", os.path.join(td, "t")]
        if lang == "zh":
            cmd += ["--prompt", ZH_PROMPT]
        r = subprocess.run(cmd, capture_output=True, text=True)
        txt = os.path.join(td, "t.txt")
        if r.returncode != 0 or not os.path.isfile(txt):
            raise ReelError("WHISPER_FAILED", "whisper 转写失败", stderr=r.stderr[-600:])
        with open(txt, encoding="utf-8") as fh:
            return " ".join(fh.read().split()), "whisper-cli " + os.path.basename(model)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run(sample, script_path, voice_id=None, brief=None, lang="zh"):
    from .errors import GateFailed
    if not os.path.isfile(sample):
        raise ReelError("ASSET_UNREADABLE", "试听样本不存在", path=sample)
    with open(script_path, encoding="utf-8") as fh:
        script = fh.read().strip()
    if len(norm(script)) < MIN_SCRIPT_CHARS:
        raise ReelError("ARG_INVALID", f"试听文本至少 {MIN_SCRIPT_CHARS} 个字（10–20 秒）", chars=len(norm(script)))
    b = None
    if brief:
        with open(brief, encoding="utf-8") as fh:
            b = json.load(fh)
        v = b.get("voice") if isinstance(b, dict) else None
        if not isinstance(v, dict) or v.get("mode") not in ("tts", "clone"):
            raise ReelError("BRIEF_INVALID", "brief.voice.mode 要是 tts 或 clone 才需要试听", brief=brief)
        if voice_id and voice_id != v.get("voice_id"):
            raise ReelError("ARG_INVALID", "--voice-id 和 brief.voice.voice_id 不一致",
                            voice_id=voice_id, brief_voice_id=v.get("voice_id"))
        voice_id = v.get("voice_id")
    if not voice_id:
        raise ReelError("ARG_INVALID", "要给 --voice-id 或 --brief（从 brief.voice.voice_id 取）")
    transcript, engine = transcribe(sample, lang)
    digest = check.sha256(sample)
    e = err_rate(script, transcript, lang)
    res = {"voice_id": voice_id, "err": e, "unit": "pinyin" if lang == "zh" else "char", "max_err": MAX_ERR,
           "cer": cer(script, transcript), "pass": e <= MAX_ERR, "transcript": transcript, "script": script}
    if not res["pass"]:
        raise GateFailed("VOICE_CHECK_FAILED", f"STT 转写和试听文本对不上：错误率 {e:.0%} > {MAX_ERR:.0%}（{res['unit']}）", res)
    proj = os.path.realpath(os.path.dirname(os.path.abspath(brief))) if brief else None
    sp = os.path.realpath(sample)
    rel = os.path.relpath(sp, proj) if proj and sp.startswith(proj + os.sep) else sp
    res["approved"] = {"voice_id": voice_id, "by": "claude", "sample": rel, "sample_sha256": digest,
                       "stt": {"engine": engine, "lang": lang, "script": script, "transcript": transcript,
                               "err": e, "unit": res["unit"], "sample_sha256": digest}}
    if brief:
        b["voice"]["approved"] = res["approved"]
        with open(brief, "w", encoding="utf-8") as fh:
            json.dump(b, fh, ensure_ascii=False, indent=1)
        res["written"] = brief
    return res
