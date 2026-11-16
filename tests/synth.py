"""Synthetic test media: no downloads, no models. Videos have a known structure so tests can assert on it.
make_video(): moving test pattern with optional scene cuts / a flash, and audio built from tones: speaker A (200 Hz) and speaker B (330 Hz) in given
windows, loud beeps at given times. words(): word timings for scripted sentences (what Whisper would return)."""
import subprocess
from pathlib import Path
from clipper.util import ffmpeg


def words(sentences, t=0.0, wd=0.35, gap=0.08, pause=0.5):
    """sentences: list of str or (str, pause_after). -> [{"w","s","e","p"}...]"""
    out = []
    for item in sentences:
        s, pz = (item, pause) if isinstance(item, str) else item
        for w in s.split():
            out.append({"w": w, "s": round(t, 3), "e": round(t + wd, 3), "p": 1.0})
            t += wd + gap
        t += pz
    return out


def make_video(path, dur=60, size="1280x720", fps=25, a_windows=(), b_windows=(), beeps=(), cuts=(), flash=None, audio=True):
    """a_windows/b_windows: [(start,end)] for speaker A/B tones. beeps: [t] loud 1 s 1 kHz beeps. cuts: times where the picture switches pattern.
    flash: (start,end) of a white flash with scene changes at both edges (a visual-only highlight)."""
    path = Path(path)
    w, h = size.split("x")
    pts = sorted({0, *cuts, *( [flash[0], flash[1]] if flash else []), dur})
    parts, labels = [], []
    for i, (a, b) in enumerate(zip(pts, pts[1:])):
        d = round(b - a, 3)
        if flash and a == flash[0]:
            parts.append(f"color=c=white:s={size}:r={fps}:d={d}[v{i}]")
        else:
            pat = ("testsrc2", "smptebars", "testsrc", "mandelbrot")[i % 4] if i % 2 else "testsrc2"
            parts.append(f"{pat}=s={size}:r={fps}:d={d}[v{i}]" if pat != "mandelbrot" else f"testsrc2=s={size}:r={fps}:d={d}[v{i}]")
        labels.append(f"[v{i}]")
    fg_video = ";".join(parts) + ";" + "".join(labels) + f"concat=n={len(labels)}:v=1:a=0[v]"
    cmd = [ffmpeg(), "-y", "-v", "error", "-filter_complex", fg_video]
    if audio:
        tones = [(200, a, b_, 0.25) for a, b_ in a_windows] + [(330, a, b_, 0.25) for a, b_ in b_windows] + [(1000, t, t + 1, 0.9) for t in beeps]
        ins, fl = [], []
        for k, (f, a, b_, amp) in enumerate(tones):  # one sine per window, delayed to its start: unambiguous, no expression quirks
            ins += ["-f", "lavfi", "-i", f"sine=f={f}:d={round(b_ - a, 3)}:r=48000"]
            fl.append(f"[{k}:a]volume={amp},adelay={int(a * 1000)}|{int(a * 1000)}[t{k}]")
        base = len(tones)
        ins += ["-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={dur}"]
        fl.append(f"[{base}:a]aformat=sample_rates=48000:channel_layouts=stereo[sil]")
        fl += [f"[t{k}]aformat=sample_rates=48000:channel_layouts=stereo[u{k}]" for k in range(len(tones))]
        fg = ";".join([fg_video] + fl + ["[sil]" + "".join(f"[u{k}]" for k in range(len(tones))) + f"amix=inputs={len(tones) + 1}:duration=first:normalize=0[a]"])
        cmd = [ffmpeg(), "-y", "-v", "error", *ins, "-filter_complex", fg, "-map", "[v]", "-map", "[a]", "-c:a", "aac"]
    else:
        cmd += ["-map", "[v]"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-pix_fmt", "yuv420p", "-t", str(dur), str(path)]
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(r.stderr[-500:])
    return path
