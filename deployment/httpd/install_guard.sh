#!/bin/bash
# Install the askmaddi Apache renewal guard (run as root, box-side).
# Short invocation for the classifier-gated /etc write:
#   bash /opt/askmaddi-prod/deployment/httpd/install_guard.sh
set -e
SRC=/opt/askmaddi-prod/deployment/httpd
DST=/etc/letsencrypt/renewal-hooks/deploy/10-askmaddi-proxy.sh

install -m 0755 "$SRC/renewal-hooks/deploy/10-askmaddi-proxy.sh" "$DST"
echo "installed deploy-hook -> $DST"

echo "current drift check:"
bash "$SRC/verify_proxy.sh"
echo "guard active — it will re-assert /search + /adorama on every certbot renewal."
