# askmaddi.com Apache vhost — tracked + renewal-protected

The live Apache vhost that serves askmaddi.com and reverse-proxies the gateway
API. Previously **not in git** and **fragile to certbot renewal** — this dir
fixes both.

## The fragility (why this exists)

`/etc/httpd/conf.d/askmaddi.com-le-ssl.conf` reverse-proxies a set of API paths
to the gateway on `127.0.0.1:5001`. Several routes — **`/search`, `/adorama`,
`/subscribe`** — were **hand-added after certbot last generated the vhost**
(compare `askmaddi.com-le-ssl.conf.bak.20260717-222439`, which lacks them). The
vhost also **borrows ramish.io's certificate** (`SSLCertificateFile ...
live/ramish.io/...`); askmaddi.com is not its own certbot lineage. If a certbot
run regenerates the vhost, the hand-added routes vanish and `/search` +
`/adorama` **404 silently** until someone notices.

## Files

| File | Role |
|------|------|
| `askmaddi.com-le-ssl.conf` | The **known-good** vhost, verbatim. Source of truth for restore. |
| `verify_proxy.sh` | Drift detector: does the live vhost carry every `ProxyPass` route this copy declares? Exit 1 + names any missing. |
| `renewal-hooks/deploy/10-askmaddi-proxy.sh` | certbot **deploy-hook**: after any renewal, run the verify; on drift, restore the tracked vhost and `systemctl reload httpd`. Self-heals within the same renewal. |

Restoring verbatim is safe: the cert path is the stable `live/ramish.io`
symlink, which never changes across renewals.

## Install (box-side, root)

```bash
install -m 0755 /opt/askmaddi-prod/deployment/httpd/renewal-hooks/deploy/10-askmaddi-proxy.sh \
  /etc/letsencrypt/renewal-hooks/deploy/10-askmaddi-proxy.sh
# sanity: no drift right now
bash /opt/askmaddi-prod/deployment/httpd/verify_proxy.sh
```

The hook then runs automatically on every future `certbot renew`. To exercise it
without waiting for a renewal: `certbot renew --force-renewal --cert-name ramish.io`
(reloads httpd), or run the hook directly as root.

## Restore manually (if ever needed)

```bash
cp /opt/askmaddi-prod/deployment/httpd/askmaddi.com-le-ssl.conf \
   /etc/httpd/conf.d/askmaddi.com-le-ssl.conf
httpd -t && systemctl reload httpd
```

## Keeping the tracked copy current

If you intentionally add a new proxied route to the live vhost, update
`askmaddi.com-le-ssl.conf` here to match and commit — otherwise the guard will
"restore" the route away on the next renewal. The tracked copy is authoritative.
