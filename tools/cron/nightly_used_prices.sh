#!/bin/bash
# nightly_used_prices.sh — the cron entry for the used-price refresh job.
# ========================================================================
# Spec: phantom-ops claude/workspace/specs/maddi-writeback-architecture.md
# Policy: cron_used_prices = direct-to-master (Crucible-locked).
#
# Crontab entry (user askmaddi — same identity as the auto-pull):
#   10 10 * * * /opt/askmaddi-prod/tools/cron/nightly_used_prices.sh >> /home/askmaddi/.askmaddi-bot/nightly.log 2>&1
#
# Chain (each stage gates the next):
#   1. refresh_used_prices.py  — eBay Browse via local gateway, precision-gated;
#                                 writes pricing.used_market into data/cards/*.json
#   2. CONDITIONAL on a real card diff: build_site.py rebuild (pages + manifest
#                                 + sitemap). Unconditional rebuild would stamp a
#                                 fresh manifest generated_at every night and
#                                 manufacture heartbeat commits from unchanged
#                                 prices. An unchanged price is not an event.
#   3. bot_push.sh             — the machine-commit door: snapshot fence, gate,
#                                 [bot:*] commit, rebase, push master.
#
# Failure posture: refresh failure aborts the night (signal); a no-change
# night exits 0 silently leaving a clean tree (auto-pull stays healthy).
set -u

REPO="/opt/askmaddi-prod"
SNAPSHOT="/home/askmaddi/.askmaddi-bot/writeback.json"
SIGNAL_DIR="/home/askmaddi/.askmaddi-bot/signals"
GATEWAY="http://127.0.0.1:5001"

cd "$REPO" || { echo "$(date -Iseconds) FATAL: cannot cd $REPO"; exit 2; }

echo "$(date -Iseconds) === NIGHTLY USED-PRICE REFRESH START ==="

# Pre-flight: refuse to run on an already-dirty tree. Dirt here predates us —
# committing it under the bot identity would launder an unknown change, and
# leaving it wedges the auto-pull (recorded failure mode 2026-05-06).
if [ -n "$(git status --porcelain)" ]; then
    echo "$(date -Iseconds) ABORT: working tree dirty before refresh — manual investigation"
    python3 - "$SIGNAL_DIR" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"nightly-preflight-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "nightly_used_prices", "stage": "preflight",
    "reason": "tree dirty before refresh — refusing to launder unknown changes"}, indent=2))
PYEOF
    exit 2
fi

# Stage 1: fetch
if ! python3 tools/refresh_used_prices.py --cards-dir data/cards/ --gateway "$GATEWAY"; then
    echo "$(date -Iseconds) ABORT: refresh_used_prices failed"
    python3 - "$SIGNAL_DIR" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"nightly-refresh-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "nightly_used_prices", "stage": "refresh",
    "reason": "refresh_used_prices.py exited nonzero"}, indent=2))
PYEOF
    exit 2
fi

# Stage 2: rebuild ONLY if a card actually changed
if git diff --quiet -- data/cards/; then
    echo "$(date -Iseconds) === NO PRICE CHANGES — clean exit, no rebuild, no commit ==="
    exit 0
fi

echo "$(date -Iseconds) cards changed — rebuilding pages/manifest/sitemap"
if ! python3 tools/build_site.py --cards-dir data/cards/ --output-dir browser/ --manifest --sitemap; then
    echo "$(date -Iseconds) ABORT: build_site failed — restoring clean tree"
    git checkout -- data/cards/ browser/ 2>/dev/null
    python3 - "$SIGNAL_DIR" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"nightly-build-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "nightly_used_prices", "stage": "build",
    "reason": "build_site.py exited nonzero; tree restored"}, indent=2))
PYEOF
    exit 2
fi

# Stage 3: the door
bash tools/bot_push.sh \
    --job cron_used_prices \
    --snapshot "$SNAPSHOT" \
    --signal-dir "$SIGNAL_DIR" \
    --summary "nightly used-price refresh"
RC=$?

echo "$(date -Iseconds) === NIGHTLY DONE (bot_push exit $RC) ==="
exit $RC
