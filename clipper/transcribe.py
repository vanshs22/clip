"""Local transcription: faster-whisper, word timestamps, VAD, overlapping chunks, each chunk cached."""
import shutil
from pathlib import Path
from .util import ffmpeg, run, jload, jsave

CHUNK, OVERLAP = 1200, 20  # 20-minute chunks, 20 s overlap


def _model(profile):
    from faster_whisper import WhisperModel
    gpu = bool(shutil.which("nvidia-smi"))
    name = {"light": "small", "balanced": "large-v3-turbo" if gpu else "small", "quality": "large-v3-turbo"}.get(profile, profile)
    return WhisperModel(name, device="cuda" if gpu else "cpu", compute_type="float16" if gpu else "int8")


def transcribe(src, duration, cache_dir, profile="balanced", language=None):
    """Return [{"w","s","e","p"}...]. A crash resumes at the first uncached chunk."""
    cache_dir, model, words = Path(cache_dir), None, []
    starts = list(range(0, int(duration), CHUNK))
    for k, a in enumerate(starts):
        f = cache_dir / f"chunk_{k:04d}.json"
        part = jload(f)
        if part is None:
            model = model or _model(profile)
            wav = cache_dir / "tmp.wav"
            cache_dir.mkdir(parents=True, exist_ok=True)
            run([ffmpeg(), "-y", "-hide_banner", "-ss", str(a), "-t", str(CHUNK + OVERLAP), "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav])
            segs, _ = model.transcribe(str(wav), word_timestamps=True, vad_filter=True, language=language)
            part = [{"w": w.word.strip(), "s": round(a + w.start, 3), "e": round(a + w.end, 3), "p": round(w.probability, 3)}
                    for s in segs for w in (s.words or []) if w.word.strip()]
            wav.unlink(missing_ok=True)
            jsave(f, part)
        lo = a + (OVERLAP / 2 if k else 0)
        hi = a + CHUNK + (OVERLAP / 2 if k + 1 < len(starts) else OVERLAP)
        words += [w for w in part if lo <= w["s"] < hi]  # overlap is only a safety margin; each instant is owned by one chunk
    return words


def from_file(path):
    """Use an existing transcript: {"words":[...]} or faster-whisper/whisper style segments with words."""
    j = jload(path)
    if isinstance(j, dict) and "words" in j:
        return j["words"]
    ws = [w for s in (j.get("segments", []) if isinstance(j, dict) else j) for w in s.get("words", [])]
    return [{"w": (w.get("word") or w.get("w")).strip(), "s": w.get("start", w.get("s")), "e": w.get("end", w.get("e")), "p": w.get("probability", 1.0)} for w in ws]
