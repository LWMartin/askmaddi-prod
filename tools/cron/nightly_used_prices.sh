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
# Failure posture: refresh failure aborts the night (signal). Preflight
# classifies dirt against the frozen bot snapshot: FOREIGN dirt aborts
# (never launder unknown changes); pipeline-owned dirt proceeds and is
# banked through bot_push — the nightly is the consolidated daily banker
# for allowlisted runtime writes (2026-07-16, after 10 straight preflight
# aborts on the pipeline's own daily skus.json writes). A fully clean
# no-change night still exits 0 silently.
set -u

REPO="/opt/askmaddi-prod"
SNAPSHOT="/home/askmaddi/.askmaddi-bot/writeback.json"
SIGNAL_DIR="/home/askmaddi/.askmaddi-bot/signals"
GATEWAY="http://127.0.0.1:5001"

cd "$REPO" || { echo "$(date -Iseconds) FATAL: cannot cd $REPO"; exit 2; }

echo "$(date -Iseconds) === NIGHTLY USED-PRICE REFRESH START ==="

# Pre-flight: classify dirt against the SAME frozen snapshot bot_push
# fences with (one source of truth — preflight and fence cannot disagree).
#   exit 0 -> clean tree
#   exit 3 -> pipeline-owned dirt only (daily resolve/sweep/publish writes
#             on allowlisted paths) — proceed; stage 3 banks it through
#             the door. 10 straight aborts 7/07–7/16 were this class.
#   exit 2 -> FOREIGN dirt — refuse to launder unknown changes (recorded
#             failure mode 2026-05-06); signal written by the classifier.
python3 tools/bot_push.py --fence-only \
    --job cron_used_prices --snapshot "$SNAPSHOT" --signal-dir "$SIGNAL_DIR"
FENCE=$?
if [ "$FENCE" -eq 2 ]; then
    echo "$(date -Iseconds) ABORT: foreign dirt before refresh — manual investigation"
    exit 2
fi
if [ "$FENCE" -eq 3 ]; then
    echo "$(date -Iseconds) proceeding over pipeline-owned dirt (will bank via bot_push)"
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
    if [ "$FENCE" -eq 3 ]; then
        # No price movement, but the tree carries pipeline-owned writes
        # (resolve rotations, provenance, sweep rescues, publish strands).
        # Stranding them re-arms tomorrow's preflight and wedges the
        # auto-pull the day a pull touches those paths — bank them now.
        echo "$(date -Iseconds) no price changes — banking carried pipeline-owned writes"
        bash tools/bot_push.sh \
            --job cron_used_prices \
            --snapshot "$SNAPSHOT" \
            --signal-dir "$SIGNAL_DIR" \
            --summary "banked pipeline-owned runtime writes (no price changes)"
        RC=$?
        echo "$(date -Iseconds) === NIGHTLY DONE (bot_push exit $RC) ==="
        exit $RC
    fi
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
