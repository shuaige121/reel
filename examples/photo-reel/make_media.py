#!/usr/bin/env python3
"""Generate the example's stand-in media locally: five 1600x1200 cards and a 14.4 s synthesized music track.

These are PLACEHOLDERS so the example runs with no downloads and no paid service. Replace the cards with your own
photos (same file names, or edit plan.json) and the track with your own music or voice-over, then run run.sh again.

Usage: make_media.py OUT_DIR     (needs numpy, Pillow and ffmpeg)
Writes OUT_DIR/card-1.png … card-5.png, OUT_DIR/music.wav and OUT_DIR/targets.json (subject boxes for reel observe).
"""
import json, math, os, subprocess, sys, tempfile, wave

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 1600, 1200
SR = 48000
BPM, BEATS = 100, 24                    # 24 beats at 100 BPM = 14.4 s, the length plan.json expects

CARDS = [  # (top colour, bottom colour, subject centre x/y as fraction of the card, caption)
    ((32, 64, 110), (12, 22, 40), (0.70, 0.42), "zoom_to: push in on the subject"),
    ((120, 52, 40), (40, 16, 18), (0.30, 0.55), "move_to: slow pan"),
    ((28, 96, 72), (10, 34, 28), (0.62, 0.62), "zoom_to: second subject"),
    ((96, 80, 30), (36, 28, 10), (0.50, 0.45), "camera: [] holds still"),
    ((60, 40, 104), (20, 12, 40), (0.50, 0.47), "end card"),
]


def font(size):
    try:
        return ImageFont.load_default(size=size)       # Pillow >= 10.1: bundled scalable font, same on every machine
    except TypeError:
        return ImageFont.load_default()


def card(i, top, bottom, centre, caption, out):
    t = np.linspace(0, 1, H)[:, None, None]
    img = (np.array(top)[None, None, :] * (1 - t) + np.array(bottom)[None, None, :] * t).repeat(W, axis=1)
    im = Image.fromarray(img.astype(np.uint8), "RGB")
    d = ImageDraw.Draw(im)
    for x in range(0, W, 80):                          # texture so camera moves are visible
        d.line([(x, 0), (x, H)], fill=(110, 110, 110) if x % 400 == 0 else (70, 70, 70), width=1)
    for y in range(0, H, 80):
        d.line([(0, y), (W, y)], fill=(70, 70, 70), width=1)
    cx, cy = centre[0] * W, centre[1] * H
    r = 150
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(235, 235, 235), outline=(255, 210, 60), width=10)
    d.ellipse([cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45], fill=(255, 210, 60))
    d.text((cx, cy + r + 30), "subject", font=font(40), fill=(255, 255, 255), anchor="mt")
    # Text stays in the centre column: a 9:16 crop of a 4:3 card keeps only the middle ~42% of its width.
    d.text((W / 2, 90), f"PLACEHOLDER {i}/5", font=font(64), fill=(255, 255, 255), anchor="mt")
    d.text((W / 2, 175), "replace with your own photo", font=font(34), fill=(225, 225, 225), anchor="mt")
    if i == 5:
        d.text((W / 2, H - 200), "Made with /reel", font=font(56), fill=(255, 255, 255), anchor="mt")
    else:
        d.text((W / 2, H - 120), caption, font=font(32), fill=(225, 225, 225), anchor="mt")
    im.save(out)
    pad = 2
    return [round((cx - r - pad) / W, 4), round((cy - r - pad) / H, 4), round((cx + r + pad) / W, 4),
            round((cy + r + pad) / H, 4)]


def music(out):
    """Four-chord pad plus a plucked arpeggio on every beat, then loudness-normalised by ffmpeg."""
    beat = 60 / BPM
    n = int(round(BEATS * beat * SR))
    t = np.arange(n) / SR
    chords = [(261.63, 329.63, 392.00), (196.00, 246.94, 293.66), (220.00, 261.63, 329.63), (174.61, 220.00, 261.63)]
    y = np.zeros(n)
    bar = 4 * beat
    for k in range(int(math.ceil(BEATS / 4))):
        a, b = int(k * bar * SR), min(n, int((k + 1) * bar * SR))
        seg = t[a:b] - k * bar
        env = np.minimum(1, seg / 0.08) * np.minimum(1, (bar - seg) / 0.08)
        for f in chords[k % 4]:
            y[a:b] += 0.12 * env * np.sin(2 * math.pi * f * t[a:b])
        for j in range(4):                              # pluck the chord tones, one per beat
            s = int((k * bar + j * beat) * SR)
            e = min(n, s + int(beat * SR))
            f = chords[k % 4][j % 3] * 2
            tt = t[s:e] - (k * bar + j * beat)
            y[s:e] += 0.25 * np.exp(-tt * 5) * np.sin(2 * math.pi * f * tt)
    y[: int(0.02 * SR)] *= np.linspace(0, 1, int(0.02 * SR))
    y /= np.abs(y).max() * 1.25
    pcm = (np.stack([y, y], axis=1) * 32767).astype("<i2")
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "raw.wav")
        with wave.open(raw, "wb") as w:
            w.setnchannels(2); w.setsampwidth(2); w.setframerate(SR); w.writeframes(pcm.tobytes())
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-af", "loudnorm=I=-14:TP=-2:LRA=7,aresample=48000",
                        "-ar", str(SR), "-c:a", "pcm_s16le", "-t", f"{n / SR:.6f}", out], check=True)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    targets = {}
    for i, (top, bottom, centre, caption) in enumerate(CARDS, 1):
        name = f"card-{i}.png"
        targets[name] = card(i, top, bottom, centre, caption, os.path.join(out, name))
    music(os.path.join(out, "music.wav"))
    with open(os.path.join(out, "targets.json"), "w", encoding="utf-8") as fh:
        json.dump(targets, fh, indent=1)
    print(json.dumps({"media": out, "cards": sorted(targets), "music": "music.wav", "placeholders": True}))


if __name__ == "__main__":
    main()
