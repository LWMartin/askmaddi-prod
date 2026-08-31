#!/usr/bin/env bash
#
# install-cron.sh — point the askmaddi */5 pull cron at the hardened wrapper
# (deployment/opt-pull.sh), under the APPEND-ONLY CRONTAB DOCTRINE.
#
# The live crontab is NOT version-controlled and a destructive rewrite can wipe
# every job. So this never rewrites: it (1) backs up, (2) COMMENT-retires any live
# raw `git pull` line in place (preserved in-file for recovery), (3) appends the
# wrapper line only if absent, and (4) REFUSES to install a crontab with fewer
# active (uncommented) jobs than it started with. Idempotent — safe to re-run.
#
# Recovery of original lines: /var/log/cron logs every executed cron line verbatim.
#
#   sudo -u askmaddi bash /opt/askmaddi-prod/deployment/install-cron.sh
set -uo pipefail

WRAP="/opt/askmaddi-prod/deployment/opt-pull.sh"
LINE="*/5 * * * * bash $WRAP"
[ -f "$WRAP" ] || { echo "ERR: $WRAP not on box yet — let the auto-pull land it first."; exit 1; }

cur="$(crontab -l 2>/dev/null || true)"
bak="/tmp/crontab.askmaddi.bak.$$"
printf '%s\n' "$cur" > "$bak"
echo "backed up current crontab -> $bak"

active_before="$(printf '%s\n' "$cur" | grep -cE '^[[:space:]]*[^#[:space:]]' || true)"

# (2) comment-retire any LIVE raw git-pull line; leave already-commented lines alone.
new="$(printf '%s\n' "$cur" \
  | sed -E 's@^([[:space:]]*[^#].*git pull origin master.*)$@# retired -> opt-pull.sh (append-only doctrine): \1@')"

# (3) append the wrapper line only if it is not already present.
if ! printf '%s\n' "$new" | grep -qF "bash $WRAP"; then
  new="$(printf '%s\n%s' "$new" "$LINE")"
fi

# (4) hard guard: never shrink the active job set.
active_after="$(printf '%s\n' "$new" | grep -cE '^[[:space:]]*[^#[:space:]]' || true)"
if [ "$active_after" -lt "$active_before" ]; then
  echo "REFUSING: active jobs would drop $active_before -> $active_after (untouched; backup $bak)"
  exit 1
fi

printf '%s\n' "$new" | crontab -
echo "installed (active jobs: $active_before -> $active_after). pull line(s):"
crontab -l | grep -nE 'opt-pull|git pull' || echo "(none found — check $bak)"
