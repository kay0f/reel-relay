"""Instagram posts (/p/...): carousels of images and/or videos, fetched with gallery-dl.

yt-dlp only handles video, so image posts and carousels go through gallery-dl. Every
item becomes one "slide": images are resized to FRAME_WIDTH JPEGs, videos contribute
their middle frame plus a transcript. Slides are stored with t = slide number
(1, 2, 3 ...) and the reel is marked media_type='carousel', so tools label frames
"slide N" instead of seconds.
"""
import json
import logging
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from . import config
from .cookies import cookie_file
from .frames import FfmpegError, _run, extract_frame, ffprobe_duration

log = logging.getLogger("reel-relay.carousel")

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}


def is_instagram_post(url: str) -> bool:
    """True for instagram.com/p/<code> (posts/carousels); reels (/reel/) go to yt-dlp."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    return host.endswith("instagram.com") and "p" in parts[:2]


def download_post(url: str, workdir: Path) -> list[tuple[Path, dict]]:
    """Download all items of a post with gallery-dl; return [(file, metadata)] in order."""
    out = workdir / "post"
    out.mkdir(parents=True, exist_ok=True)
    with cookie_file() as cookies:
        cmd = [sys.executable, "-m", "gallery_dl", "-D", str(out), "--write-metadata", "--no-mtime"]
        if cookies is not None:
            cmd += ["--cookies", str(cookies)]
        cmd.append(url)
        proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout).decode(errors="replace")[-600:]
        raise RuntimeError(f"gallery-dl failed (exit {proc.returncode}): {tail}")

    items: list[tuple[Path, dict]] = []
    for f in out.iterdir():
        if f.suffix.lower() not in IMAGE_EXT | VIDEO_EXT:
            continue
        meta: dict = {}
        for candidate in (f.with_name(f.name + ".json"), f.with_suffix(".json")):
            if candidate.exists():
                try:
                    meta = json.loads(candidate.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {}
                break
        items.append((f, meta))
    if not items:
        raise RuntimeError("gallery-dl finished but downloaded no media (private post or login required?)")

    def order(item: tuple[Path, dict]) -> tuple:
        num = item[1].get("num")
        return (num if isinstance(num, int) else 10**6, item[0].name)

    return sorted(items, key=order)[: config.CAROUSEL_MAX_ITEMS]


def post_info(items: list[tuple[Path, dict]]) -> dict:
    """Caption, author and title from the first item's gallery-dl metadata."""
    meta = items[0][1] if items else {}
    description = str(meta.get("description") or "").strip()
    uploader = str(meta.get("username") or meta.get("fullname") or "").strip()
    first_line = description.splitlines()[0].strip() if description else ""
    title = (first_line[:80] + "...") if len(first_line) > 80 else first_line
    return {"description": description, "uploader": uploader, "title": title}


def image_to_frame(src: Path, out_jpg: Path, width: int = config.FRAME_WIDTH) -> None:
    _run(
        [
            "ffmpeg", "-v", "error", "-i", str(src),
            "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2",
            "-q:v", "3", "-y", str(out_jpg),
        ]
    )


def build_slides(items: list[tuple[Path, dict]], frames_dir: Path) -> tuple[list[dict], list[tuple[int, Path]]]:
    """Write one JPEG per slide; return (manifest, [(slide_no, video_path)])."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    videos: list[tuple[int, Path]] = []
    for n, (path, _meta) in enumerate(items, start=1):
        name = f"{n:04d}_slide{n:02d}.jpg"
        out = frames_dir / name
        try:
            if path.suffix.lower() in VIDEO_EXT:
                videos.append((n, path))
                duration = ffprobe_duration(path)
                ok = extract_frame(path, duration / 2 if duration > 0 else 0.0, out)
            else:
                image_to_frame(path, out)
                ok = out.exists() and out.stat().st_size > 0
        except FfmpegError as e:
            log.warning("slide %d skipped: %s", n, e)
            ok = False
        if ok:
            manifest.append({"file": name, "t": float(n)})
    (frames_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return manifest, videos
