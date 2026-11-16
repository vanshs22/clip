"""Optional AI upscaling of one clip's source segment with the Real-ESRGAN ncnn-vulkan CLI.
Only used for low-resolution sources and never for drafts. UNVERIFIED against the real binary (plumbing is tested with a stand-in)."""
import os, shutil
from pathlib import Path
from .util import digest, ffmpeg, run

TOOL = os.environ.get("CLIPPER_UPSCALER", "realesrgan-ncnn-vulkan")
MODEL = os.environ.get("CLIPPER_UPSCALE_MODEL", "realesrgan-x4plus")


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
        r2 = run([TOOL, "-i", fin, "-o", fout, "-n", MODEL, "-s", scale, "-f", "png"])
        if r2.returncode:
            raise RuntimeError("upscaler failed: " + r2.stderr[-300:])
        r3 = run([ffmpeg(), "-y", "-v", "error", "-framerate", fps, "-i", fout / "%06d.png", "-ss", f"{s:.3f}", "-t", f"{e - s:.3f}", "-i", os.path.abspath(src),
                  "-map", "0:v", "-map", "1:a?", "-c:v", "libx264", "-crf", "14", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out])
        if r3.returncode:
            raise RuntimeError(r3.stderr[-300:])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out
