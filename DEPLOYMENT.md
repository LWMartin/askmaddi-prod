# Deployment

How `askmaddi.com` actually deploys on the Hetzner box.

## Architecture in one sentence

**Apache serves `askmaddi.com` directly out of the repo working tree** at `DocumentRoot /opt/askmaddi-prod/browser/`. There is no separate `public_html/` docroot; the repo *is* the docroot.

This means *changing a file in the repo on the live box is changing the live site*. Be deliberate.

## Standard deploy path

The repo is checked out as the `askmaddi` service user (uid 1003), pinned at a tag (`v0.1.1-pre-migration` at time of writing). To roll a new version live:

```bash
# As root, on phantom-ops-prod:
sudo -u askmaddi git -C /opt/askmaddi-prod fetch --tags
sudo -u askmaddi git -C /opt/askmaddi-prod checkout <new-tag>
```

That's the whole deploy. Apache picks up the new files on the next request — no service restart needed for the static frontend. If you've changed gateway code (Python service), restart the gateway systemd unit separately.

### Why pinned-tag rather than tracking `master`

Tag pinning is the production posture: the live site corresponds to a deliberately-cut release, not whatever happened to land on `master` in the last hour. Cut a tag when you mean to ship; check it out when you mean to deploy.

### Cutting a new tag

```bash
# In your local clone (Windows / sandbox), against an up-to-date master:
git tag -a v0.1.2-pre-migration -m "<one-line release summary>"
git push origin v0.1.2-pre-migration
```

Then run the standard deploy path above with the new tag name.

## When to use `tools/deploy.sh` instead

The `tools/deploy.sh` helper is for *one-off file deploys* where you want airlock guarantees (timestamped backup, checksum verify, auto-rollback on mismatch) without cutting a full release tag. Examples:

- Staging a `robots.txt` update without bumping the release
- Dropping a single SSL cert or DNS verification file
- A hot-fix where you don't want the change to ride a tag (because the tag isn't ready for everything else on `master`)

It is **not** the standard path. For anything that should be part of a release, cut a tag and use `git checkout`.

## File-system layout

```
/opt/askmaddi-prod/                  ← repo root, owned by askmaddi:askmaddi
├── browser/                         ← Apache DocumentRoot for askmaddi.com
│   ├── index.html
│   ├── js/affiliate.js              ← affiliate codes (Amazon askmaddi-20, eBay 5339138080)
│   └── ...
├── gateway/                         ← Python Flask service (separate systemd unit)
│   ├── app.py
│   ├── app_production.py
│   ├── venv/                        ← gitignored, built per host
│   └── ...
├── pipeline/                        ← content generation pipeline
├── tools/                           ← ops scripts (this script, etc.)
└── PRODUCTION-MAP.md                ← detailed production state notes
```

The `gateway/venv/` directory exists on the box but is not tracked by git (per `.gitignore`). It is built fresh per host by the Phase 5 deploy procedure.

## Sanity checks after a deploy

```bash
# Confirm Apache is serving the expected build
curl -sI https://askmaddi.com/                  # expect 200, AlmaLinux Apache signature
curl -s  https://askmaddi.com/js/affiliate.js | grep -E "askmaddi-20|5339138080"

# Confirm gateway service is healthy (if gateway changed)
systemctl status askmaddi-gateway
curl -s  https://askmaddi.com/api/health        # adjust path per gateway routes

# Confirm working tree is clean (no surprise modifications)
sudo -u askmaddi git -C /opt/askmaddi-prod status
```

## Rollback

To roll back to an earlier tag:

```bash
sudo -u askmaddi git -C /opt/askmaddi-prod checkout <prior-tag>
```

For a file-level rollback after a `tools/deploy.sh` run, the script prints the explicit `cp` rollback command at the end of its output. Save that line.

## What lives elsewhere

- **Pre-migration architecture and drift notes**: `PRODUCTION-MAP.md` (in this repo, root level). Read this before changing infrastructure.
- **Phantom Ops migration phases**: `claude/workspace/migration/P5-hetzner-services-deck.md` in the `phantom-ops` repo. This is where Phase 5 (services, SELinux, vhosts) is staged.
- **Airlock protocol generic**: `primer/airlock-protocol.md` in the `phantom-ops` repo. The pattern `tools/deploy.sh` implements is steps 4–6.
