"""Telegram bot ingestion: share/forward a reel link to the bot and queue it.

Dramatically simpler than the Meta path - a bot from @BotFather needs no app review,
no business verification, no publishing. Telegram delivers each message to our webhook.
"""
import logging
import re

import httpx

from . import config

log = logging.getLogger("reel-relay.telegram")

_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.I)


def message_of(update: dict) -> dict:
    """The message body from any Telegram update shape we care about."""
    return (
        update.get("message")
        or update.get("edited_message")
        or update.get("channel_post")
        or {}
    )


def extract_urls(update: dict) -> list[str]:
    """All URLs in the update's text/caption/entities, deduped, order-preserving.

    When you share an Instagram reel to Telegram, the reel link arrives as the
    message text (or a text_link entity), so this pulls it straight out.
    """
    msg = message_of(update)
    parts: list[str] = []
    if msg.get("text"):
        parts.append(msg["text"])
    if msg.get("caption"):
        parts.append(msg["caption"])
    for key in ("entities", "caption_entities"):
        for ent in msg.get(key) or []:
            if ent.get("url"):
                parts.append(ent["url"])

    urls, seen = [], set()
    for u in _URL_RE.findall(" ".join(parts)):
        u = u.rstrip('.,);]')
        if u not in seen:
            seen.add(u)
            urls.append(u)
    return urls


def send_message(chat_id, text: str) -> None:
    """Best-effort reply back into the Telegram chat (feedback for the user)."""
    if not config.TELEGRAM_BOT_TOKEN or chat_id is None:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
    except Exception as e:  # noqa: BLE001 - replies are best-effort
        log.warning("telegram sendMessage failed: %s", e)
