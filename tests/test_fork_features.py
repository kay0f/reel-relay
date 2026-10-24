"""Tests for the fork's additions: STT endpoint, cookies file, carousels, add_reel, migration."""
import asyncio
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from conftest import requires_ffmpeg
from server import carousel, config, cookies, store, transcribe


# --- STT (OpenAI-compatible, NanoGPT by default) ----------------------------


class _Resp:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def test_transcribe_audio_verbose_json(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    calls = []

    def fake_post(url, **kw):
        calls.append(kw["data"])
        return _Resp(200, {"segments": [{"start": 0, "end": 1.5, "text": " привет "}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(config, "STT_LANGUAGE", "ru")
    segs = transcribe.transcribe_audio(audio, "key")
    assert segs == [{"start": 0.0, "end": 1.5, "text": "привет"}]
    assert calls[0]["response_format"] == "verbose_json"
    assert calls[0]["language"] == "ru"
    assert calls[0]["model"] == config.STT_MODEL


def test_transcribe_audio_falls_back_to_json(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    formats = []

    def fake_post(url, **kw):
        formats.append(kw["data"]["response_format"])
        if kw["data"]["response_format"] == "verbose_json":
            return _Resp(400, text="unsupported response_format")
        return _Resp(200, {"text": "весь текст"})

    monkeypatch.setattr(httpx, "post", fake_post)
    segs = transcribe.transcribe_audio(audio, "key", duration=12.3)
    assert formats == ["verbose_json", "json"]
    assert segs == [{"start": 0.0, "end": 12.3, "text": "весь текст"}]


def test_transcribe_audio_accepts_nanogpt_native_shape_and_plain_text(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(200, {"transcription": "abc"}))
    assert transcribe.transcribe_audio(audio, "k")[0]["text"] == "abc"
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(200, None, text="plain"))
    assert transcribe.transcribe_audio(audio, "k")[0]["text"] == "plain"


def test_transcribe_audio_error(monkeypatch, tmp_path):
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"x")
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _Resp(401, {"error": "bad key"}))
    with pytest.raises(RuntimeError, match="401"):
        transcribe.transcribe_audio(audio, "k")


@requires_ffmpeg
def test_extract_audio_for_upload_reencodes_when_too_big(synth_clip, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STT_MAX_UPLOAD_BYTES", 30_000)
    audio = transcribe.extract_audio_for_upload(synth_clip, tmp_path)
    assert audio.stat().st_size <= 30_000


# --- cookies file -----------------------------------------------------------


def test_cookie_file_disabled(monkeypatch):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FILE", "")
    with cookies.cookie_file() as path:
        assert path is None


def test_cookie_file_working_copy_refreshes_on_reexport(monkeypatch, tmp_path):
    src = tmp_path / "export" / "cookies.txt"
    src.parent.mkdir()
    src.write_text("# Netscape HTTP Cookie File\nv1\n")
    monkeypatch.setattr(config, "YTDLP_COOKIES_FILE", str(src))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    with cookies.cookie_file() as path:
        assert path is not None and path != src
        assert path.read_text().endswith("v1\n")
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        path.write_text("# Netscape HTTP Cookie File\nrotated-by-yt-dlp\n")  # jar write-back

    with cookies.cookie_file() as path:  # source unchanged -> keep rotated copy
        assert "rotated-by-yt-dlp" in path.read_text()

    time.sleep(0.01)
    src.write_text("# Netscape HTTP Cookie File\nv2\n")  # user re-exported
    os.utime(src, None)
    with cookies.cookie_file() as path:
        assert path.read_text().endswith("v2\n")


def test_cookie_file_missing_source(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FILE", str(tmp_path / "nope.txt"))
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    with cookies.cookie_file() as path:
        assert path is None


# --- carousels --------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.instagram.com/p/DekFBpggKHO/?obrf=x", True),
        ("https://instagram.com/p/abc/", True),
        ("https://www.instagram.com/reel/DepgeJetCaW/", False),
        ("https://www.instagram.com/someuser/", False),
        ("https://example.com/p/abc/", False),
    ],
)
def test_is_instagram_post(url, expected):
    assert carousel.is_instagram_post(url) is expected


def test_post_info_from_metadata():
    items = [(Path("a.jpg"), {"description": "Первая строка\nвторая", "username": "heysash.ai", "num": 1})]
    info = carousel.post_info(items)
    assert info == {"description": "Первая строка\nвторая", "uploader": "heysash.ai", "title": "Первая строка"}


@requires_ffmpeg
def test_build_slides_images_and_video(synth_clip, tmp_path):
    imgs = []
    for i, color in enumerate(("red", "blue"), start=1):
        p = tmp_path / f"img{i}.png"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=1080x1350",
             "-frames:v", "1", "-y", str(p)],
            check=True,
        )
        imgs.append(p)
    items = [(imgs[0], {"num": 1}), (synth_clip, {"num": 2}), (imgs[1], {"num": 3})]
    manifest, videos = carousel.build_slides(items, tmp_path / "frames")
    assert [e["t"] for e in manifest] == [1.0, 2.0, 3.0]
    assert videos == [(2, synth_clip)]
    assert json.loads((tmp_path / "frames" / "manifest.json").read_text()) == manifest


def test_download_post_orders_by_num(monkeypatch, tmp_path):
    def fake_run(cmd, capture_output, timeout):
        out = Path(cmd[cmd.index("-D") + 1])
        for name, num in (("b.jpg", 2), ("a.jpg", 1), ("c.mp4", 3)):
            (out / name).write_bytes(b"x")
            (out / f"{name}.json").write_text(json.dumps({"num": num}))
        assert "--cookies" not in cmd
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr(config, "YTDLP_COOKIES_FILE", "")
    monkeypatch.setattr(subprocess, "run", fake_run)
    items = carousel.download_post("https://www.instagram.com/p/x/", tmp_path)
    assert [p.name for p, _ in items] == ["a.jpg", "b.jpg", "c.mp4"]


def test_download_post_reports_gallery_dl_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "YTDLP_COOKIES_FILE", "")
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, capture_output, timeout: subprocess.CompletedProcess(cmd, 1, b"", b"login required"),
    )
    with pytest.raises(RuntimeError, match="login required"):
        carousel.download_post("https://www.instagram.com/p/x/", tmp_path)


# --- store migration --------------------------------------------------------


def test_init_db_migrates_old_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "reels.db")
    conn = sqlite3.connect(tmp_path / "reels.db")
    conn.execute(
        "CREATE TABLE reels (id TEXT PRIMARY KEY, url TEXT NOT NULL, title TEXT, uploader TEXT, "
        "duration_s REAL, created_at TEXT NOT NULL, transcript_json TEXT, transcript_source TEXT, "
        "frames_dir TEXT, status TEXT NOT NULL DEFAULT 'pending', error TEXT)"
    )
    conn.commit()
    conn.close()
    store.init_db()
    rid = store.create_reel("https://www.instagram.com/p/x/")
    store.mark_ready(
        rid, title="t", uploader="u", duration_s=3, transcript=[], transcript_source="none",
        frames_dir=str(tmp_path), description="caption", media_type="carousel",
    )
    reel = store.get_reel(rid)
    assert reel["description"] == "caption" and reel["media_type"] == "carousel"
    assert store.find_by_url("https://www.instagram.com/p/x/")["id"] == rid


# --- add_reel / URL normalisation -------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.instagram.com/reel/DepgeJetCaW/?obrf=MTM3dnZt", "https://www.instagram.com/reel/DepgeJetCaW/"),
        ("https://instagram.com/p/DekFBpggKHO", "https://www.instagram.com/p/DekFBpggKHO/"),
        ("https://www.instagram.com/reels/ABC/", "https://www.instagram.com/reel/ABC/"),
        ("https://www.instagram.com/someuser/", None),
        ("https://evil.example.com/reel/x/", None),
        ("file:///etc/passwd", None),
    ],
)
def test_normalize_instagram_url(url, expected):
    from server.main import normalize_instagram_url

    assert normalize_instagram_url(url) == expected


def test_add_reel_rejects_foreign_links():
    from server.main import add_reel

    result = asyncio.run(add_reel.fn("https://example.com/video.mp4"))
    assert "Unsupported link" in result[0].text


def test_add_reel_returns_existing_ready_reel(monkeypatch, tmp_path):
    from server import main

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "reels.db")
    store.init_db()
    url = "https://www.instagram.com/reel/ABC/"
    rid = store.create_reel(url)
    store.mark_ready(
        rid, title="Готовый", uploader="u", duration_s=5, transcript=[{"start": 0, "end": 1, "text": "hi"}],
        transcript_source="captions", frames_dir=str(tmp_path / "none"), description="подпись",
    )
    queued = []
    monkeypatch.setattr(main, "enqueue", lambda *a: queued.append(a))
    result = asyncio.run(main.add_reel.fn(url + "?igsh=tracking", wait_s=5))
    assert queued == []
    assert "Title: Готовый" in result[0].text
    assert "подпись" in result[0].text
