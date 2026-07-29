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

# INTERPRETER IS PINNED, NOT RESOLVED. A bare `python3` here meant two
# different interpreters depending on who invoked it: cron runs with
# PATH=/sbin:/bin:/usr/sbin:/usr/bin (no /usr/local/bin), so `python3` is
# /usr/bin/python3 = 3.9.25, while an interactive root shell picks up
# /usr/local/bin/python3 = 3.11.13. Identical command, different result —
# which made the gate's effective interpreter depend on invocation context
# and unreproducible by hand.
#
# 3.9 is the correct pin: the nightly pipeline (card_factory, the two minting
# stages, image_catalog_sweep) all run on /usr/bin/python3. The venv 3.11.13
# serves the gateway process only. Validating on 3.9 protects what actually
# runs unattended. See tools/test_py39_compat.py for the 2026-06-25 incident
# this floor exists to prevent.
BOT_PUSH_PYTHON="${BOT_PUSH_PYTHON:-/usr/bin/python3}"
exec "$BOT_PUSH_PYTHON" tools/bot_push.py "$@"
