import os, shutil, sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("CLIPPER_TEST", "1")
# Unit/integration tests must not download or run real models (minutes of CPU, network): sound events + OCR are exercised in validation/, not here.
import clipper.signals as _S
_S.audio_events = lambda path, duration, **kw: []
_S.ocr_series = lambda path, duration, **kw: []


def _ffmpeg_ok():
    try:
        from clipper.util import ffmpeg
        return bool(ffmpeg())
    except SystemExit:
        return False


def pytest_configure(config):
    for m in ("live: needs MediaMTX + ffmpeg and takes minutes", "slow: takes more than ~30 s", "gpu: needs a GPU (never run in CI)"):
        config.addinivalue_line("markers", m)


def pytest_collection_modifyitems(config, items):
    have_ff = _ffmpeg_ok()
    for it in items:
        if "ff" in it.fixturenames and not have_ff:
            it.add_marker(pytest.mark.skip(reason="ffmpeg not available"))


@pytest.fixture(scope="session")
def ff():
    from clipper.util import ffmpeg
    return ffmpeg()


@pytest.fixture(scope="session")
def media(tmp_path_factory, ff):
    """One shared synthetic 100 s video: two 'speakers', a delayed answer, a visual-only highlight (flash + scene cuts + loud beep at 70 s)."""
    from tests import synth
    d = tmp_path_factory.mktemp("media")
    v = synth.make_video(d / "t.mp4", dur=100, a_windows=[(2, 20), (46, 56)], b_windows=[(22, 40)], beeps=[70], cuts=[30, 68], flash=(70, 72))
    return d, v


@pytest.fixture()
def cwd_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path
