"""SQLite persistence for reels."""
import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS reels (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    title TEXT,
    uploader TEXT,
    duration_s REAL,
    created_at TEXT NOT NULL,
    transcript_json TEXT,
    transcript_source TEXT,
    frames_dir TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT,
    description TEXT,
    media_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_reels_created_at ON reels(created_at DESC);
"""


def _connect() -> sqlite3.Connection:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# Columns added after the first release; older databases get them via ALTER TABLE.
_MIGRATIONS = {
    "description": "ALTER TABLE reels ADD COLUMN description TEXT",
    "media_type": "ALTER TABLE reels ADD COLUMN media_type TEXT",
}


def init_db() -> None:
    with closing(_connect()) as conn, conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(reels)")}
        for column, ddl in _MIGRATIONS.items():
            if column not in existing:
                conn.execute(ddl)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.pop("transcript_json", None)
    try:
        d["transcript"] = json.loads(raw) if raw else []
    except json.JSONDecodeError:
        d["transcript"] = []
    return d


def create_reel(url: str) -> str:
    reel_id = uuid.uuid4().hex[:12]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO reels (id, url, created_at, status) VALUES (?, ?, ?, 'pending')",
            (reel_id, url, now),
        )
    return reel_id


def mark_ready(
    reel_id: str,
    *,
    title: str,
    uploader: str,
    duration_s: float,
    transcript: list[dict],
    transcript_source: str,
    frames_dir: str,
    warning: str | None = None,
    description: str = "",
    media_type: str = "video",
) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """UPDATE reels SET title=?, uploader=?, duration_s=?, transcript_json=?,
               transcript_source=?, frames_dir=?, status='ready', error=?,
               description=?, media_type=? WHERE id=?""",
            (
                title,
                uploader,
                duration_s,
                json.dumps(transcript, ensure_ascii=False),
                transcript_source,
                frames_dir,
                warning,
                description,
                media_type,
                reel_id,
            ),
        )


def fail_stale_pending() -> int:
    """Mark orphaned 'pending' rows failed (pipeline runs in-process, so any row
    still pending at startup was killed mid-flight and will never finish)."""
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE reels SET status='failed', "
            "error='processing was interrupted by a server restart; share the reel again' "
            "WHERE status='pending'"
        )
    return cur.rowcount


def mark_failed(reel_id: str, error: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE reels SET status='failed', error=? WHERE id=?",
            (error[:2000], reel_id),
        )


def get_reel(reel_id: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT * FROM reels WHERE id=?", (reel_id,)).fetchone()
    return _row_to_dict(row) if row else None


def latest_reel() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM reels ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def latest_ready_reel() -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM reels WHERE status='ready' ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_reels(limit: int = 10) -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT * FROM reels ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def find_by_url(url: str, statuses: tuple[str, ...] = ("ready", "pending")) -> dict | None:
    """Newest reel with exactly this URL in one of the given statuses (dedup for add_reel)."""
    marks = ",".join("?" for _ in statuses)
    with closing(_connect()) as conn:
        row = conn.execute(
            f"SELECT * FROM reels WHERE url=? AND status IN ({marks}) "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (url, *statuses),
        ).fetchone()
    return _row_to_dict(row) if row else None
