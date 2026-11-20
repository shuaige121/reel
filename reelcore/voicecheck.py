"""Agent sign-off for voice auditions: STT listens on the agent's behalf.

reel voice check SAMPLE --script AUDITION_TEXT [--voice-id X] [--brief brief.json]
- The local whisper-cli transcribes the sample and the error rate against the audition text is computed (punctuation, whitespace and case ignored).
  Chinese is compared as toneless pinyin, not as characters: whisper often writes homophones wrongly (measured: edge-tts read "她戴着白头纱去嫁",
  it was transcribed as "他带着白头杀去架", 17% character error but 0% pinyin error), which is a transcription error, not a voice error.
- Only an error rate of at most MAX_ERR passes; with --brief, voice.approved is written into the brief (by=claude, STT evidence attached).
- The gate (gate._voice_errors) checks an agent-signed approval: the sample sha256 matches, stt.sample_sha256 agrees,
  and the error rate recomputed from stt.transcript / stt.script is at most MAX_ERR (the self-reported number is not trusted).
It only proves "the pronunciation is right and clearly audible", not that the timbre suits: the timbre is chosen per the brief, and if the user dislikes it, switch voice and make the next version.
whisper sometimes "corrects" a mispronounced polyphonic character back to the original from context; this check cannot catch that.
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
    """Comparison unit: each Chinese character becomes toneless pinyin (pypinyin picks polyphonic readings by word); everything else is compared per character."""
    t = norm(s)
    if lang != "zh":
        return list(t)
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        raise ReelError("BACKEND_UNAVAILABLE", "Chinese audition checking needs pypinyin (pip install pypinyin)")
    return [u.lower() for u in lazy_pinyin(t, errors=lambda x: list(x))]


def err_rate(ref, hyp, lang="zh"):
    """Error rate = edit distance / number of reference units (pinyin syllables for Chinese, characters otherwise)."""
    return _lev(units(ref, lang), units(hyp, lang))


def cer(ref, hyp):
    """Character error rate, shown for reference only."""
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
    """An agent-signed voice.approved must carry STT evidence; returns a list of errors."""
    stt = ap.get("stt")
    if not isinstance(stt, dict):
        return ["an agent-signed voice approval must carry stt evidence（reel voice check SAMPLE --script AUDITION_TEXT --brief brief.json）"]
    errs = []
    if not isinstance(stt.get("engine"), str) or not stt["engine"].strip():
        errs.append("voice.approved.stt.engine is missing")
    script, tr = stt.get("script"), stt.get("transcript")
    if not isinstance(script, str) or len(norm(script)) < MIN_SCRIPT_CHARS:
        errs.append(f"voice.approved.stt.script is too short (the audition text needs at least {MIN_SCRIPT_CHARS} characters)")
    elif not isinstance(tr, str):
        errs.append("voice.approved.stt.transcript is missing")
    else:
        lang = stt.get("lang") or "zh"
        try:
            e = err_rate(script, tr, lang)
        except ReelError as x:
            return errs + [x.message]
        if e > MAX_ERR:
            errs.append(f"STT transcript does not match the audition text: error rate {e:.0%} > {MAX_ERR:.0%}({'pinyin' if lang == 'zh' else 'characters'})")
    if stt.get("sample_sha256") != ap.get("sample_sha256"):
        errs.append("voice.approved.stt.sample_sha256 does not match the sample (the transcript is not of this sample)")
    return errs


def transcribe(sample, lang="en"):
    model = os.environ.get("REEL_WHISPER_MODEL", os.path.expanduser("~/models/whisper/ggml-large-v3-turbo.bin"))
    if not shutil.which("whisper-cli") or not os.path.isfile(model):
        raise ReelError("BACKEND_UNAVAILABLE", "whisper-cli and a model are required (REEL_WHISPER_MODEL)", model=model)
    td = tempfile.mkdtemp(prefix="reel_voice_")
    try:
        wav = os.path.join(td, "a.wav")
        r = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", sample, "-ar", "16000", "-ac", "1", wav],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise ReelError("FFMPEG_FAILED", "audition sample failed to decode", stderr=r.stderr[-400:])
        cmd = ["whisper-cli", "-m", model, "-f", wav, "-l", lang, "-nt", "-otxt", "-of", os.path.join(td, "t")]
        if lang == "zh":
            cmd += ["--prompt", ZH_PROMPT]
        r = subprocess.run(cmd, capture_output=True, text=True)
        txt = os.path.join(td, "t.txt")
        if r.returncode != 0 or not os.path.isfile(txt):
            raise ReelError("WHISPER_FAILED", "whisper transcription failed", stderr=r.stderr[-600:])
        with open(txt, encoding="utf-8") as fh:
            return " ".join(fh.read().split()), "whisper-cli " + os.path.basename(model)
    finally:
        shutil.rmtree(td, ignore_errors=True)


def run(sample, script_path, voice_id=None, brief=None, lang="en"):
    from .errors import GateFailed
    if not os.path.isfile(sample):
        raise ReelError("ASSET_UNREADABLE", "audition sample does not exist", path=sample)
    with open(script_path, encoding="utf-8") as fh:
        script = fh.read().strip()
    if len(norm(script)) < MIN_SCRIPT_CHARS:
        raise ReelError("ARG_INVALID", f"the audition text needs at least {MIN_SCRIPT_CHARS} characters (10-20 seconds)", chars=len(norm(script)))
    b = None
    if brief:
        with open(brief, encoding="utf-8") as fh:
            b = json.load(fh)
        v = b.get("voice") if isinstance(b, dict) else None
        if not isinstance(v, dict) or v.get("mode") not in ("tts", "clone"):
            raise ReelError("BRIEF_INVALID", "brief.voice.mode must be tts or clone for an audition to be needed", brief=brief)
        if voice_id and voice_id != v.get("voice_id"):
            raise ReelError("ARG_INVALID", "--voice-id differs from brief.voice.voice_id",
                            voice_id=voice_id, brief_voice_id=v.get("voice_id"))
        voice_id = v.get("voice_id")
    if not voice_id:
        raise ReelError("ARG_INVALID", "give --voice-id or --brief (voice_id is taken from brief.voice.voice_id)")
    transcript, engine = transcribe(sample, lang)
    digest = check.sha256(sample)
    e = err_rate(script, transcript, lang)
    res = {"voice_id": voice_id, "err": e, "unit": "pinyin" if lang == "zh" else "char", "max_err": MAX_ERR,
           "cer": cer(script, transcript), "pass": e <= MAX_ERR, "transcript": transcript, "script": script}
    if not res["pass"]:
        raise GateFailed("VOICE_CHECK_FAILED", f"STT transcript does not match the audition text: error rate {e:.0%} > {MAX_ERR:.0%}（{res['unit']}）", res)
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
