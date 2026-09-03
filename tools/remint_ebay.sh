#!/bin/bash
# remint_ebay.sh — ad-hoc trigger for the eBay-lane resolve pass (mint drones +
# accessories from the eBay proposals spool). Mirrors the `45 4 * * *` crontab
# job so a manual re-mint takes the EXACT same path, and flock-guards against the
# scheduled passes (0 */2 main + 45 4 eBay) so a manual run can't collide.
#
# Exists because the one-line cron invocation is too long to paste over a wrapped
# PuTTY session without breaking mid-command; this short wrapper is paste-safe.
#
# Run as the askmaddi user (owner of skus.json / ledger / logs):
#   sudo -u askmaddi bash /opt/askmaddi-prod/tools/remint_ebay.sh
#   sudo -u askmaddi bash /opt/askmaddi-prod/tools/remint_ebay.sh --fresh
#   sudo -u askmaddi bash /opt/askmaddi-prod/tools/remint_ebay.sh /path/to/other-proposals.json
#
# --fresh sets --retry-ttl-days 0, a ONE-SHOT cooling bypass: it re-attempts the
# proposals sitting in the resolve-attempts ledger's 7-day cooldown (needed right
# after a delist so the clean twin, itself cooling from a prior run, can re-mint).
# The permanent `decontaminated` marks still hold, so true built-dups stay blocked.
set -uo pipefail
EXTRA=()
if [ "${1:-}" = "--fresh" ]; then EXTRA=(--retry-ttl-days 0); shift; fi
SPOOL="${1:-/home/askmaddi/pipeline/ebay-proposals.json}"
cd /opt/askmaddi-prod/gateway || { echo "ERROR: gateway dir missing" >&2; exit 2; }
exec flock -n /tmp/askmaddi-resolve.lock /bin/python3 resolve_pass.py "$SPOOL" "${EXTRA[@]}"
