"""SQLite store tests against a temp database."""
import pytest

from server import config, store


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "reels.db")
    store.init_db()
    return tmp_path


def test_create_and_get(tmp_db):
    reel_id = store.create_reel("https://example.com/reel/1")
    reel = store.get_reel(reel_id)
    assert reel["status"] == "pending"
    assert reel["url"] == "https://example.com/reel/1"
    assert reel["transcript"] == []
    assert store.get_reel("nope") is None


def test_mark_ready_roundtrip(tmp_db):
    reel_id = store.create_reel("https://example.com/reel/2")
    transcript = [{"start": 0.0, "end": 1.5, "text": "héllo — unicode ok"}]
    store.mark_ready(
        reel_id,
        title="Test reel",
        uploader="someone",
        duration_s=57.9,
        transcript=transcript,
        transcript_source="captions",
        frames_dir="/tmp/frames/x",
        warning=None,
    )
    reel = store.get_reel(reel_id)
    assert reel["status"] == "ready"
    assert reel["title"] == "Test reel"
    assert reel["duration_s"] == 57.9
    assert reel["transcript"] == transcript
    assert reel["error"] is None


def test_mark_ready_with_warning_keeps_ready_status(tmp_db):
    reel_id = store.create_reel("https://example.com/reel/3")
    store.mark_ready(
        reel_id,
        title="t", uploader="", duration_s=1.0,
        transcript=[], transcript_source="none",
        frames_dir="/tmp/frames/y",
        warning="transcription failed: boom",
    )
    reel = store.get_reel(reel_id)
    assert reel["status"] == "ready"
    assert "transcription failed" in reel["error"]


def test_mark_failed(tmp_db):
    reel_id = store.create_reel("https://example.com/reel/4")
    store.mark_failed(reel_id, "DownloadError: 404")
    reel = store.get_reel(reel_id)
    assert reel["status"] == "failed"
    assert "404" in reel["error"]


def test_fail_stale_pending(tmp_db):
    stuck = store.create_reel("https://example.com/stuck")
    done = store.create_reel("https://example.com/done")
    store.mark_ready(
        done, title="d", uploader="", duration_s=1.0, transcript=[],
        transcript_source="none", frames_dir="/tmp/f", warning=None,
    )
    assert store.fail_stale_pending() == 1
    assert store.get_reel(stuck)["status"] == "failed"
    assert "interrupted" in store.get_reel(stuck)["error"]
    assert store.get_reel(done)["status"] == "ready", "non-pending rows must be untouched"


def test_list_and_latest_ordering(tmp_db):
    a = store.create_reel("https://example.com/a")
    b = store.create_reel("https://example.com/b")
    c = store.create_reel("https://example.com/c")
    ids = [r["id"] for r in store.list_reels(limit=10)]
    assert ids == [c, b, a], "newest first (rowid tiebreak within the same second)"
    assert store.latest_reel()["id"] == c

    store.mark_ready(
        b, title="b", uploader="", duration_s=1.0, transcript=[],
        transcript_source="none", frames_dir="/tmp/f", warning=None,
    )
    assert store.latest_ready_reel()["id"] == b, "latest *ready* skips newer pending reels"
    assert store.latest_reel()["id"] == c
    assert len(store.list_reels(limit=2)) == 2
