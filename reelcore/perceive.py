"""感知层：产出 Observation（带来源、时间、置信度的原图归一化 bbox），决策层只能引用它，不能凭空写坐标。

两条来源：
  1. llm-visual：frame_grid() 抽帧并叠加 0–1 坐标网格，LLM 看图后用 observe() 登记 bbox。
  2. detector：OWLv2 开放词汇检测（transformers 原生支持），在有 torch 的机器上跑；本机没有 torch 时走 REEL_REMOTE 远程 GPU 主机。
observations 存在 plan 旁边的 observations.json：[{id, asset, t, label, bbox, conf, source}]
"""
import json, os, subprocess, tempfile
from . import manifest
from .errors import ReelError

OWL_MODEL = "google/owlv2-base-patch16-ensemble"


# ---------------- observation 存储 ----------------

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
        raise ReelError("BBOX_INVALID", "bbox 必须是归一化 [x0,y0,x1,y1]，0≤x0<x1≤1", bbox=b)
    return [round(float(x), 5) for x in b]


def observe(obs_path, asset_rel, asset_path, bbox, label, t=0.0, conf=None, source="llm-visual"):
    manifest.probe(asset_path)
    return add_obs(obs_path, [{"asset": asset_rel, "t": t, "label": label, "bbox": _check_bbox(bbox),
                               "conf": conf, "source": source}])[0]


# ---------------- 抽帧 + 坐标网格 ----------------

def extract_frame(asset_path, t, out_png):
    meta = manifest.probe(asset_path)
    if meta["kind"] == "video" and meta["duration"] and t > meta["duration"]:
        raise ReelError("INVALID_TIME", f"t={t} 超出视频长度 {meta['duration']}")
    cmd = ["ffmpeg", "-y", "-v", "error"] + (["-ss", f"{t}"] if meta["kind"] == "video" else []) + \
          ["-i", asset_path, "-frames:v", "1", out_png]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "抽帧失败", stderr=p.stderr[-500:])
    return meta


def frame_grid(asset_path, t, out_png, max_side=1400):
    """抽帧并画 0.1 步长的归一化网格和刻度，供 LLM 读坐标。返回图的尺寸与原图尺寸。"""
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
            "note": "坐标按图上刻度读取，归一化到原图（0–1，左上为原点）"}


def draw_boxes(asset_path, t, boxes, out_png, max_side=1400):
    """把 observation 画回帧上，供 LLM 核对检测/登记是否框对。"""
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


# ---------------- OWLv2 检测（需要 torch；本机没有时走远程主机） ----------------

def detect_owlv2(image_paths, queries, threshold=0.15, top_k=3, max_vram_frac=0.8):
    """image_paths: [(key, png)]。返回 {key: [{label, bbox, conf}]}，bbox 为原图归一化坐标。"""
    try:
        import torch
        from PIL import Image
        from transformers import Owlv2ForObjectDetection, Owlv2Processor
    except ImportError as e:
        raise ReelError("BACKEND_UNAVAILABLE", "本机没有 torch/transformers：装 ./install.sh --vision，或设 REEL_REMOTE 后用 --where remote", missing=str(e))
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
        # OWLv2 预处理把图 pad 成正方形；target_sizes 传 pad 后边长，再按原图宽高归一化
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
