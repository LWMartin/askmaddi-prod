# AskMaddi — Production Map

> **⚠️ CURRENT STATE (verified 2026-05-28) is the section immediately below.**
> Everything under "HISTORICAL SURVEY (2026-04-04)" describes the *old CentOS 7 / cPanel*
> box AskMaddi was migrated away from. It's kept for migration provenance, but its
> port numbers, paths, service names, and "needs deploy" notes are all superseded.

---

## CURRENT STATE — verified 2026-05-28 (post-Hetzner)

**Host:** phantom-ops-prod (Hetzner, AlmaLinux 9). Same box as Ramish API & phantom-ops.
**Domain:** askmaddi.com — Apache (`/etc/httpd/conf.d/`), Let's Encrypt SSL.

### Serving topology
```
Browser ──HTTPS──→ Apache (askmaddi.com-le-ssl.conf)
   │
   ├── DocumentRoot /opt/askmaddi-prod/browser   ← static frontend, served from repo tree
   │       (the auto-pull IS the deploy; no separate docroot copy)
   │
   └── ProxyPass /health /instructions /proxy /ping /ebay  ──→ 127.0.0.1:5001
                                                                   │
                              gunicorn (2 workers, app_production:app)
                              service: askmaddi-gateway.service
                              user: askmaddi   (NOT root — hardening done)
```

### Canonical files (all under `/opt/askmaddi-prod/`, owner `askmaddi`)
| File | Role |
|------|------|
| `gateway/app_production.py` | **THE live gateway** (app:app on :5001). `app.py` is legacy, NOT served. |
| `gateway/ebay_api.py` | eBay Browse API client (OAuth2 client-creds, EPN affiliate attribution) |
| `gateway/headless_fetcher.py` | Headless Chrome path for scrape sites (Amazon). Chrome IS installed. |
| `gateway/.env` | Secrets: EBAY_* creds, PROXY_* (Webshare residential). Never committed. |
| `browser/` | Frontend (DocumentRoot). `app.js`, `fetcher.js`, etc. served directly. |
| `gateway/manifests/` | Site manifests. `ENABLED_SITES = {amazon, ebay}` in app_production.py. |

### Source routing (per-site)
- **eBay** → official Browse API via `/ebay/search` (server-side, structured JSON, EPN-tagged). NOT scraped. Frontend `fetcher.searchEbay()` hits it; `app.js` filters eBay out of the `/proxy` scrape loop.
- **Amazon** → headless scrape via `/proxy` (still fingerprint-blocked; PA-API path pending Amy's-daughter purchases).
- BestBuy/Newegg/Walmart → manifests on disk but not in ENABLED_SITES.

### Deploy mechanism
- Cron (`crontab -u askmaddi`): `*/5 * * * * cd /opt/askmaddi-prod && git pull origin master --quiet 2>&1 | logger -t askmaddi-pull`
- **VERIFIED WORKING 2026-05-28** via rollback test (reset box to prior commit, watched the next tick self-heal HEAD to remote). The push IS the deploy; allow up to 5 min.
- **Monitoring caveat:** `git pull --quiet` prints nothing on success, so `journalctl -t askmaddi-pull` is EMPTY when the cron is healthy. Do NOT read a silent journal as "broken" — diagnose by outcome (`git -C /opt/askmaddi-prod log -1` vs remote), not by logs. To get positive log confirmation, drop `--quiet` or append `&& logger -t askmaddi-pull "pulled $(git rev-parse --short HEAD)"`.

### Ports on this box (don't confuse them)
- **5001** = AskMaddi gateway (`askmaddi-gateway.service`, user `askmaddi`)
- **5000** = Ramish API (`ramish-api.service`, user `ramish`) — unrelated to AskMaddi

### Cross-repo dependency — the publish gate reads phantom-ops
`bot_push`'s validation gate (`python -m pytest tools/ -q`) includes
`tools/test_contamination_bridge.py`, which imports `registry_join_check` from
the **phantom-ops** checkout. Both repos live on this box but **not as
siblings**, so the test's relative candidates cannot find it and the three
cross-repo contract tests skipped silently in production from inception until
2026-07-29.

| Requirement | Value |
|-------------|-------|
| Env var | `ASKMADDI_AGGREGATOR_DIR=/home/phantomops/phantom-ops/claude/workspace/aggregator-build` |
| Set in | `/etc/systemd/system/askmaddi-gateway.service.d/20-aggregator-bridge.conf` |
| Filesystem precondition | `chmod o+x /home/phantomops` (0700 → 0701: traversal only, listing still denied) |
| Verified | 2026-07-29 in a `systemd-run` replica of the service sandbox: **316 passed, 0 skipped** |

Without the traversal bit, `Path.exists()` re-raises EACCES — setting the env
var alone turned 3 skips into 3 **hard failures inside the publish gate**. Since
`a6dd834` the finders treat unreadable as absent and name the permission in the
skip reason, so a reverted `chmod` degrades to a named skip, never a blocked
publish. Verify with `tr '\0' '\n' < /proc/$(systemctl show -p MainPID --value
askmaddi-gateway.service)/environ | grep ASKMADDI` — a shell test proves nothing
about the process that runs the gate.

**Scope limit:** this covers publishes, where `bot_push` is invoked from the
gateway process. Cron-invoked `bot_push` jobs (nightly used-price refresh,
Stage 6 ingestion) run in cron's environment and still skip these three.

**OPEN — interpreter split.** `build_site` and `bot_push` are invoked with
`sys.executable` (the venv, **Python 3.11.13**), but `bot_push`'s gate is
`shell=True` on bare `python`, which resolves off the service PATH
(`/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin`) to `/usr/bin/python`,
**3.9.25**. Every machine commit is validated on an interpreter production does
not run. It passes today, which is why it has stayed invisible. Not yet fixed —
changing it changes what gates every automated commit.

### Affiliate status (verified live)
- **eBay Partner Network:** LIVE. Campaign `5339138080` attaches to every `/ebay/search` URL (`campid=` confirmed in responses). Earning-capable now.
- **Amazon Associates:** approved (`askmaddi-20`), PA-API not yet active (awaiting purchases to unblock).

---

## HISTORICAL SURVEY (2026-04-04) — PRE-HETZNER, SUPERSEDED

**Surveyed:** 2026-04-04 by Claude + Lee (PuTTY/WinSCP session)
**VPS:** server-606198.aisciencecenter.com (CentOS 7, cPanel)
**Domain:** askmaddi.com (Let's Encrypt SSL via `/etc/letsencrypt/live/askmaddi.com`)

> Everything below predates the Hetzner/AlmaLinux migration. Port 5000, root user,
> `api.php` bridge, `app.py`-as-canonical, "Chrome not installed", and "revenue ZERO"
> are all OUT OF DATE. Read the Current State section above for reality.

---

## Architecture Overview

```
Browser (user)
    │
    ├── HTTPS ──→ Apache (cPanel) ──→ /home/askmaddi/public_html/
    │                                   Static files: index.html, js/, css/, images/
    │
    ├── /health, /instructions, /proxy, /ping
    │       │
    │       ├── Route A: Apache ProxyPass → Flask :5000 (direct)
    │       └── Route B: api.php → curl → Flask :5000 (PHP bridge)
    │
    └── Flask/Gunicorn (systemd: askmaddi.service)
            WorkingDirectory: /opt/askmaddi/gateway/
            Port: 5000, 2 workers, runs as root
            Running since: 2026-02-06 (nearly 2 months uptime)
```

**Dual routing:** Both Apache `ProxyPass` and `api.php` forward to Flask.
Likely from debugging — both work, `api.php` is the fallback if Apache
mod_proxy has issues. Can consolidate to ProxyPass-only after migration.

---

## File Locations

### Frontend — `/home/askmaddi/public_html/`

| File | Purpose | In repo? |
|------|---------|----------|
| `index.html` | Main page (5,176B, Feb 3) | YES but different — VPS has logo, affiliate disclosure, Impact meta tags |
| `css/maddi.css` | Stylesheet | YES but VPS has .bak/.bak2/.bak3/.bak4/.final/.final2 copies |
| `js/app.js` | Main app (7,855B) | YES but VPS version may differ (not diffed) |
| `js/fetcher.js` | Gateway client (825B) | YES — functionally identical |
| `js/extractor.js` | DistilBERT extraction (11,806B) | YES |
| `js/centroids.js` | Site-specific centroids (10,606B) | YES |
| `js/deduper.js` | Cross-platform dedup (2,260B) | YES |
| `js/ranker.js` | Quality ranking (1,770B) | YES |
| `js/affiliate.js` | Affiliate link wrapping (5,615B) | YES — but codes still placeholder? |
| `js/ui.js` | UI state management (5,041B) | YES |
| `mission.html` | Mission statement page | **NO** — VPS only |
| `privacy.html` | Privacy policy | **NO** — VPS only |
| `terms.html` | Terms of service | **NO** — VPS only |
| `api.php` | PHP→Flask bridge proxy | **NO** — VPS only |
| `images/logo.png` | Site logo (141KB) | **NO** — VPS only |
| `.htaccess` | PHP handler config | **NO** — VPS only |
| `*.bak*`, `*.final*`, `*.jan27` | Manual iteration backups | **NO** — archaeology |

### Gateway — `/opt/askmaddi/gateway/`

| File | Purpose | In repo? |
|------|---------|----------|
| `app.py` | Flask gateway (74 lines) | YES but DIFFERENT — VPS is simpler, has rate limiting |
| `manifests/bestbuy.json` | Best Buy extraction config | YES |
| `manifests/ebay.json` | eBay extraction config | YES |
| `manifests/newegg.json` | Newegg extraction config | YES |
| `headless_fetcher.py` | **DOES NOT EXIST on VPS** | YES in repo — dead code |

### System Config

| File | Purpose |
|------|---------|
| `/etc/systemd/system/askmaddi.service` | Gunicorn service unit |
| `/etc/apache2/conf.d/userdata/ssl/2_4/askmaddi/askmaddi.com/proxy.conf` | Apache reverse proxy |
| `/etc/letsencrypt/live/askmaddi.com/` | SSL certificates |
| `/opt/cpanel/ea-php82/root/etc/php-fpm.d/askmaddi.com.conf` | PHP-FPM config |

---

## VPS vs Repo Drift

### Gateway app.py — TWO DIFFERENT VERSIONS

**VPS (74L, production):**
- Has `flask-limiter` (30 req/min on `/proxy`)
- NO domain allowlist — open proxy to any URL
- NO headless fetcher import
- NO URL logging (privacy-correct)
- Health endpoint: simple `{status, manifests_loaded}`
- `load_manifests()` called at module level

**Repo (157L, never deployed):**
- NO rate limiting
- HAS domain allowlist via `endswith` (spoofable per security spec)
- HAS headless fetcher integration (Chrome not installed on VPS)
- Logs truncated URLs to stdout
- Health endpoint includes `headless_ready`
- Has `/instructions/<site>` endpoint (VPS doesn't)

**Resolution needed:** Merge the best of both. Rate limiting from VPS +
domain validation (fixed) from repo. Drop headless code until Chrome
is installed post-migration.

### Frontend index.html — VPS IS AHEAD

VPS additions not in repo:
- `<meta name="impact-site-verification" ...>` (two Impact affiliate tags)
- Affiliate disclosure bar: `<div class="affiliate-disclosure-bar">...</div>`
- Logo: `<img src="images/logo.png" ...>`
- Title: "AskMaddi.com - Private Product Search" (repo: "Ask Maddi")

### Files on VPS not in repo at all
- `mission.html` — brand/mission page
- `privacy.html` — privacy policy
- `terms.html` — terms of service
- `api.php` — PHP bridge to Flask
- `images/logo.png` — site logo
- `.htaccess` — PHP handler

---

## Runtime Environment

| Component | Version/Status |
|-----------|---------------|
| OS | CentOS 7 (EOL — migration to AlmaLinux 8 planned) |
| Python | 3.8.18 |
| Flask | via gunicorn (2 workers) |
| Chrome | **NOT INSTALLED** |
| PHP | 8.2 (ea-php82, cPanel managed) |
| Apache | 2.4 (cPanel managed) |
| SSL | Let's Encrypt (auto-renew via cPanel) |
| Process manager | systemd (`askmaddi.service`) |
| cPanel user | `askmaddi` (separate from `lwmpost`) |
| Service runs as | **root** (security issue — see hardening spec) |

---

## systemd Service

```ini
[Unit]
Description=AskMaddi Gateway
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/askmaddi/gateway
ExecStart=/usr/local/bin/gunicorn --bind 0.0.0.0:5000 --workers 2 app:app
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## Apache Proxy Config

```apache
ProxyPreserveHost On
ProxyPass /health http://127.0.0.1:5000/health
ProxyPassReverse /health http://127.0.0.1:5000/health
ProxyPass /instructions http://127.0.0.1:5000/instructions
ProxyPassReverse /instructions http://127.0.0.1:5000/instructions
ProxyPass /proxy http://127.0.0.1:5000/proxy
ProxyPassReverse /proxy http://127.0.0.1:5000/proxy
ProxyPass /ping http://127.0.0.1:5000/ping
ProxyPassReverse /ping http://127.0.0.1:5000/ping
```

---

## Affiliate Status

- **Amazon Associates:** Approved. Tag: `askmaddi-20`. **Wired in repo, needs deploy to VPS.**
- **eBay Partner Network:** Approved. Campaign ID: `5339138080`. **Wired in repo, needs deploy to VPS.**
- **Impact:** Two verification meta tags in index.html — likely for affiliate network onboarding.
- **Best Buy, Newegg, Walmart:** Denied (insufficient traffic).
- **API access:** None from any platform — headless scraping is the only path.
- **Revenue status:** ZERO — no affiliate codes are wired. Approved but not earning.

---

## AlmaLinux 8 Migration — What Must Survive

### Must preserve
1. `/home/askmaddi/public_html/` — entire frontend (including VPS-only files)
2. `/opt/askmaddi/gateway/` — production gateway code (74L version with rate limiting)
3. `/etc/systemd/system/askmaddi.service` — service unit (update User from root)
4. `/etc/apache2/conf.d/userdata/ssl/2_4/askmaddi/askmaddi.com/proxy.conf` — Apache proxy
5. SSL certificates (Let's Encrypt will re-issue on new box)
6. DNS records (`/var/named/askmaddi.com.db`)
7. cPanel user `askmaddi` and its configuration

### Must fix during migration
1. Stop running as root → dedicated `askmaddi` service user
2. Install Chrome/Chromium for headless fetching
3. Merge gateway app.py (rate limiting + domain validation)
4. Sync repo with VPS (pull VPS-only files into repo)
5. Upgrade Python (3.8 → 3.9+ on AlmaLinux 8)
6. Consolidate routing (ProxyPass only, drop api.php)

### Can discard
- All `.bak*`, `.final*`, `.jan27` files (archaeology — snapshot first)
- `api.php` after ProxyPass is confirmed working
- `/backup/` entries (cPanel handles its own backups)

---

## Specs (in phantom-ops repo)

| Spec | Lines | Status |
|------|-------|--------|
| `maddi-product-core-spec` | 1,024 | READY TO BUILD — Amazon catalog encoding |
| `maddi-security-hardening` | 1,087 | READY TO BUILD — 10 issues identified |
| `maddi-distribution-engine` | 782 | READY TO BUILD — automated daily pipeline |

---

## Next Actions

1. **Deploy affiliate codes to VPS** — Amazon (askmaddi-20) + eBay (5339138080) wired in repo, need to update VPS `affiliate.js`. Fastest path to revenue.
2. **Sync repo with VPS** — pull mission.html, privacy.html, terms.html, api.php, logo.png, live index.html, live app.py into repo
3. **AlmaLinux 8 migration** — use this map as the preservation checklist
4. **Install Chrome on new box** — unblocks headless scraping (Amazon)
5. **Merge gateway versions** — best of VPS (rate limiting) + repo (validation)
6. **Security hardening** — run through the 10-item checklist post-migration
7. **Add Amazon manifest** — gateway has bestbuy/ebay/newegg but no amazon.json
