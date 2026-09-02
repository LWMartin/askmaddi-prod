#!/bin/bash
# refresh_new_prices.sh — Adorama New-price refresh (the priced 'buy new' rung).
# ============================================================================
# Match the live card spine into today's Adorama/Partnerize feed snapshot and
# write pricing.current_new_usd + affiliate_url into data/cards/*.json. SURGICAL:
# price_writeback touches ONLY the New fields, so the eBay used_market band the
# used-price refresh just wrote is preserved (both rungs coexist in one card).
#
# This is an ENHANCEMENT STEP of nightly_used_prices.sh, invoked right after the
# used refresh and BEFORE its conditional rebuild — so the single build_site pass
# and single bot_push there bank BOTH price lanes in one commit. It does NO
# rebuild and NO commit itself.
#
# NON-FATAL by design: a feed/sheet hiccup must never abort the used-price night,
# so every failure path exits 0 with a WARN (the used lane already succeeded).
# The runner lives in phantom-ops (the feed side); askmaddi has read access to it
# and the phantomops-owned snapshot store. Sheet lands in the bot's own dir.
#
# needs_eyes rows (ambiguous / gtin_agree=false) are HELD by price_writeback's
# default — an unattended nightly must not auto-publish a shaky match; those stay
# a craft-seat decision.
set -u

REPO="/opt/askmaddi-prod"
AGG="/home/phantomops/phantom-ops/claude/workspace/aggregator-build"
STORE="/home/phantomops/feed-snapshots/adorama"
SPINE="$REPO/data/skus.json"
SHEET="/home/askmaddi/.askmaddi-bot/price-backfill-latest.json"

# Runner is phantom-ops code pinned to 3.11 (matches adorama-feed-nightly.sh);
# the writeback rides the nightly's /usr/bin/python3 (3.9) like the rest of it.
RUNNER_PY="${NEW_PRICE_RUNNER_PY:-python3.11}"

command -v "$RUNNER_PY" >/dev/null 2>&1 || RUNNER_PY=python3

if [ ! -d "$AGG" ] || [ ! -d "$STORE" ]; then
    echo "$(date -Iseconds) new-prices: WARN runner/store absent ($AGG | $STORE) — skipped"
    exit 0
fi

# 1. Surface the review sheet: live spine × today's feed snapshot (matcher-gated).
if ! ( cd "$AGG" && PYTHONPATH="$AGG" "$RUNNER_PY" -m ingest.price_backfill "$STORE" \
        --spine "$SPINE" --cards-dir "$REPO/data/cards" --out "$SHEET" ); then
    echo "$(date -Iseconds) new-prices: WARN price_backfill failed — new prices unchanged"
    exit 0
fi

# 2. Apply the New fields onto the cards (surgical; needs_eyes held by default).
if ! ( cd "$REPO" && python3 tools/price_writeback.py "$SHEET" \
        --cards-dir data/cards --apply ); then
    echo "$(date -Iseconds) new-prices: WARN price_writeback failed"
    exit 0
fi

echo "$(date -Iseconds) new-prices: applied (sheet $SHEET)"
exit 0
