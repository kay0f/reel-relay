"""Unit + offline integration tests for frame extraction and dedup."""
import json
import subprocess
from pathlib import Path

import pytest

from conftest import requires_ffmpeg
from server import frames


# --- evenly_sample (pure) ---------------------------------------------------

def test_evenly_sample_passthrough_when_short():
    items = [1, 2, 3]
    assert frames.evenly_sample(items, 10) == [1, 2, 3]
    assert frames.evenly_sample(items, 3) == [1, 2, 3]


def test_evenly_sample_downsamples_keeping_ends():
    items = list(range(100))
    picked = frames.evenly_sample(items, 10)
    assert len(picked) == 10
    assert picked[0] == 0
    assert picked[-1] == 99
    assert picked == sorted(set(picked)), "must be strictly increasing"


@pytest.mark.parametrize("n,target", [(50, 2), (7, 5), (40, 36), (2, 2)])
def test_evenly_sample_exact_target(n, target):
    assert len(frames.evenly_sample(list(range(n)), target)) == target


def test_evenly_sample_degenerate_targets():
    assert frames.evenly_sample([1, 2, 3], 0) == []
    assert frames.evenly_sample([1, 2, 3], 1) == [1]
    assert frames.evenly_sample([], 5) == []


# --- mean_abs_diff (pure) ---------------------------------------------------

def test_mean_abs_diff_identical_is_zero():
    a = bytes(range(256))
    assert frames.mean_abs_diff(a, a) == 0.0


def test_mean_abs_diff_uniform_offset():
    a = bytes([10] * 256)
    b = bytes([12] * 256)
    assert frames.mean_abs_diff(a, b) == pytest.approx(2.0)


def test_mean_abs_diff_at_threshold_boundary():
    # exactly 2.0 must count as a duplicate under `<= 2.0`
    a = bytes([0] * 256)
    b = bytes([2] * 256)
    assert frames.mean_abs_diff(a, b) <= 2.0
    c = bytes([3] * 256)
    assert frames.mean_abs_diff(a, c) > 2.0


def test_mean_abs_diff_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        frames.mean_abs_diff(b"\x00" * 10, b"\x00" * 11)
    with pytest.raises(ValueError):
        frames.mean_abs_diff(b"", b"")


# --- ffmpeg-backed (offline, synthesized clip) ------------------------------

@requires_ffmpeg
def test_keyframe_times(synth_clip):
    ts = frames.keyframe_times(synth_clip)
    assert len(ts) >= 8, f"expected forced keyframes every 0.5s, got {ts}"
    assert ts[0] <= 0.1
    assert ts == sorted(ts)


@requires_ffmpeg
def test_ffprobe_duration(synth_clip):
    assert frames.ffprobe_duration(synth_clip) == pytest.approx(6.0, abs=0.3)


@requires_ffmpeg
def test_gray_thumb_is_256_bytes(synth_clip, tmp_path):
    jpg = tmp_path / "f.jpg"
    assert frames.extract_frame(synth_clip, 0.0, jpg)
    assert len(frames.gray_thumb(jpg)) == 256


@requires_ffmpeg
def test_extract_frames_dedups_static_segments(synth_clip, tmp_path):
    out_dir = tmp_path / "frames"
    manifest = frames.extract_frames(synth_clip, out_dir)

    # 12 keyframe candidates, but only 3 distinct color segments should survive
    assert 3 <= len(manifest) <= 6, f"dedup failed: {manifest}"
    ts = [e["t"] for e in manifest]
    assert ts == sorted(ts)
    assert ts[0] <= 0.6, "first (red) frame missing"
    assert any(1.8 <= t <= 2.8 for t in ts), "green segment frame missing"
    assert any(3.8 <= t <= 4.8 for t in ts), "blue segment frame missing"

    for entry in manifest:
        assert (out_dir / entry["file"]).stat().st_size > 0
    saved = json.loads((out_dir / "manifest.json").read_text())
    assert saved == manifest
    assert frames.load_manifest(out_dir) == manifest

    # frames are scaled to the configured width
    probe = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width", "-of", "json",
            str(out_dir / manifest[0]["file"]),
        ],
        capture_output=True, check=True,
    )
    width = json.loads(probe.stdout)["streams"][0]["width"]
    assert width == 512


@requires_ffmpeg
def test_extract_frames_respects_target(synth_clip, tmp_path):
    manifest = frames.extract_frames(synth_clip, tmp_path / "frames", target=2, dedup_threshold=0.0)
    assert 1 <= len(manifest) <= 2


def test_extract_frames_dedup_decision_at_threshold(tmp_path, monkeypatch):
    """The <= threshold decision inside extract_frames itself (no ffmpeg needed).

    A frame whose thumb differs by exactly 2.0 from the last KEPT frame must be
    dropped; > 2.0 must be kept; comparisons are against the last kept, not the
    previous candidate.
    """
    thumbs = iter(
        [
            bytes([0] * 256),  # t=0: kept (first)
            bytes([2] * 256),  # t=1: diff 2.0 vs [0]  -> dropped (boundary)
            bytes([0] * 256),  # t=2: diff 0.0 vs [0]  -> dropped
            bytes([3] * 256),  # t=3: diff 3.0 vs [0]  -> kept
            bytes([5] * 256),  # t=4: diff 2.0 vs [3]  -> dropped (boundary vs last kept)
            bytes([6] * 256),  # t=5: diff 3.0 vs [3]  -> kept
            bytes([6] * 256),  # t=6: diff 0.0 vs [6]  -> dropped
            bytes([6] * 256),  # t=7: dropped
            bytes([6] * 256),  # t=8: dropped
        ]
    )

    def fake_extract(video, t, out_jpg, width=512):
        out_jpg.write_bytes(b"fake-jpeg")
        return True

    monkeypatch.setattr(frames, "ffprobe_duration", lambda v: 10.0)
    monkeypatch.setattr(frames, "keyframe_times", lambda v: [float(i) for i in range(9)])
    monkeypatch.setattr(frames, "extract_frame", fake_extract)
    monkeypatch.setattr(frames, "gray_thumb", lambda p: next(thumbs))

    out_dir = tmp_path / "frames"
    manifest = frames.extract_frames(Path("fake.mp4"), out_dir)

    assert [e["t"] for e in manifest] == [0.0, 3.0, 5.0]
    kept_files = {e["file"] for e in manifest}
    on_disk = {p.name for p in out_dir.glob("*.jpg")}
    assert on_disk == kept_files, "dropped frames must be deleted from disk"


@requires_ffmpeg
def test_extract_frames_pads_sparse_keyframes(tmp_path):
    """A video with almost no keyframes must still get uniform frame coverage."""
    clip = tmp_path / "sparse.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc=size=320x240:rate=12:duration=12",
            "-c:v", "libx264", "-preset", "ultrafast",
            "-g", "999", "-sc_threshold", "0",
            "-pix_fmt", "yuv420p", str(clip),
        ],
        check=True, capture_output=True,
    )
    from server import config

    assert len(frames.keyframe_times(clip)) < config.FRAMES_MIN_KEYFRAMES, (
        "precondition: clip must be keyframe-sparse to exercise the padding branch"
    )
    manifest = frames.extract_frames(clip, tmp_path / "frames")
    ts = [e["t"] for e in manifest]
    assert len(ts) >= 5, f"expected uniform padding to produce coverage, got {ts}"
    assert max(ts) > 6.0, f"coverage must reach the back half of the video, got {ts}"


def test_load_manifest_resilience(tmp_path):
    assert frames.load_manifest(tmp_path / "does-not-exist") == []

    d = tmp_path / "frames"
    d.mkdir()
    (d / "manifest.json").write_text("{corrupt json!")
    assert frames.load_manifest(d) == []

    entries = [{"file": "a.jpg", "t": 1.0}, {"file": "gone.jpg", "t": 2.0}]
    (d / "manifest.json").write_text(json.dumps(entries))
    (d / "a.jpg").write_bytes(b"x")
    assert frames.load_manifest(d) == [{"file": "a.jpg", "t": 1.0}], (
        "entries whose image file is missing must be filtered out"
    )
