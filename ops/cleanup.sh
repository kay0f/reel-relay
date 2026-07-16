#!/bin/bash
# Disk safety net for long-running deployments. Videos are never persisted (the
# pipeline deletes them), so only small frame JPEGs + transcripts accumulate, but
# this guarantees the disk can never fill and take the service down.
#
# Run hourly via cron:
#   17 * * * * /home/ubuntu/reel-relay/ops/cleanup.sh
set -euo pipefail

DATA="${DATA_DIR:-/home/ubuntu/reel-relay/data}/frames"
THRESHOLD="${DISK_THRESHOLD:-85}"   # start pruning when / exceeds this % used

# Belt-and-suspenders: drop any leaked temp download dirs older than a day.
find /tmp -maxdepth 1 -type d -name 'reel-*' -mtime +1 -exec rm -rf {} + 2>/dev/null || true

# If the root disk is over threshold, delete oldest reel frame-dirs until under it.
# (Transcripts stay in SQLite, so pruned reels are still discussable as text.)
while [ "$(df --output=pcent / | tail -1 | tr -dc '0-9')" -gt "$THRESHOLD" ]; do
  oldest="$(ls -1dt "$DATA"/*/ 2>/dev/null | tail -1 || true)"
  [ -z "$oldest" ] && break
  echo "$(date -Is) pruning $oldest"
  rm -rf "$oldest"
done
