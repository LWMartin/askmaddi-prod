#!/bin/bash
# certbot DEPLOY hook — runs after ANY successful certificate renewal.
# ================================================================
# The askmaddi.com vhost borrows ramish.io's certificate, and its /search +
# /adorama + /subscribe ProxyPass routes were hand-added after certbot last
# generated the vhost. If a renewal regenerates the vhost, those routes vanish
# and /search + /adorama 404 silently. This hook self-heals within the same
# renewal: detect drift, restore the git-tracked known-good vhost, reload httpd.
#
# Restoring verbatim is safe because the cert path is the stable
# /etc/letsencrypt/live/ramish.io symlink (never changes across renewals).
# Idempotent — a no-op when the routes are intact.
#
# INSTALL: copy to /etc/letsencrypt/renewal-hooks/deploy/10-askmaddi-proxy.sh
# (chmod 755). The canonical copy is tracked here in the repo.
set -u

LOG=/var/log/askmaddi-proxy-guard.log
LIVE=/etc/httpd/conf.d/askmaddi.com-le-ssl.conf
REPO=/opt/askmaddi-prod/deployment/httpd
GOOD="$REPO/askmaddi.com-le-ssl.conf"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null; }

if [ ! -f "$GOOD" ]; then
    log "deploy-hook: tracked vhost $GOOD absent (repo not deployed?) — SKIP"
    exit 0
fi

if bash "$REPO/verify_proxy.sh" "$LIVE" >/dev/null 2>&1; then
    log "deploy-hook: proxy routes intact, no action"
    exit 0
fi

log "deploy-hook: DRIFT — a route is missing from $LIVE; restoring from $GOOD"
cp -f "$LIVE" "${LIVE}.pre-guard.$(date +%Y%m%d-%H%M%S)" 2>/dev/null
cp -f "$GOOD" "$LIVE"

if httpd -t >>"$LOG" 2>&1; then
    if systemctl reload httpd >>"$LOG" 2>&1; then
        log "deploy-hook: restored tracked vhost + reloaded httpd OK"
    else
        log "deploy-hook: ERROR reloading httpd after restore — manual attention"
        exit 1
    fi
else
    log "deploy-hook: ERROR httpd -t failed after restore — NOT reloading; manual attention"
    exit 1
fi
