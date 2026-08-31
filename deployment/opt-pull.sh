#!/usr/bin/env bash
#
# opt-pull.sh — hardened auto-pull for the /opt/askmaddi-prod deploy checkout.
#
# WHY: Apache serves the site straight out of this working tree, and the box's
# build/drip crons continuously write live output into it (browser/**, data/skus.json,
# data/cards/*.json, manifest/sitemap/llms.txt). A plain `git pull --ff-only` wedges
# SILENTLY whenever an incoming commit touches a path the box also wrote live —
# which is exactly what card deploys do. That stranded the DJI Air 2S card ~1h on
# 2026-08-31.
#
# POLICY (surgical, non-regressing): discard local writes ONLY on the exact paths the
# incoming commit delivers (if we're pushing that file, we intend it authoritative for
# this deploy), leave all other live output untouched, refuse LOUDLY on true divergence,
# and log every outcome (tag: askmaddi-pull) so the failure mode is never silent again.
#
# Replaces the raw crontab line:
#   */5 * * * * cd /opt/askmaddi-prod && git pull origin master --quiet 2>&1 | logger -t askmaddi-pull
# with:
#   */5 * * * * bash /opt/askmaddi-prod/deployment/opt-pull.sh
#
# See DEPLOYMENT.md ("Standard deploy path") and phantom-ops memory
# askmaddi-prod-stale-cards-vs-browser.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" || {
  logger -t askmaddi-pull "FATAL: cannot resolve repo root"; exit 1; }
cd "$REPO" || { logger -t askmaddi-pull "FATAL: cannot cd $REPO"; exit 1; }

git fetch --quiet origin master || { logger -t askmaddi-pull "FETCH FAILED"; exit 1; }

L="$(git rev-parse HEAD)"
R="$(git rev-parse origin/master)"
[ "$L" = "$R" ] && exit 0                      # already up to date — nothing to do

# Refuse to auto-resolve real divergence (local commits / rewritten history).
# That needs the manual reconcile procedure, not a cron. Fail loud, don't guess.
if ! git merge-base --is-ancestor "$L" "$R"; then
  logger -t askmaddi-pull "DIVERGED: local ${L:0:8} not ancestor of origin ${R:0:8} — manual reconcile needed"
  exit 1
fi

# Neutralize collisions ONLY on incoming paths; preserve all other live output.
# -z / NUL-delimited to survive any path quoting.
while IFS= read -r -d '' f; do
  [ -z "$f" ] && continue
  git checkout -- "$f" 2>/dev/null || true                          # discard local mod (no-op if clean)
  git ls-files --error-unmatch "$f" >/dev/null 2>&1 || rm -rf "$f"  # drop untracked collision
done < <(git diff --name-only -z "$L" "$R")

n="$(git diff --name-only "$L" "$R" | grep -c .)"
# logger must never influence the wrapper's exit status (no syslog socket => nonzero).
if git merge --ff-only --quiet "$R"; then
  logger -t askmaddi-pull "FF ${L:0:8}->${R:0:8} ($n files)" || true
  # Observability only — gateway code needs a manual restart (privileged; not auto here).
  if git diff --name-only "$L" "$R" | grep -q '^gateway/'; then
    logger -t askmaddi-pull "NOTE: gateway/ changed — restart askmaddi-gateway to apply" || true
  fi
  exit 0
else
  logger -t askmaddi-pull "FF FAILED after cleanup ${L:0:8}->${R:0:8} — manual check needed" || true
  exit 1
fi
