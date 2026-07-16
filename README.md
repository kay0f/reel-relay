# reel-relay

**Forward an Instagram reel to Claude the same way you send it to a friend - then have Claude watch it, research it, and build what it shows.**

<p>
  <a href="https://github.com/siemd2/reel-relay/actions/workflows/tests.yml"><img alt="tests" src="https://github.com/siemd2/reel-relay/actions/workflows/tests.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="MCP" src="https://img.shields.io/badge/Model_Context_Protocol-connector-8A2BE2">
  <img alt="Self-hosted" src="https://img.shields.io/badge/self--hosted-yes-orange">
</p>

Ever be doomscrolling reels, hit one with a genuinely good idea - for me it's usually some insane UI demo - and I think *"I want Claude to build a demo"*... but you can't be bothered to pause, transcribe it, and explain it? So you save it to the graveyard folder you'll never open again.

**reel-relay closes that gap.** Share the reel to your self-hosted endpoint (an iOS Shortcut in your share sheet, or an Instagram DM), and it lands in Claude as a **timestamped transcript plus sampled video frames** - so Claude can actually "watch" it and you can just say *"what's in the reel I just shared?"* Then take it further: *"research whether this tactic would work for my project"* or *"build the thing in that reel."*

> The share gesture you already use 20 times a day - now one of the recipients is Claude.

---

## How it works

```
  phone: Telegram bot  ·  iOS Shortcut  ·  Instagram DM
            │  POST /ingest {url}   (X-Ingest-Secret)
            ▼
   your server (localhost or a free cloud VM)
     ├─ yt-dlp        → download the reel (≤720p mp4)
     ├─ ffmpeg        → ~30 deduplicated keyframes (JPEG, 512px wide)
     ├─ captions →    else Groq whisper-large-v3 → timestamped transcript
     └─ SQLite + frames on disk (the video itself is deleted)
            │
            ▼
   FastMCP server (streamable HTTP, same process)
            │  get_reel() → transcript + frames as MCP images
            ▼
   Claude  (web · desktop · mobile · Claude Code)  via a custom connector
```

Everything runs in **one Python process** you host yourself. Claude reaches it as a
standard [MCP](https://modelcontextprotocol.io) custom connector - the reel's transcript
arrives as text and its frames arrive as images, so Claude sees both what's *said* and
what's *shown*.

## Features

- **Multiple ways to ingest** - forward a reel to a [Telegram bot](docs/telegram.md) (easiest, works on iOS *and* Android, no app review), an [iOS share-sheet Shortcut](shortcuts/README.md), or an [Instagram DM webhook](docs/instagram-webhook.md).
- **Claude actually "watches" the video** - timestamped transcript + evenly-sampled, deduplicated frames delivered as MCP image content.
- **Three MCP tools** tuned so Claude knows to grab "the reel I just shared" with zero ceremony.
- **Free transcription** via Groq's `whisper-large-v3` (falls back to it only when a reel has no captions).
- **Runs anywhere** - your Mac, a Raspberry Pi, or a free-forever cloud VM. Binds to localhost; you expose it through a tunnel, so no ports are opened to the world.
- **Self-healing & tiny** - the video is never stored (~0.6 MB of frames per reel), the disk auto-prunes, and the service auto-restarts.

## MCP tools

| Tool | What it does |
|---|---|
| `list_recent_reels(limit=10)` | Browse recently shared reels (id, title, transcript preview). |
| `get_reel(reel_id?, include_frames=true, max_frames=10)` | Watch a reel - full timestamped transcript + sampled frames. **No `reel_id` → the most recent one** (that's "the reel I just shared"). |
| `get_reel_frames(reel_id?, start_s, end_s)` | Zoom into a specific time window for more visual detail. |

---

## Quickstart (self-host in ~10 minutes)

**Requirements:** Python 3.11+, [`uv`](https://docs.astral.sh/uv/), `ffmpeg`, and a free
[Groq API key](https://console.groq.com). A [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/)
or [Tailscale](https://tailscale.com) tunnel to expose it, and a Claude account with custom connectors.

```bash
git clone https://github.com/siemd2/reel-relay.git
cd reel-relay

# system deps (macOS shown; Linux: apt install ffmpeg)
brew install ffmpeg uv

uv sync                 # install Python deps
make secrets            # generate INGEST_SECRET + MCP_PATH_TOKEN into .env
# add your Groq key:  echo 'GROQ_API_KEY=gsk_...' >> .env   (or edit .env)
make test               # offline tests - synthesizes a clip with ffmpeg, no network
make dev                # start the server on http://127.0.0.1:8787
```

**Expose it & connect Claude:**

```bash
# quickest tunnel for testing (random URL, no domain needed):
cloudflared tunnel --url http://localhost:8787

make mcp-url            # prints your connector URL: https://<tunnel-host>/mcp-<token>/mcp
```

Add that URL in Claude → **Settings → Connectors → Add custom connector**. No OAuth - the
long random path segment *is* the credential, so treat the URL as a secret.

**Share reels to it.** Easiest is a **Telegram bot** — works on iOS *and* Android, no app
review, ~2-minute setup: [docs/telegram.md](docs/telegram.md). Prefer something else? An
[iOS share-sheet Shortcut](shortcuts/README.md) or the [Instagram DM webhook](docs/instagram-webhook.md).
Then: share/forward a reel → open Claude → *"what's in the reel I just shared?"*

## Run it 24/7 for free (always-on cloud)

Instagram rate-limits datacenter IPs, so the download step wants a "residential-looking"
connection - but in practice a **free-tier cloud VM works fine** for personal volume. The
setup the author runs (≈$0/month, survives reboots, independent of your laptop):

1. **Free VM** - [Oracle Cloud Always Free](https://www.oracle.com/cloud/free/) (`VM.Standard.E2.1.Micro`), or any small Linux box.
2. **Deploy** - `uv sync`, drop your `.env`, install the [`ops/reel-relay.service`](ops/reel-relay.service) systemd unit (`systemctl enable --now reel-relay`), and add the hourly [`ops/cleanup.sh`](ops/cleanup.sh) cron.
3. **HTTPS with no open ports** - [Tailscale Funnel](https://tailscale.com/kb/1223/funnel): `tailscale funnel --bg 8787` gives you a permanent `https://<machine>.<tailnet>.ts.net` URL that routes inbound through Tailscale's edge (the VM never exposes a public port; the app stays bound to localhost). Register that URL + `/mcp-<token>/mcp` as your connector.

On macOS instead, `make launchd-install` keeps the server + a Cloudflare tunnel running at login.

---

## Configuration

All settings come from environment variables (see [`.env.example`](.env.example)):

| Var | Required | Purpose |
|---|---|---|
| `GROQ_API_KEY` | for transcription | Groq key for `whisper-large-v3` (free tier). |
| `INGEST_SECRET` | ✅ | Shared secret the `/ingest` endpoint requires (`X-Ingest-Secret` header). |
| `MCP_PATH_TOKEN` | ✅ | Long random segment in the MCP URL - the connector's only auth. |
| `DATA_DIR`, `HOST`, `PORT` | optional | Storage location and bind address (defaults: `./data`, `127.0.0.1`, `8787`). |
| `YTDLP_COOKIES_FROM_BROWSER` | optional | Reuse a browser's Instagram session for reels that require login. |

`make secrets` fills the two required secrets for you.

## Security

- The server binds `127.0.0.1` only; the tunnel is the sole path in.
- `/ingest` and the debug endpoints require the `X-Ingest-Secret` header (constant-time compared).
- The MCP endpoint is guarded by a long random path token - **the connector URL is a secret**.
- Interactive API docs (`/docs`, `/openapi.json`) are disabled since the app faces the internet.
- Videos are never persisted; `data/` and `.env` are gitignored.

## Roadmap

- Not sure, will evolve with needs.

Contributions welcome - open an issue or PR.

## How it's built

Python 3.11+ · [FastAPI](https://fastapi.tiangolo.com) · [FastMCP](https://github.com/jlowin/fastmcp) (streamable HTTP) · [yt-dlp](https://github.com/yt-dlp/yt-dlp) · `ffmpeg` · [Groq](https://groq.com) `whisper-large-v3` · SQLite · [`uv`](https://docs.astral.sh/uv/). One process, no external database, no build step.

## License

[MIT](LICENSE) - do anything you like; self-host your own.
