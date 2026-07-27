# Deployment

How `askmaddi.com` actually deploys on the Hetzner box (phantom-ops-prod, AlmaLinux 9).

> **Authority:** `PRODUCTION-MAP.md` (CURRENT STATE section, verified 2026-05-28) is the
> topology truth. If this file and that one disagree, PRODUCTION-MAP wins — then fix this file.

## Architecture in one sentence

**Apache serves `askmaddi.com` directly out of the repo working tree** at
`DocumentRoot /opt/askmaddi-prod/browser/`, and **the box auto-pulls this repo —
the auto-pull IS the deploy.** Changing a file on master changes the live site
shortly after push. Be deliberate.

## Standard deploy path (static frontend + card content)

```bash
# From sandbox or local clone:
git push origin master        # that's the deploy
```

Apache picks up files on the next request after the box's auto-pull lands. No
service restart for anything under `browser/`. Verify with the sanity checks below.

> Historical note: pre-2026-05-28 posture was pinned-tag checkout
> (`sudo -u askmaddi git -C /opt/askmaddi-prod checkout <tag>`). Tags are still
> useful as release/rollback anchors, but checkout is no longer the deploy step —
> a pinned checkout would now FIGHT the auto-pull. Don't pin without disabling it.

## Gateway deploys (anything under `gateway/`)

The live gateway is **`gateway/app_production.py`** (`app.py` is legacy, NOT served):

```
askmaddi-gateway.service — gunicorn, 2 workers, app_production:app, 127.0.0.1:5001
user: askmaddi (hardened — NOT root)
Apache ProxyPass: /health /instructions /proxy /ping /ebay → :5001
```

Gateway code changes need a restart after the auto-pull lands:

```bash
sudo systemctl restart askmaddi-gateway && systemctl status askmaddi-gateway
```

Secrets live in `gateway/.env` (EBAY_*, PROXY_* Webshare residential) — never
committed, never overwritten by deploys. `gateway/venv/` is gitignored, built per host.

## Card-content deploys (the pipeline → page path)

Canonical card JSONs live in **phantom-ops** (`claude/workspace/aggregator-build/`
— baselines/ for current production cards, extraction on the box). To ship card
changes:

```bash
# 1. (Re)generate or edit the card JSON in phantom-ops (synthesis fills
#    automatically via assemble_card / synthesize_classifier.py)
# 2. Rebuild pages + manifest in THIS repo:
python3 tools/build_site.py --cards-dir <dir-of-card-jsons> --output-dir browser --manifest
# 3. Commit browser/cards/* + browser/cards-manifest.json, push master.
```

`build_site.py` enforces affiliate tags on every CTA (`ensure_affiliate_tag`) and
reads images from `identity.image_thumb` on the card — no `--image-url` needed for
the four live SKUs.

## Sanity checks after a deploy

```bash
curl -sI https://askmaddi.com/                                   # 200
curl -s  https://askmaddi.com/health                             # gateway alive (via proxy)
curl -s  https://askmaddi.com/cards/sigma-35-art-dg-dn-ii/ | grep -c synthesis-text # 1

# --- Affiliate tripwires (rewritten 2026-07-27, Associates reinstated) ---
# The Amazon rung lives on the DETAIL PAGE, not in cards-manifest.json (the
# teaser carries only the Adorama new_url + eBay used_url), so these target
# /cards/<id>/ — the old manifest-scoped greps were verifying nothing.

# 1. Amazon rung present and tagged on a card with a known ASIN (expect 1):
curl -s https://askmaddi.com/cards/sony-a7iv/ | grep -c 'amazon\.com/dp/B09JZT6YK5?tag=askmaddi20-20'

# 2. DEAD-TAG tripwire (expect no output). askmaddi-20 is NOT a substring of
#    askmaddi20-20, so this is a real check rather than a tautology:
curl -s https://askmaddi.com/cards/sony-a7iv/ | grep -Eo 'tag=askmaddi-20([^0-9]|$)'

# 3. UNTAGGED-amazon-link tripwire (expect no output).
#    MUST match the whole href to the closing quote: query separators render as
#    the HTML entity &amp;, so a [^"&]* character class truncates the match
#    BEFORE the tag and reports every correctly-tagged search URL as untagged.
curl -s https://askmaddi.com/cards/sony-a7iv/ \
  | grep -Eo 'href="https://www\.amazon\.com[^"]*"' | grep -v 'tag=askmaddi20-20'

# 4. NO-PRICE INVARIANT — the Amazon button must never carry a number. Without
#    Creators API credentials, displaying Amazon price / star rating / review
#    count violates the Operating Agreement (see build_site.py doctrine block).
#    MUST scope to the anchor TEXT ([^>]*> then strip): a [^<]* class runs past
#    the '>' into the href, whose ASIN and tag contain digits — false positive
#    on every card. Expect NO output; a hit means a price leaked onto the rung.
curl -s https://askmaddi.com/cards/sony-a7iv/ \
  | grep -Eo 'btn-buy-amazon[^>]*>[^<]*' | sed 's/.*>//' | grep -E '\$|[0-9]'

# 5. Absent-list card must have NO Amazon rung at all (expect 0):
curl -s https://askmaddi.com/cards/peak-design-pro-tripod/ | grep -c btn-buy-amazon
# On the box — working tree must be clean or auto-pull will wedge:
sudo -u askmaddi git -C /opt/askmaddi-prod status
```

## Rollback

```bash
git revert <bad-commit> && git push origin master   # rides the auto-pull
```

For emergencies on the box, `tools/deploy.sh` still provides airlocked single-file
deploys (timestamped backup, checksum verify, auto-rollback) — but anything placed
outside git will be visible as a dirty working tree and may conflict with auto-pull.
Prefer revert-and-push.

## What lives elsewhere

- **Topology truth + drift notes**: `PRODUCTION-MAP.md` (this repo).
- **Pipeline, gates, extraction**: `phantom-ops` repo, `claude/workspace/aggregator-build/`.
- **Airlock protocol generic**: `primer/airlock-protocol.md` in `phantom-ops`.
