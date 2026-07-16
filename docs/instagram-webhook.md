# Instagram DM webhook (Phase 1)

Share a reel to a dedicated Instagram account — exactly like DMing it to a friend — and
Meta pushes it straight to your server. No iOS Shortcut, no clipboard, nothing fragile on
the phone. This is the most reliable ingestion path because Meta hands you the reel's video
URL directly instead of you scraping it.

```
you (personal IG) --share reel--> your bot IG account (Creator, linked to a Meta app)
                                        │  Meta Messaging webhook (POST /webhook)
                                        ▼
                        reel-relay downloads the CDN video -> frames + transcript -> Claude
```

## What you need

- A **second Instagram account** to act as the bot (a burner is fine), switched to a
  **Professional (Creator or Business)** account: IG app → Settings → *Account type and
  tools* → *Switch to professional account* → **Creator**.
- Your running reel-relay server's public base URL (e.g. `https://<your-host>`).
- ~30 minutes, once.

## Server side (already built)

The server exposes `GET/POST /webhook`. Set two env vars in your server's `.env`:

```bash
META_VERIFY_TOKEN=<any random string>   # python3 -c "import secrets; print(secrets.token_urlsafe(24))"
META_APP_SECRET=<from your Meta app, step 5 below>
```

Restart the service after adding them (`sudo systemctl restart reel-relay`, or `make dev`).
Until both are set the webhook stays inert (returns 403), so it's safe to deploy early.

## Meta console setup

1. **Create a Meta app.** Go to [developers.facebook.com](https://developers.facebook.com)
   → *My Apps* → *Create App* → pick the **Instagram** use case (API setup with Instagram
   login). This path needs **no** Facebook Page.
2. **Add the Instagram product** to the app, and under *Instagram → API setup with Instagram
   login*, **connect your bot Instagram account** (log in, authorize). Generate an access
   token if prompted.
3. **Configure the webhook.** In the app dashboard → *Webhooks* (or the Instagram product's
   webhook panel):
   - **Callback URL:** `https://<your-host>/webhook`
   - **Verify token:** the `META_VERIFY_TOKEN` you set above
   - Click **Verify and Save** — Meta calls your `GET /webhook` and your server echoes the
     challenge. It should turn green immediately (server must be running).
4. **Subscribe to the `messages` field** for Instagram (in the same webhooks panel, click
   *Subscribe* / *Add subscriptions* → check **messages**).
5. **Copy the App Secret.** App dashboard → *App settings → Basic → App Secret → Show* →
   put it in your server `.env` as `META_APP_SECRET`, and restart the service. (This is what
   verifies each webhook's `X-Hub-Signature-256` so only Meta can post to you.)
6. **Permissions.** The app needs `instagram_business_manage_messages` (a.k.a. instagram
   messaging). In **Development mode** this works for accounts that have a role on the app —
   i.e. your own bot account — with **no App Review** needed, which is perfect for a personal
   tool. Make sure your bot account is added as a tester/role if prompted.
7. In the **bot account's** Instagram app: Settings → *Messages and story replies* /
   *Connected tools* → ensure the app is **allowed to access messages**.

## Use it

From your **personal** Instagram: open a reel → share (paper airplane) → send it to your
**bot account** in a DM. Within a few seconds the reel appears in reel-relay, and you can ask
Claude *"what's in the reel I just shared?"*

## Troubleshooting

- **"Verify and Save" fails / stays red** → server not reachable or `META_VERIFY_TOKEN`
  mismatch. Test manually: opening
  `https://<your-host>/webhook?hub.mode=subscribe&hub.verify_token=<token>&hub.challenge=hello`
  should print `hello`.
- **Reels don't arrive** → check the server logs (`journalctl -u reel-relay -f`). Every
  webhook delivery is logged raw (`webhook received: {...}`), so you can see exactly what Meta
  sent. If you see the payload but no reel queued, the attachment type/shape differs — send me
  the logged JSON and it's a one-line parser tweak.
- **403 on every POST** → `META_APP_SECRET` is missing/wrong, or the service wasn't restarted
  after setting it.
- **Nothing logged at all** → the `messages` subscription (step 4) isn't active, or the bot
  account hasn't granted message access (step 7).

## Security notes

- Every webhook POST is HMAC-verified against `META_APP_SECRET` before processing — requests
  without a valid `X-Hub-Signature-256` are rejected.
- The reel's video comes from Meta's signed CDN URL, so it downloads fine from any host
  (unlike scraping, which needs a residential IP).
