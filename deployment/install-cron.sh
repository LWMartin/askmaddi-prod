#!/usr/bin/env bash
#
# install-cron.sh — idempotently point the askmaddi */5 pull cron at the hardened
# wrapper (deployment/opt-pull.sh). Run AS the askmaddi user, after the wrapper has
# landed on the box:
#
#   sudo -u askmaddi bash /opt/askmaddi-prod/deployment/install-cron.sh
#
# Safe to run repeatedly: it backs up the current crontab, removes any prior pull
# line (raw `git pull` OR an existing wrapper line), and installs the canonical one.
set -uo pipefail

WRAP="/opt/askmaddi-prod/deployment/opt-pull.sh"
LINE="*/5 * * * * bash $WRAP"

[ -f "$WRAP" ] || { echo "ERR: $WRAP not on box yet — let the auto-pull land it first."; exit 1; }

cur="$(crontab -l 2>/dev/null || true)"
bak="/tmp/crontab.askmaddi.bak.$$"
printf '%s\n' "$cur" > "$bak"
echo "backed up current crontab -> $bak"

# Drop any existing pull-related line(s), then append the canonical wrapper line.
new="$(printf '%s\n' "$cur" | grep -vE 'git pull origin master|opt-pull\.sh' || true)"
{ printf '%s\n' "$new"; printf '%s\n' "$LINE"; } | crontab -

echo "installed. active pull line(s):"
crontab -l | grep -nE 'opt-pull|git pull' || echo "(none found — check $bak)"
