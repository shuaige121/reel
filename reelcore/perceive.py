"""Perception layer: produces Observations (source-image normalised bboxes with origin, time and confidence); the decision layer may only reference them and must not invent coordinates.

Two sources:
  1. llm-visual：frame_grid() extracts a frame and overlays a 0-1 coordinate grid; after looking at the picture the LLM registers a bbox with observe().
  2. detector：OWLv2 open-vocabulary detection (natively supported by transformers), run on a machine with torch; without local torch it goes to the REEL_REMOTE GPU host.
observations are stored in observations.json next to the plan:[{id, asset, t, label, bbox, conf, source}]
"""
import json, os, subprocess, tempfile
from . import manifest
from .errors import ReelError

OWL_MODEL = "google/owlv2-base-patch16-ensemble"


# ---------------- observation storage ----------------

def load_obs(path):
    if not path or not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_obs(path, obs):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obs, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def add_obs(path, items):
    obs = load_obs(path)
    n = max([int(o["id"].split("-")[1]) for o in obs if o["id"].startswith("obs-")] + [0])
    out = []
    for it in items:
        n += 1
        it = {"id": f"obs-{n}", **it}
        obs.append(it)
        out.append(it)
    save_obs(path, obs)
    return out


def _check_bbox(b):
    if not (isinstance(b, (list, tuple)) and len(b) == 4 and 0 <= b[0] < b[2] <= 1 and 0 <= b[1] < b[3] <= 1):
        raise ReelError("BBOX_INVALID", "bbox must be normalised [x0,y0,x1,y1] with 0<=x0<x1<=1", bbox=b)
    return [round(float(x), 5) for x in b]


def observe(obs_path, asset_rel, asset_path, bbox, label, t=0.0, conf=None, source="llm-visual"):
    manifest.probe(asset_path)
    return add_obs(obs_path, [{"asset": asset_rel, "t": t, "label": label, "bbox": _check_bbox(bbox),
                               "conf": conf, "source": source}])[0]


# ---------------- frame extraction + coordinate grid ----------------

def extract_frame(asset_path, t, out_png):
    meta = manifest.probe(asset_path)
    if meta["kind"] == "video" and meta["duration"] and t > meta["duration"]:
        raise ReelError("INVALID_TIME", f"t={t} is beyond the video length {meta['duration']}")
    cmd = ["ffmpeg", "-y", "-v", "error"] + (["-ss", f"{t}"] if meta["kind"] == "video" else []) + \
          ["-i", asset_path, "-frames:v", "1", out_png]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "frame extraction failed", stderr=p.stderr[-500:])
    return meta


def frame_grid(asset_path, t, out_png, max_side=1400):
    """Extract a frame and draw a normalised grid with 0.1 steps and tick marks so the LLM can read coordinates. Returns the image size and the source size."""
    from PIL import Image, ImageDraw, ImageFont
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "f.png")
        meta = extract_frame(asset_path, t, raw)
        im = Image.open(raw).convert("RGB")
    k = min(1.0, max_side / max(im.size))
    if k < 1:
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    pad = 36
    canvas = Image.new("RGB", (im.width + pad, im.height + pad), (20, 20, 20))
    canvas.paste(im, (pad, pad))
    d = ImageDraw.Draw(canvas, "RGBA")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    for i in range(11):
        x = pad + im.width * i / 10
        y = pad + im.height * i / 10
        strong = i % 5 == 0
        col = (255, 255, 0, 200) if strong else (255, 255, 255, 110)
        d.line([(x, pad), (x, pad + im.height)], fill=col, width=2 if strong else 1)
        d.line([(pad, y), (pad + im.width, y)], fill=col, width=2 if strong else 1)
        d.text((x - 10, 8), f"{i / 10:.1f}", fill=(255, 255, 255), font=font)
        d.text((2, y - 8), f"{i / 10:.1f}", fill=(255, 255, 255), font=font)
    canvas.save(out_png)
    return {"png": out_png, "source_size": [meta["width"], meta["height"]], "t": t,
            "note": "read coordinates from the ticks on the picture, normalised to the source image (0-1, origin at top-left)"}


def draw_boxes(asset_path, t, boxes, out_png, max_side=1400):
    """Draw the observation back onto the frame so the LLM can check whether the detection/registration boxed the right thing."""
    from PIL import Image, ImageDraw
    with tempfile.TemporaryDirectory() as td:
        raw = os.path.join(td, "f.png")
        extract_frame(asset_path, t, raw)
        im = Image.open(raw).convert("RGB")
    k = min(1.0, max_side / max(im.size))
    if k < 1:
        im = im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)
    d = ImageDraw.Draw(im)
    for b in boxes:
        x0, y0, x1, y1 = b["bbox"]
        d.rectangle([x0 * im.width, y0 * im.height, x1 * im.width, y1 * im.height], outline=(0, 255, 0), width=4)
        d.text((x0 * im.width + 4, y0 * im.height + 4), f"{b.get('id', '')} {b.get('label', '')}",
               fill=(0, 255, 0))
    im.save(out_png)
    return out_png


# ---------------- OWLv2 detection (needs torch; goes to the remote host when it is missing locally) ----------------

def detect_owlv2(image_paths, queries, threshold=0.15, top_k=3, max_vram_frac=0.8):
    """image_paths: [(key, png)]。Returns {key: [{label, bbox, conf}]}, with bbox in source-image normalised coordinates."""
    try:
        import torch
        from PIL import Image
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
    except ImportError as e:
        raise ReelError("BACKEND_UNAVAILABLE", "no local torch/transformers: install with ./install.sh --vision, or set REEL_REMOTE and use --where remote", missing=str(e))
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    if dev == "cuda":
        torch.cuda.set_per_process_memory_fraction(max_vram_frac)
    proc = Owlv2Processor.from_pretrained(OWL_MODEL)
    model = Owlv2ForObjectDetection.from_pretrained(OWL_MODEL).to(dev).eval()
    out = {}
    for key, png in image_paths:
        im = Image.open(png).convert("RGB")
        inputs = proc(text=[queries], images=im, return_tensors="pt").to(dev)
        with torch.no_grad():
            res = model(**inputs)
        # OWLv2 preprocessing pads the image to a square; pass the padded side as target_sizes, then normalise by the source width and height
        side = max(im.size)
        pp = getattr(proc, "post_process_grounded_object_detection", None) or proc.post_process_object_detection
        det = pp(res, threshold=threshold, target_sizes=torch.tensor([[side, side]], device=dev))[0]
        items = []
        for score, lab, box in zip(det["scores"].tolist(), det["labels"].tolist(), det["boxes"].tolist()):
            x0, y0, x1, y1 = box
            b = [max(0.0, x0 / im.width), max(0.0, y0 / im.height), min(1.0, x1 / im.width),
                 min(1.0, y1 / im.height)]
            if b[0] < b[2] and b[1] < b[3]:
                items.append({"label": queries[lab], "bbox": [round(v, 5) for v in b], "conf": round(score, 4)})
        items.sort(key=lambda x: -x["conf"])
        out[key] = items[:top_k]
    return out
