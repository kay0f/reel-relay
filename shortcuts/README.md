# iOS Shortcut: "Send Reel to Claude"

One-time setup, ~3 minutes. The Shortcut takes a reel from Instagram's share sheet and
POSTs its URL to your reel-relay server through the Cloudflare tunnel.

You'll need two values from your Mac (from `reel-relay/.env`):

- your public hostname, e.g. `reels.example.com` (the tunnel hostname from the main README)
- your `INGEST_SECRET`

## Build the Shortcut

1. Open **Shortcuts** on iPhone → **+** to create a new shortcut.
2. Tap the name at the top → rename to **Send Reel to Claude**.
3. Tap the **ⓘ** (info) button → enable **Show in Share Sheet**.
4. Tap **Receive Any input** (under "Share Sheet") and limit it to **URLs** and **Text**.
5. Add action **Get URLs from Input**, with input = **Shortcut Input**.
   (Instagram shares the reel as text containing the link; this pulls the URL out.)
6. Add action **Get Contents of URL** and configure it (tap the ▸ arrow):
   - **URL**: `https://reels.example.com/ingest`  ← your hostname
   - **Method**: `POST`
   - **Headers**: add one - Key `X-Ingest-Secret`, Value `<your INGEST_SECRET>`
   - **Request Body**: `JSON`, add one field - Key `url`, Type `Text`,
     Value = the **URLs** variable from step 5.
7. Add action **Show Notification**:
   - Title: `Reel sent to Claude`
   - Body: the **Contents of URL** variable (shows `{"id": ..., "status": "pending"}`).

## Use it

1. In Instagram, open a reel → tap the **share arrow** → **Share to...** (or the ⋯ row)
   → **Send Reel to Claude**. (First time: scroll the share sheet and tap **Edit Actions...**
   / **More** to enable the shortcut.)
2. Wait ~15-60 seconds for download + transcription.
3. In any Claude app with the connector enabled, say: *"Check out the reel I just shared."*

## Fallback: clipboard variant

If some app won't hand the URL to the share sheet: **Copy Link** on the reel, then run a
variant of the shortcut where step 5's input is **Clipboard** instead of Shortcut Input
(add it to your Home Screen or an Action Button for one-tap use).

## Troubleshooting

- **Notification shows `{"detail":"missing or invalid X-Ingest-Secret header"}`** - the
  header name/value in step 6 doesn't match `INGEST_SECRET` in `.env`.
- **No response / timeout** - tunnel or server down on the Mac. Check
  `launchctl list | grep reelrelay` and `data/logs/`.
- **Reel stays `pending` then `failed`** - check `data/logs/server.err.log`; some reels
  require Instagram login (see `YTDLP_COOKIES_FROM_BROWSER` in the main README).
