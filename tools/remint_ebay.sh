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
#   sudo -u askmaddi bash /opt/askmaddi-prod/tools/remint_ebay.sh /path/to/other-proposals.json
set -uo pipefail
SPOOL="${1:-/home/askmaddi/pipeline/ebay-proposals.json}"
cd /opt/askmaddi-prod/gateway || { echo "ERROR: gateway dir missing" >&2; exit 2; }
exec flock -n /tmp/askmaddi-resolve.lock /bin/python3 resolve_pass.py "$SPOOL"
