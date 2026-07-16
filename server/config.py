"""Environment configuration. All settings come from env vars, optionally via .env at repo root."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
INGEST_SECRET = os.getenv("INGEST_SECRET", "")
MCP_PATH_TOKEN = os.getenv("MCP_PATH_TOKEN", "")

DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH = DATA_DIR / "reels.db"
FRAMES_ROOT = DATA_DIR / "frames"

HOST = os.getenv("HOST", "127.0.0.1")
PORT = int(os.getenv("PORT", "8787"))

# Optional: let yt-dlp reuse a logged-in browser session for reels that need auth,
# e.g. YTDLP_COOKIES_FROM_BROWSER=safari (or chrome, firefox...). Off by default.
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "")

# Frame extraction tuning
FRAME_WIDTH = 512          # output JPEG width in px
FRAMES_TARGET = 36         # ~30-40 frames for a <=60s reel
FRAMES_MIN_KEYFRAMES = 8   # below this, pad with uniformly sampled timestamps
DEDUP_THRESHOLD = 2.0      # mean abs pixel diff on 16x16 grayscale; <= this drops the frame

GROQ_TRANSCRIPTION_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_WHISPER_MODEL = "whisper-large-v3"


def require_secrets() -> None:
    """Fail fast at server startup when security-critical env vars are missing."""
    missing = [
        name
        for name, value in (("INGEST_SECRET", INGEST_SECRET), ("MCP_PATH_TOKEN", MCP_PATH_TOKEN))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}. Run `make secrets` "
            "(or generate values with `python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"` "
            "and put them in .env — see .env.example)."
        )
