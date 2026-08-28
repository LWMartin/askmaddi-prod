#!/bin/bash
# Verify the LIVE askmaddi vhost carries every ProxyPass route that the tracked
# known-good copy declares. This is the drift detector behind the certbot
# renewal deploy-hook (a cert renewal that regenerates the vhost can silently
# drop the hand-added /search + /adorama + /subscribe routes).
#
#   verify_proxy.sh [live_vhost_path]
#   exit 0 = every declared route present   exit 1 = drift (missing routes named)
set -u

LIVE="${1:-/etc/httpd/conf.d/askmaddi.com-le-ssl.conf}"
GOOD="$(cd "$(dirname "$0")" && pwd)/askmaddi.com-le-ssl.conf"

[ -f "$LIVE" ] || { echo "MISSING live vhost: $LIVE"; exit 1; }
[ -f "$GOOD" ] || { echo "MISSING tracked vhost: $GOOD"; exit 1; }

missing=0
# Each declared forward route (ProxyPass <route> ...), not ProxyPassReverse.
while read -r route; do
    [ -n "$route" ] || continue
    if ! grep -qE "^[[:space:]]*ProxyPass[[:space:]]+${route}([[:space:]]|$)" "$LIVE"; then
        echo "DRIFT: live vhost missing ProxyPass ${route}"
        missing=1
    fi
done < <(grep -E "^[[:space:]]*ProxyPass[[:space:]]+/" "$GOOD" | awk '{print $2}' | sort -u)

if [ "$missing" -eq 0 ]; then
    n=$(grep -cE "^[[:space:]]*ProxyPass[[:space:]]+/" "$GOOD")
    echo "OK: all ${n} declared ProxyPass routes present in ${LIVE}"
fi
exit "$missing"
