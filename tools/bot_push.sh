#!/bin/bash
# bot_push.sh — thin cron entry point for the machine-commit door.
# All logic lives in bot_push.py (testable). Spec: maddi-writeback-architecture.
#
# Usage (VPS cron, after deploy key provisioning):
#   bash tools/bot_push.sh --job cron_used_prices \
#        --snapshot /home/lwmpost/phantom-ops/bus/crucible/spawns/writeback.json \
#        --summary "nightly used-price refresh"
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 tools/bot_push.py "$@"
