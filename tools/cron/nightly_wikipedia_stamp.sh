#!/bin/bash
# nightly_wikipedia_stamp.sh — de-eBay hero images + legacy-body Wikipedia specs.
# =============================================================================
# Spec: phantom-ops claude/workspace/specs/maddi-images-on-spine.md (hero ladder)
#       + the Wikipedia fetch surface (wikipedia_resolve / spec fallback).
# Policy: cron_wikipedia_stamp = direct-to-master (Crucible-locked, same class as
#         cron_used_prices — verified-safe machine data: the resolver only ever
#         returns an {{Infobox camera}} article and a raster photo, so a run can
#         only ADD a real product photo / real spec title or leave a card alone).
#
# Crontab entry (user askmaddi — same identity as the auto-pull). Runs BEFORE the
# 04:00 resolve so the tree is quiet:
#   30 2 * * * /opt/askmaddi-prod/tools/cron/nightly_wikipedia_stamp.sh >> /home/askmaddi/.askmaddi-bot/wikipedia-stamp.log 2>&1
#
# Chain (each stage gates the next):
#   1. stamp_wikipedia_hero        — resolver -> data/hero_images.json. Idempotent:
#                                     only card_ids not already stamped hit the net.
#   2. stamp_wikipedia_spec_title  — resolver -> data/skus.json spec_surface for
#                                     bodies with NO manufacturer surface. Idempotent.
#   3. CONDITIONAL rebuild if hero_images.json changed — apply_hero_registry bakes
#                                     the clean photo into browser/. (Spec titles
#                                     flow into cards on the next re-drip, not here.)
#   4. bot_push                    — the machine-commit door: fence, [bot:*] commit,
#                                     rebase, push master.
#
# The stamps live in the phantom-ops tree (the resolver is a fact-pipeline
# component, dual-use with the spec pipeline); this cron only triggers them with
# askmaddi-prod paths, the same way the card factory writes into data/cards.
#
# Failure posture mirrors nightly_used_prices: a stamp failure aborts the night
# with a signal; a fully idempotent no-change night exits 0 silently.
set -u

REPO="/opt/askmaddi-prod"
PHANTOM="${PHANTOM_OPS_REPO:-/home/phantomops/phantom-ops}"
AGG="$PHANTOM/claude/workspace/aggregator-build"
SNAPSHOT="/home/askmaddi/.askmaddi-bot/writeback.json"
SIGNAL_DIR="/home/askmaddi/.askmaddi-bot/signals"
JOB="cron_wikipedia_stamp"

cd "$REPO" || { echo "$(date -Iseconds) FATAL: cannot cd $REPO"; exit 2; }

echo "$(date -Iseconds) === NIGHTLY WIKIPEDIA STAMP START ==="

signal() {  # stage reason -> drop a signal file
    python3 - "$SIGNAL_DIR" "$1" "$2" <<'PYEOF'
import json, sys, time
from datetime import datetime, timezone
from pathlib import Path
d = Path(sys.argv[1]); d.mkdir(parents=True, exist_ok=True)
(d / f"wikipedia-stamp-{sys.argv[2]}-{int(time.time())}.json").write_text(json.dumps({
    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "tool": "nightly_wikipedia_stamp", "stage": sys.argv[2],
    "reason": sys.argv[3]}, indent=2))
PYEOF
}

# The stamps must be present (phantom-ops auto-pull clone). Absence is a config
# fault, not a quiet skip — a silent no-op would read as "nothing to stamp".
if [ ! -f "$AGG/stamp_wikipedia_hero.py" ]; then
    echo "$(date -Iseconds) FATAL: stamp tools not found at $AGG (PHANTOM_OPS_REPO?)"
    signal setup "stamp tools absent at $AGG"
    exit 2
fi

# Pre-flight: classify dirt against the SAME frozen snapshot bot_push fences with.
python3 tools/bot_push.py --fence-only \
    --job "$JOB" --snapshot "$SNAPSHOT" --signal-dir "$SIGNAL_DIR"
FENCE=$?
if [ "$FENCE" -eq 2 ]; then
    echo "$(date -Iseconds) ABORT: foreign dirt before stamp — manual investigation"
    exit 2
fi
if [ "$FENCE" -eq 3 ]; then
    echo "$(date -Iseconds) proceeding over pipeline-owned dirt (will bank via bot_push)"
fi

# Stage 1: hero images (de-eBay). Polite delay; idempotent skip of stamped cards.
if ! python3 "$AGG/stamp_wikipedia_hero.py" \
        --cards-dir "$REPO/data/cards" \
        --out "$REPO/data/hero_images.json" --delay 1.0; then
    echo "$(date -Iseconds) ABORT: stamp_wikipedia_hero failed"
    signal hero "stamp_wikipedia_hero.py exited nonzero"
    exit 2
fi

# Stage 2: legacy-body spec titles (uncovered bodies only).
if ! python3 "$AGG/stamp_wikipedia_spec_title.py" \
        --spine "$REPO/data/skus.json" --delay 1.0; then
    echo "$(date -Iseconds) ABORT: stamp_wikipedia_spec_title failed"
    signal spec "stamp_wikipedia_spec_title.py exited nonzero"
    exit 2
fi

# Stage 3: rebuild ONLY if new heroes landed (they need a build to render). A
# spec-title-only change writes skus.json and is banked, but produces no page
# change until the card is re-dripped through the fact pipeline.
if ! git diff --quiet -- data/hero_images.json; then
    echo "$(date -Iseconds) new heroes — rebuilding pages/manifest/sitemap"
    if ! python3 tools/build_site.py --cards-dir data/cards/ \
            --output-dir browser/ --manifest --sitemap; then
        echo "$(date -Iseconds) ABORT: build_site failed — restoring clean tree"
        git checkout -- data/hero_images.json browser/ 2>/dev/null
        signal build "build_site.py exited nonzero; tree restored"
        exit 2
    fi
fi

# Stage 4: bank whatever changed. Idempotent nights change nothing -> clean exit.
if git diff --quiet -- data/hero_images.json data/skus.json browser/ && [ "$FENCE" -ne 3 ]; then
    echo "$(date -Iseconds) === NO NEW HEROES OR SPEC TITLES — clean exit ==="
    exit 0
fi

bash tools/bot_push.sh \
    --job "$JOB" \
    --snapshot "$SNAPSHOT" \
    --signal-dir "$SIGNAL_DIR" \
    --summary "nightly wikipedia de-eBay heroes + legacy-body spec titles"
RC=$?

echo "$(date -Iseconds) === NIGHTLY WIKIPEDIA STAMP DONE (bot_push exit $RC) ==="
exit $RC
