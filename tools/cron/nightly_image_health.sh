#!/bin/bash
# nightly_image_health.sh — cron entry for image rot detection (images-on-spine D5).
# ==============================================================================
# Spec: phantom-ops claude/workspace/specs/maddi-images-on-spine.md (D5)
#
# READ-ONLY by design: three checks (rendered-vs-spine mismatch, dead image
# URL, dead eBay LISTING) over the published cards and the spine, ~14 HEAD
# requests + ~14 gateway resolves, findings dropped as signal files.
# NO commit stage, NO rebuild stage, NO auto-republish — publish stays behind
# the human gate; the air gap is structural. Because nothing writes to the
# tree, there is no dirty-tree preflight here (contrast weekly_gtin_sweep.sh).
#
# Timing: after the 04:00 resolve cron, so tonight's spine refresh is what the
# comparison reads.
#
# Crontab entry (user askmaddi — same identity as the auto-pull):
#   30 4 * * * /opt/askmaddi-prod/tools/cron/nightly_image_health.sh >> /home/askmaddi/.askmaddi-bot/image-health.log 2>&1
set -u

REPO="/opt/askmaddi-prod"
SIGNAL_DIR="/home/askmaddi/.askmaddi-bot/signals"
GATEWAY="${GATEWAY:-http://127.0.0.1:5001}"

cd "$REPO" || { echo "$(date -Iseconds) FATAL: cannot cd $REPO"; exit 2; }

echo "$(date -Iseconds) === NIGHTLY IMAGE HEALTH CHECK START ==="

# --gateway enables the third check (listing liveness). It goes through the
# LOCAL gateway, same door refresh_used_prices.py uses, so eBay creds stay in
# gateway/.env and this read-only job never handles them.
python3 tools/image_health_check.py \
    --skus data/skus.json \
    --manifest browser/cards-manifest.json \
    --signals "$SIGNAL_DIR" \
    --gateway "$GATEWAY"
RC=$?

case "$RC" in
    0) echo "$(date -Iseconds) === HEALTHY — no findings ===" ;;
    1) echo "$(date -Iseconds) === FINDINGS FLAGGED — see $SIGNAL_DIR (no auto-republish; re-render through the gate) ===" ;;
    *) echo "$(date -Iseconds) === CHECK FAILED (rc=$RC) — inputs missing? ===" ;;
esac

exit "$RC"
