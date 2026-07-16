"""Shared test fixtures: a tiny ffmpeg-synthesized clip so tests need no network."""
import shutil
import subprocess
from pathlib import Path

import pytest

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not on PATH")


@pytest.fixture(scope="session")
def synth_clip(tmp_path_factory) -> Path:
    """6s 320x240 clip: 2s red, 2s green, 2s blue, 440 Hz tone, keyframe every 0.5s.

    Colors are full-intensity hex: CSS 'green' (#008000) has almost the same grayscale
    luma as red (~75 vs ~76), which the 16x16 gray dedup would rightly collapse.
    """
    out = tmp_path_factory.mktemp("clip") / "clip.mp4"
    cmd = [
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "color=c=0xFF0000:s=320x240:d=2:r=12",
        "-f", "lavfi", "-i", "color=c=0x00FF00:s=320x240:d=2:r=12",
        "-f", "lavfi", "-i", "color=c=0x0000FF:s=320x240:d=2:r=12",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-force_key_frames", "expr:gte(t,n_forced*0.5)",
        "-c:a", "aac", "-shortest",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    return out
