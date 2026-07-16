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

# How many reels to process at once. A small default keeps a 1GB VM + Groq's
# free-tier rate limit happy under bursts; extra shares queue and drain in order.
WORKER_CONCURRENCY = int(os.getenv("WORKER_CONCURRENCY", "2"))

# Optional: let yt-dlp reuse a logged-in browser session for reels that need auth,
# e.g. YTDLP_COOKIES_FROM_BROWSER=safari (or chrome, firefox...). Off by default.
YTDLP_COOKIES_FROM_BROWSER = os.getenv("YTDLP_COOKIES_FROM_BROWSER", "")

# Instagram DM webhook (Phase 1) - optional, only needed for the "share a reel to a
# bot Instagram account" ingestion path. When both are set, /webhook is active.
META_APP_SECRET = os.getenv("META_APP_SECRET", "")        # verifies X-Hub-Signature-256
META_VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "")    # for the GET webhook handshake

# Telegram bot ingestion - share/forward a reel to your bot and it lands in Claude.
# Far simpler than Meta: make a bot with @BotFather, no app review needed.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")          # from @BotFather
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")  # verifies webhook calls
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")  # optional: only accept from you

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
            "and put them in .env - see .env.example)."
        )
