# Telegram bot ingestion (recommended — works on iOS *and* Android)

Share or forward a reel to your Telegram bot and it lands in Claude. This is the
easiest, friendliest ingestion path: a bot from **@BotFather** needs **no app review,
no business verification, no developer account bureaucracy** — unlike the Instagram DM
webhook. And because it's a Telegram bot, it works identically on **iOS, Android, and
desktop**.

```
any reel  --Share-->  Telegram  --to your bot-->  Telegram delivers the message
                                                        │  POST /telegram/webhook
                                                        ▼
                          reel-relay extracts the link -> yt-dlp -> frames + transcript -> Claude
```

## Setup (~2 minutes)

1. **Create the bot.** In Telegram, message **@BotFather** → send `/newbot` → give it a
   name and a username ending in `bot`. BotFather replies with a **token** like
   `8123456789:AAF...`.
2. **Set env vars** in your server's `.env` and restart:
   ```bash
   TELEGRAM_BOT_TOKEN=<the token from BotFather>
   TELEGRAM_WEBHOOK_SECRET=<random>   # python3 -c "import secrets; print(secrets.token_urlsafe(24))"
   ```
3. **Register the webhook** (point Telegram at your public server URL):
   ```bash
   curl "https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook" \
     -d "url=https://<your-host>/telegram/webhook" \
     -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
     -d 'allowed_updates=["message","channel_post","edited_message"]'
   ```
   You should get `{"ok":true,...,"description":"Webhook was set"}`. Confirm with
   `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"` (look for your url and
   `last_error_message: null`).

## Use it

- Open a reel → **Share** → **Telegram** → send it to your bot. (Or forward/paste any
  reel link straight into the bot chat.)
- A few seconds later, ask Claude: *"what's in the reel I just shared?"*
- The bot replies in-chat ("Got it — processing…") so you get instant confirmation.

## Lock it to just you (recommended)

Anyone who finds your bot could send it links. To accept reels only from your own
Telegram account, set `TELEGRAM_ALLOWED_USER_ID` in `.env` and restart. To find your id:
send `/start` to the bot — it replies with `your Telegram id is <number>`.

## Notes

- The webhook is verified via Telegram's `secret_token` header, so only Telegram can post to you.
- Bursts are safe: shares queue and process a few at a time (see `WORKER_CONCURRENCY`).
- It handles any video link yt-dlp supports, not just Instagram (TikTok, YouTube Shorts, …).
