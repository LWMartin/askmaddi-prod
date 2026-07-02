#!/bin/bash
# weekly_gtin_sweep.sh — the cron entry for the GTIN re-sweep (L2, Amendment A).
# ==============================================================================
# Spec: phantom-ops claude/workspace/specs/maddi-product-substrate.md (Amendment A)
# Cadence decision (2026-07-02): WEEKLY, fixed. The tail is tiny (single-digit
# SKUs x <=6 eBay calls), the sweep is idempotent and drop-safe (drops are not
# persisted, set_gtin is upgrade-only, adjudications are terminal), and a new
# launch (a7-v family) gets caught within a week of eBay catalog population —
# inside the card-build gate window anyway. No decay schedule, no per-entry
# attempt state: "drops stay re-attemptable" is clean precisely because nothing
# remembers the drops. Growth tripwire instead of decay — the sweep's own
# output IS the longitudinal record; when the tail grows enough to hurt, that
# evidence (not imagined scale) is the trigger to design backoff.
#
# WEEKLY HEARTBEAT COUPLING (Lee, 2026-07-02): this run deliberately leads the
# week's publishing rhythm — fresh anchors land BEFORE the week's card
# selection and the teaser-card social rotation draw from the spine. Sweep
# first, then factory drip, then publish gate, then teasers. If the posting
# day moves, move this with it (keep the sweep >= a few hours ahead).
#
# Crontab entry (user askmaddi — same identity as the auto-pull):
#   30 9 * * 1 /opt/askmaddi-prod/tools/cron/weekly_gtin_sweep.sh >> /home/askmaddi/.askmaddi-bot/weekly-sweep.log 2>&1
#
# Chain (each stage gates the next):
#   1. preflight            — refuse a dirty tree (recorded failure mode
#                             2026-05-06: committing pre-existing dirt under the
#                             bot identity launders an unknown change).
#   2. secondpass_gtin.py   — own-listing-first recovery + 4-clause admission
#                             gate over the null-anchor tail; --commit writes
#                             ADMIT + CONFLICT-DROP into data/skus.json.
#   3. CONDITIONAL on a real spine diff: bot_push.sh — the machine-commit door.
#      No site rebuild stage: GTIN anchors do not render on cards today; the
#      spine change alone is the event. (When anchors start rendering, add the
#      conditional build_site stage here, nightly_used_prices.sh is the model.)
#
# Failure posture: sweep failure aborts the week (signal file); a no-recovery
# week exits 0 silently leaving a clean tree (auto-pull stays healthy).
set -u

REPO="/opt/askmaddi-prod"
SIGNAL_DIR="/home/askmaddi/.askmaddi-bot/signals"
SNAPSHOT="/home/askmaddi/.askmaddi-bot/writeback.json"

cd "$REPO" || { echo "$(date -Iseconds) FATAL: cannot cd $REPO"; exit 2; }

echo "$(date -Iseconds) === WEEKLY GTIN RE-SWEEP START ==="

# Stage 1: pre-flight — refuse to run on an already-dirty tree.
if [ -n "$(git status --porcelain)" ]; then
    echo "$(date -Iseconds) ABORT: working tree dirty before sweep — manual investigation"
    python3 - "$SIGNAL_DIR" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"weekly-sweep-preflight-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "weekly_gtin_sweep", "stage": "preflight",
    "reason": "tree dirty before sweep — refusing to launder unknown changes"}, indent=2))
PYEOF
    exit 2
fi

# Stage 2: the sweep (registry-level, own-listing-first, gate-admitted only)
if ! python3 tools/secondpass_gtin.py --skus data/skus.json --commit; then
    echo "$(date -Iseconds) ABORT: secondpass_gtin failed"
    python3 - "$SIGNAL_DIR" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"weekly-sweep-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "weekly_gtin_sweep", "stage": "sweep",
    "reason": "secondpass_gtin.py exited nonzero"}, indent=2))
PYEOF
    exit 2
fi

# Stage 3: commit ONLY if the spine actually changed. An empty-handed sweep
# is not an event — no heartbeat commits.
if git diff --quiet -- data/skus.json; then
    echo "$(date -Iseconds) === NO RECOVERIES — clean exit, no commit ==="
    exit 0
fi

echo "$(date -Iseconds) spine changed — pushing through the machine-commit door"
if ! bash tools/bot_push.sh --job cron_gtin_sweep \
        --snapshot "$SNAPSHOT" \
        --summary "weekly GTIN re-sweep: anchors/receipts recovered"; then
    echo "$(date -Iseconds) WARN: bot_push failed — spine change is LOCAL only (see writeback provisioning)"
    python3 - "$SIGNAL_DIR" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"weekly-sweep-push-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "weekly_gtin_sweep", "stage": "bot_push",
    "reason": "bot_push failed — recoveries committed to local spine only"}, indent=2))
PYEOF
    exit 1
fi

echo "$(date -Iseconds) === WEEKLY GTIN RE-SWEEP COMPLETE ==="
exit 0
