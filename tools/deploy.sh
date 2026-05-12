#!/usr/bin/env bash
# deploy.sh — Airlock deploy helper for askmaddi.com static frontend
#
# Usage:  sudo bash tools/deploy.sh <component>
#
# Components:
#   affiliate     Deploy browser/js/affiliate.js → <docroot>/js/affiliate.js
#
# Pattern (steps 4–6 of primer/airlock-protocol.md, repo-side steps 1–3 done by commit):
#   1. Backup current production file with timestamp
#   2. Copy from repo into docroot
#   3. Restore ownership + SELinux context
#   4. Verify checksum matches source
#   5. Print rollback command + smoke-test hints
#
# Env overrides:
#   ASKMADDI_DOCROOT (default: /home/askmaddi/public_html)
#   ASKMADDI_USER    (default: askmaddi)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCROOT="${ASKMADDI_DOCROOT:-/home/askmaddi/public_html}"
SERVICE_USER="${ASKMADDI_USER:-askmaddi}"
COMPONENT="${1:-}"

usage() {
    echo "Usage: $0 <component>"
    echo "Components: affiliate"
    exit 1
}

[ -z "$COMPONENT" ] && usage

case "$COMPONENT" in
    affiliate)
        SRC="$REPO_ROOT/browser/js/affiliate.js"
        DST="$DOCROOT/js/affiliate.js"
        SMOKE_URL="https://askmaddi.com/js/affiliate.js"
        ;;
    *)
        echo "Unknown component: $COMPONENT"
        usage
        ;;
esac

[ ! -f "$SRC" ] && { echo "✗ Missing source: $SRC"; exit 2; }
[ ! -d "$(dirname "$DST")" ] && { echo "✗ Target dir missing: $(dirname "$DST")"; exit 2; }

TS=$(date +%Y%m%d-%H%M%S)
BACKUP="$DST.bak.$TS"
SRC_SHA=$(sha256sum "$SRC" | awk '{print $1}')

echo "→ Component: $COMPONENT"
echo "→ Source:    $SRC ($(wc -c < "$SRC") bytes, sha256 ${SRC_SHA:0:12})"
echo "→ Target:    $DST"

if [ -f "$DST" ]; then
    DST_SHA_OLD=$(sha256sum "$DST" | awk '{print $1}')
    if [ "$SRC_SHA" = "$DST_SHA_OLD" ]; then
        echo "✓ No-op: target already matches source (sha256 ${SRC_SHA:0:12})"
        exit 0
    fi
    cp -p "$DST" "$BACKUP"
    echo "✓ Backed up: $BACKUP"
else
    echo "  (no existing file — first deploy)"
fi

cp "$SRC" "$DST"
chown "$SERVICE_USER:$SERVICE_USER" "$DST" 2>/dev/null || true
chmod 0644 "$DST"
command -v restorecon >/dev/null && restorecon "$DST" 2>/dev/null || true

DST_SHA_NEW=$(sha256sum "$DST" | awk '{print $1}')
if [ "$SRC_SHA" != "$DST_SHA_NEW" ]; then
    echo "✗ Checksum mismatch after copy — aborting"
    [ -f "$BACKUP" ] && { cp "$BACKUP" "$DST"; echo "  Rolled back from $BACKUP"; }
    exit 3
fi

echo "✓ Deployed (sha256 ${DST_SHA_NEW:0:12})"
[ -f "$BACKUP" ] && echo "  Rollback:  cp $BACKUP $DST"
echo
echo "Smoke test (from your laptop):"
echo "  curl -sI $SMOKE_URL | head -3"
echo "  curl -s  $SMOKE_URL | sha256sum   # expect ${SRC_SHA:0:12}…"
