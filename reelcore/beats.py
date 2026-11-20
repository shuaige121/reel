"""Beat analysis plus cut-to-beat snapping.

analyze()：music to beats, downbeats (first beat of each bar) and BPM.
  - beat_this (2024, CPJKU, outputs beats and downbeats together, high accuracy) is the default;
  - librosa is the fallback (beats only; downbeats are inferred in 4/4 from the strongest-accent phase and marked downbeat_method=inferred).
  Both run in reel's own venv ($REEL_VENV, default ~/.local/share/reel/venv; installed by ./install.sh --beats); the system Python needs nothing installed.
snap_boundaries()：snaps shot cut points to the nearest beat / downbeat (within tolerance) while guaranteeing a minimum shot duration.
"""
import json, os, subprocess
from .errors import ReelError

VENV_PY = os.environ.get("REEL_VENV_PY") or os.path.join(
    os.environ.get("REEL_VENV") or os.path.expanduser("~/.local/share/reel/venv"), "bin", "python")

_WORKER = r'''
import json, sys, numpy as np
path, backend = sys.argv[1], sys.argv[2]
import librosa
y, sr = librosa.load(path, sr=22050, mono=True)
dur = len(y) / sr
out = {"duration": round(dur, 4)}
HOP = 256   # 11.6ms resolution (the default 512 is 23ms)
def lib():
    tempo, bt = librosa.beat.beat_track(y=y, sr=sr, hop_length=HOP, units="time", trim=False)
    beats = np.atleast_1d(bt).astype(float)
    # the onset-strength peak always comes after the hit; align each beat to the nearest "hit start" within +-60ms (backtrack to the energy valley)
    ons = librosa.onset.onset_detect(y=y, sr=sr, hop_length=HOP, units="time", backtrack=True)
    if len(ons):
        idx = np.clip(np.searchsorted(ons, beats), 1, len(ons) - 1)
        near = np.where(np.abs(ons[idx - 1] - beats) < np.abs(ons[idx] - beats), ons[idx - 1], ons[idx])
        beats = np.where(np.abs(near - beats) <= 0.06, near, beats)
    beats = [float(b) for b in beats]
    # downbeat inference: of the 4 phases in 4/4, the phase with the largest summed onset strength at the beats is the downbeat
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    fr = librosa.time_to_frames(beats, sr=sr, hop_length=HOP)
    fr = np.clip(fr, 0, len(env) - 1)
    score = [env[fr[p::4]].sum() if len(fr[p::4]) else 0 for p in range(4)]
    ph = int(np.argmax(score))
    ibi = np.diff(beats)
    return beats, beats[ph::4], float(60 / np.median(ibi)) if len(ibi) else float(np.atleast_1d(tempo)[0])
if backend in ("auto", "beat_this"):
    try:
        import torch
        from beat_this.inference import File2Beats
        f2b = File2Beats(checkpoint_path="final0", device="cpu", dbn=False)
        b, d = f2b(path)
        beats = np.array([float(x) for x in b]); raw_d = np.array([float(x) for x in d])
        ib = np.diff(beats)
        ref = float(np.median(ib)) if len(ib) else 0.0
        glob_t = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
        if glob_t > 0 and 1.6 <= (60 / glob_t) / max(ref, 1e-6) <= 2.4:   # so many extra points that the median beat interval collapses to half a beat
            ref = 60 / glob_t
        crowd = float(np.mean(ib < 0.6 * ref)) if len(ib) else 0.0
        if crowd > 0.10:
            # more than 10% of the beats are squeezed between two beats (common with sparse rhythms of only drum hits / off-beats): the beat_this grid is unreliable, so use onset-corrected librosa instead
            raise RuntimeError(f"beat_this beats are too dense ({crowd:.0%} of intervals are under 0.6 beat)")
        # remove extra points squeezed between normal beats (syncopation mistaken for beats): when the interval is under 0.6 median beat intervals, keep only the one closer to the uniform grid
        if len(beats) > 4:
            med = float(np.median(np.diff(beats)))
            keep = [beats[0]]
            for x in beats[1:]:
                if x - keep[-1] >= 0.6 * med:
                    keep.append(x)
                else:
                    ref = keep[-2] + med if len(keep) > 1 else keep[-1]
                    if abs(x - ref) < abs(keep[-1] - ref):
                        keep[-1] = x
            beats = np.array(keep)
        # downbeat regularisation: the downbeats from the model are often missed or duplicated (in synthetic rhythms both beat 1 and 2 get marked). Vote for the bar phase by "beat index mod 4",
        # break ties with the onset accent, then take one every 4 beats to get a regular bar grid; the raw downbeats are stored separately
        votes = np.zeros(4)
        for x in raw_d:
            k = int(np.argmin(np.abs(beats - x)))
            if abs(beats[k] - x) < 0.07:
                votes[k % 4] += 1
        env0 = librosa.onset.onset_strength(y=y, sr=sr, hop_length=256)
        # the onset-strength peak comes tens of milliseconds after the hit: take the maximum in a ~60ms window after the beat as that beat's accent
        env = np.array([env0[i:i + 6].max() if i < len(env0) else 0 for i in range(len(env0))])
        fr = np.clip(librosa.time_to_frames(beats, sr=sr, hop_length=256), 0, len(env) - 1)
        accent = np.array([env[fr[p::4]].mean() if len(fr[p::4]) else 0 for p in range(4)])
        best = votes.max()
        cands = [p for p in range(4) if votes[p] >= best - 1e-9]
        ph = max(cands, key=lambda p: accent[p])
        # vote per segment (every 16 beats): after missed or extra beats the bar phase drifts, so re-align per segment; with too few votes in a segment, keep the global phase
        near_k = []
        for x in raw_d:
            k = int(np.argmin(np.abs(beats - x)))
            if abs(beats[k] - x) < 0.07:
                near_k.append(k)
        near_k = np.array(near_k, dtype=int)
        downs, last = [], -99
        for s0 in range(0, len(beats), 16):
            ks = near_k[(near_k >= s0 - 4) & (near_k < s0 + 20)]
            v = np.bincount(ks % 4, minlength=4) if len(ks) else np.zeros(4)
            if v.max() >= 2:
                top = [q for q in range(4) if v[q] >= v.max() - 1e-9]
                if len(top) == 1:
                    loc = top[0]
                else:                                   # tie: compare the onset accents of the beats of each phase within this segment
                    seg = fr[s0:s0 + 16]; idx = np.arange(s0, s0 + len(seg))
                    loc = max(top, key=lambda q: env[seg[idx % 4 == q]].mean() if np.any(idx % 4 == q) else 0)
            else:
                loc = ph
            for k in range(s0, min(s0 + 16, len(beats))):
                if k % 4 == loc and k - last >= 3:          # avoid two downbeats too close together where the phase switches
                    downs.append(beats[k]); last = k
        downs = np.array(downs)
        ibi = np.diff(beats)
        bpm = float(60 / np.median(ibi)) if len(ibi) else 0.0
        out.update(backend="beat_this", beats=beats.tolist(), downbeats=downs.tolist(), bpm=round(bpm, 2),
                   downbeat_method="model_vote_regularized", raw_downbeats=raw_d.tolist(),
                   downbeat_votes=votes.tolist(), downbeat_agreement=round(float(best / max(1, votes.sum())), 3))
    except Exception as e:
        if backend == "beat_this":
            print(json.dumps({"error": f"beat_this failed: {e}"})); sys.exit(0)
        beats, downs, bpm = lib()
        out.update(backend="librosa", beats=beats, downbeats=downs, bpm=round(bpm, 2),
                   downbeat_method="inferred", fallback_reason=str(e)[:200])
else:
    beats, downs, bpm = lib()
    out.update(backend="librosa", beats=beats, downbeats=downs, bpm=round(bpm, 2), downbeat_method="inferred")
print(json.dumps(out))
'''


def analyze(audio_path, backend="auto", timeout=600):
    if not os.path.isfile(audio_path):
        raise ReelError("ASSET_NOT_FOUND", f"audio does not exist: {audio_path}")
    if not os.path.exists(VENV_PY):
        raise ReelError("BACKEND_UNAVAILABLE", "beat analysis needs the reel venv with librosa (and optionally beat_this/torch): run ./install.sh --beats",
                        venv=VENV_PY)
    # beat_this needs wav or common formats; first convert everything to 44.1k mono wav to avoid mp3 decoding differences
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        wav = os.path.join(td, "a.wav")
        p = subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", audio_path, "-ac", "1", "-ar", "44100", wav],
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise ReelError("FFMPEG_FAILED", "audio decoding failed", stderr=p.stderr[-400:])
        p = subprocess.run([VENV_PY, "-c", _WORKER, wav, backend], capture_output=True, text=True, timeout=timeout)
    lines = [ln for ln in p.stdout.splitlines() if ln.startswith("{")]
    if p.returncode != 0 or not lines:
        raise ReelError("BEATS_FAILED", "beat analysis failed", stderr=p.stderr[-800:])
    res = json.loads(lines[-1])
    if "error" in res:
        raise ReelError("BEATS_FAILED", res["error"])
    if len(res.get("beats", [])) < 4:
        raise ReelError("BEATS_FAILED", "too few beats detected (the music is too short or has no clear rhythm)", beats=len(res.get("beats", [])))
    res["source"] = audio_path
    return res


def grid(beats_info, mode="beat"):
    """mode: beat (every beat) | downbeat | every:N (every N beats) | half (half beats, a midpoint inserted between beats)."""
    b = beats_info["beats"]
    if mode == "beat":
        return list(b)
    if mode == "downbeat":
        return list(beats_info.get("downbeats") or b[::4])
    if mode == "half":
        out = []
        for x, y in zip(b, b[1:]):
            out += [x, (x + y) / 2]
        return out + b[-1:]
    if mode.startswith("every:"):
        n = int(mode.split(":")[1])
        d = beats_info.get("downbeats") or []
        start = b.index(min(b, key=lambda t: abs(t - d[0]))) if d else 0   # count from the first downbeat
        return b[start::n]
    raise ReelError("ARG_INVALID", f"unknown beat-snap mode {mode!r} (beat / downbeat / every:N / half)")


def snap_boundaries(bounds, targets, tolerance=0.30, min_gap=0.35, offset=0.0, fallback=None, feasible=None):
    """bounds: original cut points (seconds, strictly increasing, excluding the start 0 and the end). targets: beat points (music time); offset: where the music starts on the timeline.
    Each cut snaps to the nearest beat within tolerance that keeps a gap of at least min_gap; if a downbeat does not work it falls back to fallback (ordinary beats).
    feasible(i, t, prev): optional constraint (e.g. the shot duration must fit camera move / transition / source length); beats that fail it are not chosen.
    If none is found the cut stays where it was, with reason=no_beat in the report (no beat within tolerance) or constraint (a beat exists but would make the shot invalid). Returns (new cuts, report)."""
    tg = sorted(t + offset for t in targets)
    fb = sorted(t + offset for t in (fallback or []))
    ok = feasible or (lambda i, t, prev: True)
    out, report, prev = [], [], 0.0
    for i, b in enumerate(bounds):
        allowed, level, blocked = [], None, False
        for lvl, pool in (("primary", tg), ("fallback", fb)):
            near = [t for t in pool if abs(t - b) <= tolerance and t - prev >= min_gap]
            allowed = [t for t in near if ok(i, t, prev)]
            blocked = blocked or bool(near and not allowed)
            if allowed:
                level = lvl
                break
        if allowed:
            t = min(allowed, key=lambda x: abs(x - b))
            report.append({"cut": i, "from": round(b, 4), "to": round(t, 4), "moved": round(t - b, 4), "level": level})
            prev = t
            out.append(t)
        else:
            report.append({"cut": i, "from": round(b, 4), "to": round(b, 4), "moved": 0.0, "unsnapped": True,
                           "reason": "constraint" if blocked else "no_beat"})
            prev = b
            out.append(b)
    return out, report


def _duration_limits(c, observations, trans):
    """Allowed duration range [lo, hi] (seconds) for each shot, consistent with the plan validation/compile rules:
    the t1 of camera moves and manual highlights, twice the transitions on each side, the observation time points referenced by video shots, and the remaining length of the video source."""
    fps = c["plan"]["output"]["fps"]
    obs = {o["id"]: o for o in (observations or []) if isinstance(o, dict) and "id" in o}
    num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    lo, hi = [], []
    for k, s in enumerate(c["plan"]["shots"]):
        need = [2 / fps, 2 * trans[k], 2 * (trans[k - 1] if k else 0)]
        need += [op["t1"] for op in s.get("camera") or [] if num(op.get("t1"))]
        need += [h["t1"] for h in s.get("highlight") or [] if h.get("sync") == "manual" and num(h.get("t1"))]
        top = float("inf")
        m = c["metas"].get(s["id"]) or {}
        if m.get("kind") == "video":
            si = s.get("source_in") or 0
            if num(m.get("duration")):
                top = m["duration"] - si
            for op in s.get("camera") or []:
                ids = [op.get("observation_id")] + list(op.get("track") or [])
                need += [obs[x]["t"] - si for x in ids if x in obs and num(obs[x].get("t"))]
        lo.append(max(need) + 1e-3)
        hi.append(top - 1e-3)
    return lo, hi


def snap_plan(plan, beats_info, mode="downbeat", tolerance=0.30, min_gap=0.35, offset=0.0, observations=None):
    """Adjust the shot durations of a plan to the beats so that hard cuts / transition starts land on beats. Only duration changes, never order or camera moves.
    A cut is defined as the start of the next shot on the timeline (the start of the transition when there is one). observations is used to validate zoom_to references.
    Only beats that do not make a shot invalid are chosen (camera t1 does not fit, transition too long, video source too short); if none is found the cut stays and the report says why."""
    import copy
    from . import render
    c = render.compile_plan(copy.deepcopy(plan), observations)
    fps = c["plan"]["output"]["fps"]
    starts = [s / fps for s in c["starts"]]
    trans = [t / fps for t in c["trans"]]
    end = (c["starts"][-1] + c["frames"][-1]) / fps
    bounds = starts[1:]
    lo, hi = _duration_limits(c, observations, trans)
    nxt = bounds[1:] + [end]                 # the next cut is first estimated at its original position: shot i+1 must also be valid if that cut does not snap

    def feasible(i, t, prev):
        d_i, d_n = t - prev + trans[i], nxt[i] - t + trans[i + 1]
        return lo[i] <= d_i <= hi[i] and lo[i + 1] <= d_n <= hi[i + 1]

    fb = beats_info["beats"] if mode != "beat" else None
    new_b, report = snap_boundaries(bounds, grid(beats_info, mode), tolerance, min_gap, offset, fallback=fb,
                                    feasible=feasible)
    p = copy.deepcopy(plan)
    edges = [0.0] + new_b
    for i, s in enumerate(p["shots"][:-1]):
        # shot i occupies [edges[i], edges[i+1]) on the timeline plus its transition overlap with the next shot
        s["duration"] = round(edges[i + 1] - edges[i] + trans[i], 4)
    if c["plan"]["duration_policy"] == "require_match":
        p["shots"][-1]["duration"] = round(end - edges[-1], 4)   # total length unchanged, otherwise DURATION_MISMATCH
    return p, {"mode": mode, "tolerance": tolerance, "cuts": report,
               "snapped": sum(1 for r in report if not r.get("unsnapped")), "total": len(report)}
