import json, os, subprocess
from .errors import ReelError

IMG_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
VID_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
AUD_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def _exif_orientation(path):
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            return im.getexif().get(0x0112, 1)
    except Exception:
        return None


def probe(path):
    """Return the media display size (rotation applied), duration and kind."""
    if not os.path.isfile(path):
        raise ReelError("ASSET_NOT_FOUND", f"media does not exist: {path}", path=path)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".heic":
        raise ReelError("ASSET_UNSUPPORTED", "HEIC must be converted to PNG first (reel-prep.sh or sips)", path=path)
    p = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("ASSET_UNREADABLE", "ffprobe failed", path=path, stderr=p.stderr[-500:])
    info = json.loads(p.stdout)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(info.get("format", {}).get("duration") or 0) or None
    if ext in AUD_EXT or (v is None and a is not None):
        return {"path": path, "kind": "audio", "duration": dur}
    if v is None:
        raise ReelError("ASSET_UNREADABLE", "no video/image stream", path=path)
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(round(float(sd["rotation"]))) % 360
    if int(v.get("tags", {}).get("rotate", 0) or 0):
        rot = int(v["tags"]["rotate"]) % 360
    if rot in (90, 270):
        w, h = h, w
    kind = "image" if ext in IMG_EXT else "video"
    if kind == "image":
        o = _exif_orientation(path)
        if o not in (None, 1):
            raise ReelError("ASSET_EXIF_ROTATED", "the image has EXIF rotation; straighten it with sips/PIL first, otherwise coordinates will be off",
                            path=path, orientation=o)
        dur = None
    return {"path": path, "kind": kind, "width": w, "height": h, "duration": dur,
            "rotation": rot, "has_audio": a is not None}


def probe_full(path):
    """probe plus video scene cuts (used by the inspect tool; slower, the render path calls scene_cuts separately only when needed)."""
    m = probe(path)
    if m["kind"] == "video":
        m["scene_cuts"] = scene_cuts(path)
    return m


def media_duration(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    try:
        return float(p.stdout.strip())
    except ValueError:
        raise ReelError("ASSET_UNREADABLE", "cannot read duration", path=path, stderr=p.stderr[-300:])


_CUTS = {}


def scene_cuts(path, threshold=0.3):
    """Scene-cut times of a video (seconds). A single-frame observation must not be used across a cut; the decision layer splits shots by it."""
    key = (os.path.realpath(path), os.path.getmtime(path), threshold)
    if key in _CUTS:
        return _CUTS[key]
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-an", "-vf",
                        f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise ReelError("FFMPEG_FAILED", "scene-cut detection failed", path=path, stderr=p.stderr[-500:])
    import re
    cuts = [round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", p.stderr)]
    _CUTS[key] = cuts
    return cuts


def frame_ssim(path, t_a, t_b):
    """Whole-frame SSIM between two time points of the same video (320 px wide, grayscale). A static camera is usually above 0.95; noticeably lower when the source itself has camera movement."""
    import re, tempfile
    with tempfile.TemporaryDirectory() as td:
        pngs = []
        for i, t in enumerate((t_a, t_b)):
            png = os.path.join(td, f"{i}.png")
            p = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t}", "-i", path, "-frames:v", "1", "-vf",
                                "scale=320:-2,format=gray", png], capture_output=True, text=True)
            if p.returncode != 0 or not os.path.isfile(png):
                raise ReelError("FFMPEG_FAILED", "frame extraction failed", path=path, t=t, stderr=p.stderr[-300:])
            pngs.append(png)
        p = subprocess.run(["ffmpeg", "-hide_banner", "-i", pngs[0], "-i", pngs[1], "-filter_complex",
                            "[0:v][1:v]ssim", "-f", "null", "-"], capture_output=True, text=True)
        m = re.search(r"All:([0-9.]+)", p.stderr)
        if not m:
            raise ReelError("FFMPEG_FAILED", "SSIM calculation failed", stderr=p.stderr[-300:])
        return float(m.group(1))
