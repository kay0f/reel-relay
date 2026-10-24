"""Cookies for authenticated downloads (yt-dlp / gallery-dl) from a cookies.txt file.

The exported file (YTDLP_COOKIES_FILE) may be mounted read-only. yt-dlp writes the
cookie jar back on exit (Instagram rotates some cookies), so downloads use a private
working copy in DATA_DIR. The copy is refreshed whenever the source file changes,
i.e. when the user re-exports cookies. A lock serialises downloads that use it so two
workers never write the jar at the same time.
"""
import logging
import os
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

log = logging.getLogger("reel-relay.cookies")

_lock = threading.Lock()


def _working_copy() -> Path | None:
    if not config.YTDLP_COOKIES_FILE:
        return None
    src = Path(config.YTDLP_COOKIES_FILE)
    if not src.is_file():
        log.warning("YTDLP_COOKIES_FILE=%s does not exist; downloading without cookies", src)
        return None

    cdir = config.DATA_DIR / "cookies"
    cdir.mkdir(parents=True, exist_ok=True)
    os.chmod(cdir, 0o700)
    dst = cdir / "cookies.txt"
    marker = cdir / "source.mtime"

    src_mtime = str(src.stat().st_mtime_ns)
    recorded = marker.read_text().strip() if marker.exists() else ""
    if not dst.exists() or recorded != src_mtime:
        shutil.copyfile(src, dst)
        os.chmod(dst, 0o600)
        marker.write_text(src_mtime)
        log.info("cookies: working copy refreshed from %s", src.name)
    return dst


@contextmanager
def cookie_file() -> Iterator[Path | None]:
    """Yield the working cookies.txt path (or None) while holding the cookie lock."""
    if not config.YTDLP_COOKIES_FILE:
        yield None
        return
    with _lock:
        yield _working_copy()
