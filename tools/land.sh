#!/usr/bin/env bash
# land.sh — the guarded push path for askmaddi-prod.
# ==========================================================================
# The sanctioned way to push a clone's commits to origin/master. Fast-forwards
# past any auto-status commit, then — CRITICALLY — refuses to push a data/skus.json
# that would revert /opt's live spine growth (the 231->195 wipe, 2026-09-02).
#
# Why here and not a git hook: hooks aren't tracked/deployed across the fleet of
# clones; a tracked helper is. Every skus.json push MUST go through this.
#
#   bash tools/land.sh
#   SPINE_ALLOW_DROP="slug-a slug-b" bash tools/land.sh   # intentional delist
#
# Env:
#   LIVE_SKUS         /opt live spine to guard against (default below)
#   SPINE_ALLOW_DROP  space-separated slugs this push intentionally removes
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || { echo "CD FAILED"; exit 2; }
cd "$REPO" || { echo "CD FAILED"; exit 2; }
LIVE_SKUS="${LIVE_SKUS:-/opt/askmaddi-prod/data/skus.json}"

echo "== local HEAD =="
git log --oneline -1

echo "== fetch + fast-forward past any auto-status commit =="
git fetch origin master --quiet
if [ "$(git rev-list --count HEAD..origin/master)" != "0" ]; then
  echo "== origin moved; rebasing =="
  git pull --rebase origin master || { echo "REBASE FAILED — resolve then re-run"; exit 3; }
fi

echo "== unpushed vs origin/master =="
git log --oneline origin/master..HEAD

# ── SPINE GUARD: block a stale/partial skus.json from reverting live growth ──
if git diff --name-only origin/master..HEAD | grep -qx "data/skus.json"; then
  echo "== spine guard (push touches data/skus.json) =="
  drop_args=()
  for s in ${SPINE_ALLOW_DROP:-}; do drop_args+=(--allow-drop "$s"); done
  if [ ! -r "$LIVE_SKUS" ]; then
    echo "SPINE GUARD: cannot read live spine $LIVE_SKUS — refusing to push blind." >&2
    echo "  (set LIVE_SKUS, or run where /opt is reachable)" >&2
    exit 4
  fi
  if ! python3 "$REPO/tools/spine_guard.py" \
        --clone-skus "$REPO/data/skus.json" --live-skus "$LIVE_SKUS" "${drop_args[@]}"; then
    echo "SPINE GUARD BLOCKED THE PUSH — see above. Re-bank /opt growth or declare drops." >&2
    exit 5
  fi
fi

echo "== pushing =="
git push origin master
rc=$?
echo "PUSH_RC=$rc"
echo "== live origin/master now =="
git ls-remote origin -h refs/heads/master | awk '{print $1}'
exit $rc
