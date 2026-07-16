"""Keyframe extraction + near-duplicate removal using ffmpeg/ffprobe."""
import json
import logging
import math
import subprocess
from pathlib import Path

from . import config

log = logging.getLogger("reel-relay.frames")

THUMB_SIZE = 16  # dedup thumbnails are 16x16 grayscale


class FfmpegError(RuntimeError):
    pass


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace")[-800:]
        raise FfmpegError(f"{cmd[0]} failed (exit {proc.returncode}): {tail}")
    return proc


def ffprobe_duration(video: Path) -> float:
    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json", str(video),
        ]
    )
    data = json.loads(proc.stdout.decode())
    try:
        return float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as e:
        raise FfmpegError(f"could not read duration of {video.name}: {e}") from e


def keyframe_times(video: Path) -> list[float]:
    """Timestamps (s) of video keyframes, via ffprobe decoding keyframes only."""
    proc = _run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-skip_frame", "nokey",
            "-show_entries", "frame=pts_time,best_effort_timestamp_time",
            "-of", "json", str(video),
        ]
    )
    data = json.loads(proc.stdout.decode())
    times: list[float] = []
    for frame in data.get("frames", []):
        raw = frame.get("pts_time") or frame.get("best_effort_timestamp_time")
        try:
            times.append(float(raw))
        except (TypeError, ValueError):
            continue
    return sorted(set(times))


def evenly_sample(items: list, target: int) -> list:
    """Pick <=target items spread evenly across the list, preserving order.

    Always keeps the first item; keeps the last when target >= 2.
    """
    if target <= 0:
        return []
    if len(items) <= target:
        return list(items)
    if target == 1:
        return [items[0]]
    step = (len(items) - 1) / (target - 1)
    indices = sorted({round(i * step) for i in range(target)})
    return [items[i] for i in indices]


def mean_abs_diff(a: bytes, b: bytes) -> float:
    """Mean absolute pixel difference between two equal-length grayscale buffers."""
    if len(a) != len(b) or not a:
        raise ValueError(f"buffers must be equal-length and non-empty ({len(a)} vs {len(b)})")
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def gray_thumb(image: Path) -> bytes:
    """16x16 grayscale raw pixels of an image, for cheap near-duplicate detection."""
    proc = _run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(image),
            "-vf", f"scale={THUMB_SIZE}:{THUMB_SIZE},format=gray",
            "-frames:v", "1",
            "-f", "rawvideo", "-",
        ]
    )
    raw = proc.stdout
    expected = THUMB_SIZE * THUMB_SIZE
    if len(raw) != expected:
        raise FfmpegError(f"expected {expected} gray bytes for {image.name}, got {len(raw)}")
    return raw


def extract_frame(video: Path, t: float, out_jpg: Path, width: int = config.FRAME_WIDTH) -> bool:
    """Extract one frame at timestamp t (s) as a JPEG scaled to `width` px wide."""
    # Floor to ms: ffmpeg outputs the first frame with pts >= the seek target, so a
    # target rounded UP past the keyframe's exact pts (e.g. 0.4667 -> "0.467") would
    # skip the intended keyframe (and at EOF produce nothing).
    t_seek = math.floor(max(t, 0.0) * 1000) / 1000
    _run(
        [
            "ffmpeg", "-v", "error",
            "-ss", f"{t_seek:.3f}",
            "-i", str(video),
            "-frames:v", "1",
            "-vf", f"scale={width}:-2",
            "-q:v", "3",
            "-y", str(out_jpg),
        ]
    )
    return out_jpg.exists() and out_jpg.stat().st_size > 0


def extract_frames(
    video: Path,
    out_dir: Path,
    target: int = config.FRAMES_TARGET,
    dedup_threshold: float = config.DEDUP_THRESHOLD,
) -> list[dict]:
    """Extract deduplicated keyframes; returns manifest [{"file", "t"}] sorted by t.

    Keyframes only (like `-skip_frame nokey`); if the video has very few keyframes,
    uniformly sampled timestamps are added so short/static reels still get coverage.
    Near-duplicates (mean abs diff of 16x16 gray thumbs <= threshold vs the last
    kept frame) are dropped. Writes manifest.json into out_dir.
    """
    duration = ffprobe_duration(video)
    times = keyframe_times(video)
    if len(times) < config.FRAMES_MIN_KEYFRAMES and duration > 0:
        n = max(4, min(target, int(duration * 0.6) + 1))
        step = duration / (n + 1)
        uniform = {round(step * (i + 1), 3) for i in range(n)}
        times = sorted(set(times) | uniform)
    if not times:
        times = [0.0]
    times = evenly_sample(times, target)

    out_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    last_kept: bytes | None = None
    for i, t in enumerate(times):
        name = f"{i:04d}_{int(round(t * 1000)):07d}ms.jpg"
        path = out_dir / name
        try:
            if not extract_frame(video, t, path):
                continue
            thumb = gray_thumb(path)
        except FfmpegError as e:
            log.warning("frame at %.2fs skipped: %s", t, e)
            path.unlink(missing_ok=True)
            continue
        if last_kept is not None and mean_abs_diff(thumb, last_kept) <= dedup_threshold:
            path.unlink(missing_ok=True)
            continue
        last_kept = thumb
        manifest.append({"file": name, "t": round(float(t), 3)})

    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    log.info("extracted %d frames (of %d candidates) for %s", len(manifest), len(times), video.name)
    return manifest


def load_manifest(frames_dir: Path) -> list[dict]:
    manifest_path = frames_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        entries = json.loads(manifest_path.read_text())
    except json.JSONDecodeError:
        return []
    return [e for e in entries if (frames_dir / e.get("file", "")).exists()]
