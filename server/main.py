"""FastAPI app (ingest + debug endpoints) with the FastMCP server mounted under a token path."""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import secrets as pysecrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from pydantic import BaseModel

from . import config, store, telegram
from .frames import evenly_sample, load_manifest
from .ingest import enqueue, run_pipeline, run_pipeline_media, start_workers
from .transcribe import format_transcript

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("reel-relay")

# ---------------------------------------------------------------------------
# MCP server + tools
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="reel-relay",
    instructions=(
        "Instagram reels, posts and carousels the user shared. Each item is stored as a "
        "timestamped transcript plus video frames (or carousel slides) and the author's "
        "caption, so you can 'watch' it. When the user pastes an instagram.com link in "
        "the chat, call add_reel with it - it downloads, processes and returns the item. "
        "When the user says 'the reel I just shared/sent you', call get_reel with no "
        "reel_id - that returns the most recent one. Use list_recent_reels to browse, "
        "and get_reel_frames to zoom into a specific time window. Never describe or "
        "summarise a reel you could not retrieve: if a tool reports a failure, say so."
    ),
)


def _reel_or_message(reel_id: str | None) -> tuple[dict | None, str | None]:
    """Resolve a reel (latest ready one when reel_id is None) or an explanation."""
    if reel_id:
        reel = store.get_reel(reel_id)
        if reel is None:
            return None, f"No reel with id '{reel_id}'. Use list_recent_reels to see valid ids."
        if reel["status"] == "pending":
            return None, (
                f"Reel {reel_id} is still processing (shared {reel['created_at']}). "
                "It usually takes ~15-60s after sharing; try again shortly."
            )
        if reel["status"] == "failed":
            return None, f"Reel {reel_id} failed to process: {reel.get('error') or 'unknown error'}"
        return reel, None

    newest = store.latest_reel()
    if newest is None:
        return None, "No reels have been shared yet."
    if newest["status"] == "ready":
        return newest, None
    ready = store.latest_ready_reel()
    if newest["status"] == "pending":
        msg = (
            f"The most recently shared reel (id {newest['id']}, shared {newest['created_at']}) "
            "is still processing - try again in ~30s."
        )
    else:
        msg = (
            f"The most recently shared reel (id {newest['id']}) failed: "
            f"{newest.get('error') or 'unknown error'}."
        )
    if ready is not None:
        msg += (
            f" The most recent *ready* reel is '{ready.get('title') or ready['url']}' "
            f"(id {ready['id']}) - pass that id explicitly if you want it."
        )
    return None, msg


def _is_carousel(reel: dict) -> bool:
    return reel.get("media_type") == "carousel"


def _reel_header(reel: dict) -> str:
    lines = [
        f"Title: {reel.get('title') or '(untitled)'}",
        f"Uploader: {reel.get('uploader') or '(unknown)'}",
        f"URL: {reel['url']}",
    ]
    if _is_carousel(reel):
        lines.append(
            f"Type: carousel/post with {int(reel.get('duration_s') or 0)} slide(s); "
            "frame labels give the slide number, not seconds"
        )
    else:
        lines.append(f"Duration: {reel.get('duration_s') or '?'}s")
    lines += [
        f"Shared at: {reel['created_at']} (id {reel['id']})",
        f"Transcript source: {reel.get('transcript_source') or 'none'}",
    ]
    if reel.get("error"):
        lines.append(f"Warning: {reel['error']}")
    description = (reel.get("description") or "").strip()
    lines.append("Author's caption:\n" + (description if description else "(none)"))
    return "\n".join(lines)


def _frame_blocks(reel: dict, entries: list[dict]) -> tuple[str, list]:
    """Base64 the given manifest entries; returns (summary line, interleaved blocks).

    Each image is immediately preceded by its own timestamp label - one aggregate
    timestamp list would force the model to count ordinally across many unlabeled
    images, which reliably misattributes times.
    """
    frames_dir = Path(reel["frames_dir"])
    loaded: list[tuple[float, ImageContent]] = []
    for entry in entries:
        path = frames_dir / entry["file"]
        try:
            data = base64.b64encode(path.read_bytes()).decode()
        except OSError:
            continue
        loaded.append((entry["t"], ImageContent(type="image", data=data, mimeType="image/jpeg")))
    if not loaded:
        return "No frames available.", []
    blocks: list = []
    carousel = _is_carousel(reel)
    for n, (t, image) in enumerate(loaded, start=1):
        label = f"slide {t:g}" if carousel else f"t={t:g}s"
        blocks.append(TextContent(type="text", text=f"[frame {n}/{len(loaded)} - {label}]"))
        blocks.append(image)
    if carousel:
        summary = f"{len(loaded)} slides follow in order, each preceded by its slide number."
    else:
        summary = (
            f"{len(loaded)} frames follow in chronological order, each preceded by a "
            f"label with its timestamp in seconds."
        )
    return summary, blocks


@mcp.tool
def list_recent_reels(limit: int = 10) -> list[dict]:
    """List the reels the user recently shared from their phone, newest first.

    Returns id, title, uploader, url, created_at, status, duration_s and the first
    ~200 characters of the transcript. Use get_reel(reel_id) to actually watch one.
    If the user just says "the reel I sent", skip this and call get_reel directly.
    """
    limit = max(1, min(int(limit), 50))
    out = []
    for r in store.list_reels(limit=limit):
        preview = " ".join(seg["text"] for seg in r.get("transcript") or [])
        out.append(
            {
                "id": r["id"],
                "title": r.get("title"),
                "uploader": r.get("uploader"),
                "url": r["url"],
                "created_at": r["created_at"],
                "status": r["status"],
                "duration_s": r.get("duration_s"),
                "transcript_preview": (preview[:200] + "...") if len(preview) > 200 else preview,
                "error": r.get("error"),
            }
        )
    return out


def _get_reel_impl(reel_id: str | None = None, include_frames: bool = True, max_frames: int = 10):
    """Shared implementation of get_reel (also used by add_reel)."""
    reel, message = _reel_or_message(reel_id)
    if reel is None:
        return [TextContent(type="text", text=message)]

    text = _reel_header(reel) + "\n\nTranscript:\n" + format_transcript(reel.get("transcript") or [])
    blocks: list = []
    if include_frames:
        if _is_carousel(reel):
            max_frames = max(int(max_frames), 20)  # slide text IS the content: show all
        max_frames = max(1, min(int(max_frames), 20))
        entries = evenly_sample(load_manifest(Path(reel["frames_dir"])), max_frames)
        summary, frame_blocks = _frame_blocks(reel, entries)
        blocks.append(TextContent(type="text", text=text + "\n\n" + summary))
        blocks.extend(frame_blocks)
    else:
        blocks.append(TextContent(type="text", text=text))
    return blocks


@mcp.tool
def get_reel(reel_id: str | None = None, include_frames: bool = True, max_frames: int = 10):
    """Watch a reel the user shared: full timestamped transcript + frames from the video.

    Call with NO reel_id to get the most recently shared reel - do this when the user
    says "the reel I just shared/sent you". Frames are evenly sampled JPEG images in
    chronological order, each preceded by a timestamp label - treat them as the visual
    storyline of the video. The default 10 frames is an overview; call get_reel_frames
    for a closer look at a specific time window instead of raising max_frames.
    Set include_frames=false for transcript-only.
    """
    return _get_reel_impl(reel_id, include_frames, max_frames)


@mcp.tool
def get_reel_frames(
    reel_id: str | None = None,
    start_s: float = 0.0,
    end_s: float = 1e9,
    max_frames: int = 10,
):
    """Get frames from one time window of a reel (e.g. 10s-25s) to look closer.

    Useful after get_reel when you need more visual detail about a specific moment -
    e.g. a step in a recipe or an exercise's form. reel_id defaults to the most
    recently shared reel. Timestamps are in seconds from the start of the video.
    """
    reel, message = _reel_or_message(reel_id)
    if reel is None:
        return [TextContent(type="text", text=message)]
    if end_s < start_s:
        start_s, end_s = end_s, start_s

    manifest = load_manifest(Path(reel["frames_dir"]))
    window = [e for e in manifest if start_s <= e["t"] <= end_s]
    if not window:
        available = ", ".join(f"{e['t']:g}" for e in manifest) or "none"
        return [
            TextContent(
                type="text",
                text=(
                    f"No frames between {start_s:g}s and {end_s:g}s for reel {reel['id']}. "
                    f"Available frame timestamps (s): {available}"
                ),
            )
        ]
    max_frames = max(1, min(int(max_frames), 20))
    summary, frame_blocks = _frame_blocks(reel, evenly_sample(window, max_frames))
    header = f"Reel '{reel.get('title') or reel['url']}' - window {start_s:g}s-{end_s:g}s. {summary}"
    return [TextContent(type="text", text=header), *frame_blocks]


def normalize_instagram_url(url: str) -> str | None:
    """https://instagram.com/<kind>/<code>/ without tracking params; None if not allowed."""
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or host not in config.ALLOWED_INGEST_HOSTS:
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2 and parts[0] in ("reel", "reels", "p", "tv"):
        kind = "reel" if parts[0] == "reels" else parts[0]
        return f"https://www.instagram.com/{kind}/{parts[1]}/"
    if host.endswith("instagram.com"):
        return None  # profiles, stories, explore... are not supported
    return parsed._replace(fragment="").geturl()


@mcp.tool
async def add_reel(url: str, wait_s: int = 60):
    """Fetch an Instagram reel or post (carousel) from a link the user pasted, then watch it.

    Downloads and processes the item (video -> transcript + frames; carousel -> slides),
    waiting up to wait_s seconds, and returns the same result as get_reel. If processing
    takes longer it returns the reel id - call get_reel with it a bit later. Links that
    were already processed are returned immediately. Only instagram.com links are accepted.
    """
    clean = normalize_instagram_url(url)
    if clean is None:
        return [
            TextContent(
                type="text",
                text=(
                    "Unsupported link. add_reel accepts Instagram reel/post links like "
                    "https://www.instagram.com/reel/<code>/ or https://www.instagram.com/p/<code>/."
                ),
            )
        ]

    existing = store.find_by_url(clean)
    if existing is not None and existing["status"] == "ready":
        return _get_reel_impl(existing["id"])
    reel_id = existing["id"] if existing is not None else store.create_reel(clean)
    if existing is None:
        enqueue(run_pipeline, reel_id, clean)
        log.info("[%s] queued from add_reel: %s", reel_id, clean)

    deadline = max(5, min(int(wait_s), 110))
    for _ in range(deadline // 2):
        await asyncio.sleep(2)
        reel = store.get_reel(reel_id)
        if reel is None:
            break
        if reel["status"] == "ready":
            return _get_reel_impl(reel_id)
        if reel["status"] == "failed":
            return [
                TextContent(
                    type="text",
                    text=(
                        f"Could not process {clean}: {reel.get('error') or 'unknown error'}. "
                        "Do not guess its content."
                    ),
                )
            ]
    return [
        TextContent(
            type="text",
            text=(
                f"Still processing {clean} (id {reel_id}). Call get_reel(reel_id='{reel_id}') "
                "in ~30 seconds."
            ),
        )
    ]


# ---------------------------------------------------------------------------
# FastAPI app: /ingest + debug endpoints, MCP mounted at /mcp-<token>
# ---------------------------------------------------------------------------

mcp_app = mcp.http_app(path="/mcp", stateless_http=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.require_secrets()
    store.init_db()
    config.FRAMES_ROOT.mkdir(parents=True, exist_ok=True)
    start_workers(config.WORKER_CONCURRENCY)
    stale = store.fail_stale_pending()
    if stale:
        log.warning("marked %d reel(s) stuck in 'pending' from a previous run as failed", stale)
    # never log the full token: the MCP path is the connector's only credential
    log.info(
        "MCP endpoint ready at http://%s:%s/mcp-%s.../mcp (run `make mcp-url` for the full URL)",
        config.HOST, config.PORT, config.MCP_PATH_TOKEN[:4],
    )
    async with mcp_app.lifespan(app):
        yield


# no /docs, /redoc or /openapi.json: this app is exposed through a public tunnel
app = FastAPI(title="reel-relay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount(f"/mcp-{config.MCP_PATH_TOKEN}", mcp_app)


class IngestBody(BaseModel):
    url: str


def _check_secret(request: Request) -> None:
    provided = request.headers.get("x-ingest-secret", "")
    # compare bytes: compare_digest raises TypeError on non-ASCII str input (-> 500)
    valid = config.INGEST_SECRET and pysecrets.compare_digest(
        provided.encode("utf-8", errors="replace"), config.INGEST_SECRET.encode("utf-8")
    )
    if not valid:
        raise HTTPException(status_code=401, detail="missing or invalid X-Ingest-Secret header")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest")
def ingest(body: IngestBody, request: Request, background: BackgroundTasks) -> dict:
    _check_secret(request)
    url = body.url.strip()
    if not url.lower().startswith(("http://", "https://")):
        log.warning("rejected ingest (400): request body url was %r", body.url[:200])
        raise HTTPException(status_code=400, detail="url must be an http(s) URL")
    reel_id = store.create_reel(url)
    enqueue(run_pipeline, reel_id, url)
    log.info("[%s] queued: %s", reel_id, url)
    return {"id": reel_id, "status": "pending"}


@app.get("/reels")
def reels_index(request: Request, limit: int = 50) -> list[dict]:
    _check_secret(request)
    return store.list_reels(limit=max(1, min(limit, 200)))


@app.get("/reels/{reel_id}")
def reel_detail(reel_id: str, request: Request) -> dict:
    _check_secret(request)
    reel = store.get_reel(reel_id)
    if reel is None:
        raise HTTPException(status_code=404, detail="no such reel")
    if reel.get("frames_dir"):
        reel["frames"] = [e["t"] for e in load_manifest(Path(reel["frames_dir"]))]
    return reel


# ---------------------------------------------------------------------------
# Instagram DM webhook (Phase 1): share a reel to a bot account -> Meta pushes
# the reel's CDN URL here -> we download + process it. No iOS Shortcut needed.
# ---------------------------------------------------------------------------

_REEL_ATTACHMENT_TYPES = {"ig_reel", "reel", "share", "video"}


def _verify_meta_signature(raw: bytes, header: str) -> bool:
    if not config.META_APP_SECRET or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(config.META_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


def _extract_reel_urls(data: dict) -> list[tuple[str, str]]:
    """Pull (cdn_url, title) for each shared reel in an Instagram messaging payload."""
    out: list[tuple[str, str]] = []
    for entry in data.get("entry") or []:
        for msg in entry.get("messaging") or []:
            message = msg.get("message") or {}
            if message.get("is_echo"):
                continue  # ignore our own outgoing messages echoed back
            for att in message.get("attachments") or []:
                if att.get("type") in _REEL_ATTACHMENT_TYPES:
                    payload = att.get("payload") or {}
                    url = payload.get("url")
                    if url:
                        out.append((url, (payload.get("title") or "").strip()))
    return out


@app.get("/webhook")
def webhook_verify(request: Request):
    """Meta's subscription handshake: echo hub.challenge when the verify token matches."""
    p = request.query_params
    if (
        config.META_VERIFY_TOKEN
        and p.get("hub.mode") == "subscribe"
        and p.get("hub.verify_token") == config.META_VERIFY_TOKEN
    ):
        return PlainTextResponse(p.get("hub.challenge", ""))
    raise HTTPException(status_code=403, detail="verification failed")


@app.post("/webhook")
async def webhook_receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    if not _verify_meta_signature(raw, request.headers.get("x-hub-signature-256", "")):
        raise HTTPException(status_code=403, detail="invalid or missing signature")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json")
    # log the raw payload (truncated) so real Meta deliveries can be inspected during setup
    log.info("webhook received: %s", json.dumps(data)[:1500])
    for media_url, title in _extract_reel_urls(data):
        reel_id = store.create_reel(media_url)
        enqueue(run_pipeline_media, reel_id, media_url, title)
        log.info("[%s] queued from DM webhook (title=%r)", reel_id, title)
    return PlainTextResponse("EVENT_RECEIVED")


# ---------------------------------------------------------------------------
# Telegram bot ingestion: share/forward a reel link to your bot -> it lands here.
# Far simpler than Meta: a @BotFather bot, no app review or verification.
# ---------------------------------------------------------------------------


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    if not config.TELEGRAM_WEBHOOK_SECRET or (
        request.headers.get("x-telegram-bot-api-secret-token") != config.TELEGRAM_WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="invalid secret token")
    update = await request.json()
    msg = telegram.message_of(update)
    chat_id = (msg.get("chat") or {}).get("id")
    sender_id = str((msg.get("from") or {}).get("id", ""))

    # optional owner lock: ignore anyone who isn't you
    if config.TELEGRAM_ALLOWED_USER_ID and sender_id != str(config.TELEGRAM_ALLOWED_USER_ID):
        return {"ok": True}

    urls = telegram.extract_urls(update)
    if not urls:
        text = msg.get("text") or ""
        if chat_id and (text.startswith("/start") or not text):
            background.add_task(
                telegram.send_message, chat_id,
                f"Share or forward an Instagram reel to me and I'll add it to Claude. "
                f"(your Telegram id is {sender_id})",
            )
        return {"ok": True}

    for url in urls:
        reel_id = store.create_reel(url)
        enqueue(run_pipeline, reel_id, url)
        log.info("[%s] queued from Telegram (sender %s): %s", reel_id, sender_id, url)
    background.add_task(
        telegram.send_message, chat_id,
        f"Got it - processing {len(urls)} reel{'s' if len(urls) > 1 else ''}. "
        "Ask Claude about it in ~30s.",
    )
    return {"ok": True}
