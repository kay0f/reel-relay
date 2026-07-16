"""Download + processing pipeline: yt-dlp -> frames -> transcript -> SQLite."""
import logging
import shutil
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import config, store
from .frames import extract_frames, ffprobe_duration
from .transcribe import extract_audio, parse_vtt, transcribe_with_groq

log = logging.getLogger("reel-relay.ingest")

# best mp4 <= 720p, falling back gracefully for extractors without mp4 variants
FORMAT = (
    "bv*[ext=mp4][height<=720]+ba[ext=m4a]/"
    "b[ext=mp4][height<=720]/b[height<=720]/b"
)


def download_video(url: str, workdir: Path) -> tuple[Path, dict]:
    """Download the video (and any English captions) into workdir; return (path, info)."""
    opts = {
        "format": FORMAT,
        "outtmpl": str(workdir / "video.%(ext)s"),
        "merge_output_format": "mp4",
        "writesubtitles": True,
        "writeautomaticsub": True,
        # exact matches only: "en.*" pulls piles of translated variants, each an extra
        # request that can 429 (yt-dlp matches these as regexes)
        "subtitleslangs": ["en", "en-orig"],
        "subtitlesformat": "vtt",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if config.YTDLP_COOKIES_FROM_BROWSER:
        opts["cookiesfrombrowser"] = (config.YTDLP_COOKIES_FROM_BROWSER,)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as e:
        # Captions are best-effort, but yt-dlp fetches them BEFORE the video and a
        # single failed subtitle request (timedtext 429s are common) aborts the whole
        # download. Retry once without captions; whisper covers the transcript.
        log.warning("download failed (%s); retrying without captions", e)
        opts_nosubs = {**opts, "writesubtitles": False, "writeautomaticsub": False}
        with YoutubeDL(opts_nosubs) as ydl:
            info = ydl.extract_info(url, download=True)
    if info is None:
        raise RuntimeError("yt-dlp returned no info for this URL")
    if info.get("_type") == "playlist":
        entries = info.get("entries") or []
        if not entries:
            raise RuntimeError("URL resolved to an empty playlist")
        info = entries[0]

    requested = info.get("requested_downloads") or []
    path = Path(requested[0]["filepath"]) if requested and requested[0].get("filepath") else None
    if path is None or not path.exists():
        candidates = [
            p
            for p in workdir.glob("video.*")
            if p.suffix.lower() not in {".vtt", ".srt", ".part", ".json"}
        ]
        if not candidates:
            raise RuntimeError("download finished but no video file found")
        path = max(candidates, key=lambda p: p.stat().st_size)
    return path, info


def find_caption_file(workdir: Path) -> Path | None:
    """Pick the best caption file: plain 'en', then 'en-orig', then anything else."""
    def rank(p: Path) -> tuple:
        name = p.name.lower()
        return (not name.endswith(".en.vtt"), not name.endswith(".en-orig.vtt"), name)

    candidates = sorted(workdir.glob("*.vtt"), key=rank)
    return candidates[0] if candidates else None


def _build_transcript(video: Path, workdir: Path) -> tuple[list[dict], str, str | None]:
    """Returns (segments, source, warning). Captions first, then Groq whisper."""
    vtt = find_caption_file(workdir)
    if vtt is not None:
        try:
            segments = parse_vtt(vtt.read_text(encoding="utf-8", errors="replace"))
            if segments:
                return segments, "captions", None
        except Exception as e:  # noqa: BLE001 - captions are best-effort
            log.warning("caption parse failed (%s), falling back to whisper", e)

    if not config.GROQ_API_KEY:
        return [], "none", "no captions found and GROQ_API_KEY is not set; transcript empty"
    try:
        audio = extract_audio(video, workdir / "audio.mp3")
        segments = transcribe_with_groq(audio, config.GROQ_API_KEY)
        return segments, f"groq/{config.GROQ_WHISPER_MODEL}", None
    except Exception as e:  # noqa: BLE001 - keep frames usable even if audio fails
        log.warning("transcription failed: %s", e)
        return [], "none", f"transcription failed: {e}"


def run_pipeline(reel_id: str, url: str) -> None:
    """Background task: download, extract frames + transcript, persist.

    Status becomes 'ready' (possibly with a warning in `error` when only the
    transcript failed) or 'failed' when download/frames fail.
    """
    log.info("[%s] ingest started: %s", reel_id, url)
    workdir = Path(tempfile.mkdtemp(prefix=f"reel-{reel_id}-"))
    frames_dir = config.FRAMES_ROOT / reel_id
    try:
        video, info = download_video(url, workdir)
        title = (info.get("title") or "").strip() or url
        uploader = (info.get("uploader") or info.get("channel") or info.get("uploader_id") or "").strip()
        try:
            duration = ffprobe_duration(video)
        except Exception:  # noqa: BLE001
            duration = float(info.get("duration") or 0.0)

        if frames_dir.exists():
            shutil.rmtree(frames_dir)
        manifest = extract_frames(video, frames_dir)
        if not manifest:
            raise RuntimeError("no frames could be extracted")

        transcript, source, warning = _build_transcript(video, workdir)
        store.mark_ready(
            reel_id,
            title=title,
            uploader=uploader,
            duration_s=round(duration, 2),
            transcript=transcript,
            transcript_source=source,
            frames_dir=str(frames_dir),
            warning=warning,
        )
        log.info(
            "[%s] ready: %d frames, %d transcript segments (%s)%s",
            reel_id, len(manifest), len(transcript), source,
            f" — warning: {warning}" if warning else "",
        )
    except Exception as e:  # noqa: BLE001 - background task must record its own failure
        log.exception("[%s] ingest failed", reel_id)
        store.mark_failed(reel_id, f"{type(e).__name__}: {e}")
        shutil.rmtree(frames_dir, ignore_errors=True)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
