SHELL := /bin/bash
-include .env

HOST ?= 127.0.0.1
PORT ?= 8787
TUNNEL_NAME ?= reel-relay

.PHONY: run dev test secrets mcp-url ingest tunnel launchd-install launchd-uninstall

run: ## start the server (foreground)
	uv run uvicorn server.main:app --host $(HOST) --port $(PORT)

dev: ## start with auto-reload
	uv run uvicorn server.main:app --host $(HOST) --port $(PORT) --reload

test:
	uv run pytest

secrets: ## create .env from the example and fill empty secrets with random values
	@test -f .env || cp .env.example .env
	@grep -q '^INGEST_SECRET=..*' .env || sed -i '' "s|^INGEST_SECRET=.*|INGEST_SECRET=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')|" .env
	@grep -q '^MCP_PATH_TOKEN=..*' .env || sed -i '' "s|^MCP_PATH_TOKEN=.*|MCP_PATH_TOKEN=$$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env
	@echo ".env ready (add your GROQ_API_KEY if it is still empty):"
	@grep -E '^(GROQ_API_KEY|INGEST_SECRET|MCP_PATH_TOKEN)=' .env | sed -E 's/=(.{1,6}).+/=\1.../'

mcp-url: ## print the MCP URL to register as a Claude custom connector
	@echo "Local:  http://$(HOST):$(PORT)/mcp-$(MCP_PATH_TOKEN)/mcp"
	@echo "Public: https://<your-tunnel-hostname>/mcp-$(MCP_PATH_TOKEN)/mcp"

ingest: ## send a URL through the local ingest endpoint: make ingest URL=https://...
	@test -n "$(URL)" || { echo "usage: make ingest URL=https://www.instagram.com/reel/..."; exit 1; }
	curl -s -X POST "http://$(HOST):$(PORT)/ingest" \
		-H "X-Ingest-Secret: $(INGEST_SECRET)" \
		-H "Content-Type: application/json" \
		-d '{"url":"$(URL)"}' | python3 -m json.tool

tunnel: ## run the named Cloudflare tunnel (see README for one-time setup)
	cloudflared tunnel run $(TUNNEL_NAME)

launchd-install: ## render plists and register server + tunnel to run at login
	@mkdir -p ops/rendered data/logs
	@sed -e "s|__REPO__|$(CURDIR)|g" -e "s|__UV__|$$(command -v uv)|g" -e "s|__HOST__|$(HOST)|g" -e "s|__PORT__|$(PORT)|g" \
		ops/com.reelrelay.server.plist.template > ops/rendered/com.reelrelay.server.plist
	@sed -e "s|__REPO__|$(CURDIR)|g" -e "s|__CLOUDFLARED__|$$(command -v cloudflared)|g" -e "s|__TUNNEL_NAME__|$(TUNNEL_NAME)|g" \
		ops/com.reelrelay.tunnel.plist.template > ops/rendered/com.reelrelay.tunnel.plist
	cp ops/rendered/com.reelrelay.server.plist ops/rendered/com.reelrelay.tunnel.plist ~/Library/LaunchAgents/
	-launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/com.reelrelay.server.plist
	-launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/com.reelrelay.tunnel.plist
	@echo "Installed. Check with: launchctl list | grep reelrelay   (logs: data/logs/)"

launchd-uninstall:
	-launchctl bootout gui/$$(id -u)/com.reelrelay.server
	-launchctl bootout gui/$$(id -u)/com.reelrelay.tunnel
	rm -f ~/Library/LaunchAgents/com.reelrelay.server.plist ~/Library/LaunchAgents/com.reelrelay.tunnel.plist
