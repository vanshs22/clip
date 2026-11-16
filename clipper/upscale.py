"""OPT-IN AI upscaling (`--upscale ai`, never automatic) of one clip's source segment with the Real-ESRGAN ncnn-vulkan CLI.
Only for low-resolution sources, never for drafts. WARNING from real-footage tests: the default video model is trained on ANIME and redraws real faces as cartoons
(skin flattened, eyes/mouth re-inked); the photographic model (realesrgan-x4plus) produced scrambled tiles under a software Vulkan driver and is untested on a real GPU.
Set CLIPPER_UPSCALE_MODEL and LOOK at a frame before delivering. Default `auto` = lanczos + sharpen, with a low_res_source warning. Verified with the real binary (CPU via lavapipe: ~4 s per 480x270 frame; a GPU is the practical way to use it)."""
import os, shutil
from pathlib import Path
from .util import digest, ffmpeg, run

TOOL = os.environ.get("CLIPPER_UPSCALER", "realesrgan-ncnn-vulkan")
MODEL = os.environ.get("CLIPPER_UPSCALE_MODEL", "realesr-animevideov3")  # video model: verified correct on real footage; x4plus produced scrambled tiles under a software Vulkan driver (untested on a real GPU)


def available():
    return shutil.which(TOOL) is not None


def upscale_clip(src, s, e, work, fps, scale=2):
    """-> path of a 2x video that covers [s, e] of src starting at t=0 (so render uses -ss 0). Cached by content spec."""
    work = Path(work)
    out = work / f"up_{digest(str(src), s, e, scale, MODEL)}.mp4"
    if out.exists():
        return out
    tmp = work / f"frames_{out.stem}"
    fin, fout = tmp / "in", tmp / "out"
    fin.mkdir(parents=True, exist_ok=True)
    fout.mkdir(parents=True, exist_ok=True)
    try:
        r = run([ffmpeg(), "-y", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", os.path.abspath(src), "-an", "-fps_mode", "passthrough", fin / "%06d.png"])
        if r.returncode:
            raise RuntimeError("frame extract failed: " + r.stderr[-300:])
        models = Path(shutil.which(TOOL) or TOOL).resolve().parent / "models"
        r2 = run([TOOL, "-i", fin, "-o", fout, "-n", MODEL, "-s", scale, "-f", "png", *(["-m", models] if models.is_dir() else [])])
        if r2.returncode:
            raise RuntimeError("upscaler failed: " + r2.stderr[-300:])
        r3 = run([ffmpeg(), "-y", "-v", "error", "-framerate", fps, "-i", fout / "%06d.png", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", os.path.abspath(src),
                  "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out])
        if r3.returncode:
            raise RuntimeError(r3.stderr[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


RIFE = os.environ.get("CLIPPER_RIFE", "rife-ncnn-vulkan")
RIFE_MODEL = os.environ.get("CLIPPER_RIFE_MODEL", "rife-v4.6")


def rife_available():
    return shutil.which(RIFE) is not None


def interpolate_clip(src, s, e, work, fps):
    """OPT-IN 2x frame interpolation (`edit --interpolate`) with the RIFE ncnn CLI. -> mp4 of [s, e] of src at 2*fps, starting at t=0.
    Verified on real footage (clean in-between frames on a talking head; fast motion and scene cuts can still smear: look at a frame)."""
    work = Path(work)
    out = work / f"rife_{digest(str(src), s, e, RIFE_MODEL)}.mp4"
    if out.exists():
        return out
    tmp = work / f"rframes_{out.stem}"
    fin, fout = tmp / "in", tmp / "out"
    fin.mkdir(parents=True, exist_ok=True)
    fout.mkdir(parents=True, exist_ok=True)
    try:
        r = run([ffmpeg(), "-y", "-v", "error", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", os.path.abspath(src), "-an", "-fps_mode", "passthrough", fin / "%08d.png"])
        if r.returncode:
            raise RuntimeError("frame extract failed: " + r.stderr[-300:])
        n = len(list(fin.glob("*.png")))
        models = Path(shutil.which(RIFE) or RIFE).resolve().parent / RIFE_MODEL
        r2 = run([RIFE, "-i", fin, "-o", fout, "-n", n * 2, "-f", "%08d.png", *(["-m", models] if models.is_dir() else ["-m", RIFE_MODEL])])
        if r2.returncode or len(list(fout.glob("*.png"))) < n * 2 - 1:
            raise RuntimeError("rife failed: " + r2.stderr[-300:])
        r3 = run([ffmpeg(), "-y", "-v", "error", "-framerate", fps * 2, "-i", fout / "%08d.png", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", os.path.abspath(src),
                  "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out])
        if r3.returncode:
            raise RuntimeError(r3.stderr[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
