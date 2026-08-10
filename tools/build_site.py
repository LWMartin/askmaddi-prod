#!/usr/bin/env python3
"""
build_site.py — AskMaddi static card-detail page generator (Phase 2 + Phase 4).

Reads full card JSON (the pipeline's card.vN.json shape) and emits a static,
crawlable detail page at  browser/cards/{card_id}/index.html  plus refreshes
browser/cards-manifest.json (the teaser-grid summary the homepage consumes).

Design: pure static HTML + the existing browser/css/maddi.css design system.
No client-side JSON loading — fast, shareable, SEO-friendly. Card detail uses
a small dedicated stylesheet (cards-detail.css) that extends, not replaces, the
shared tokens in maddi.css.

Usage:
    python tools/build_site.py --cards-dir data/cards/ --output-dir browser/
    python tools/build_site.py --card path/to/card.v4.json --output-dir browser/

Real-data discipline (the v1 photography corpus is honest about its gaps):
  - Missing pricing (0.0 / None)  -> render a "Check current price" CTA, never "$0".
  - confidence == "low"           -> show an honest confidence badge, don't hide it.
  - issue_clusters empty          -> omit the section rather than show an empty box.
  - We render exactly what the card says; we never invent data to fill the template.
"""

import argparse
import html
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path


# ─── Affiliate tags (match the live frontend / cards-manifest) ──────────────
# 2026-07-27: Associates REINSTATED after the no-traffic suspension; Lee
# reapplied at Amazon's direction. New tracking id is askmaddi20-20 (the old
# askmaddi-20 is dead and must never reappear — note it is NOT a substring of
# the new one, so any tripwire grepping the old string is silently useless).
AMAZON_TAG = "askmaddi20-20"
EBAY_CAMPID = "5339138080"
EBAY_RID = "711-53200-19255-0"

# ─── Amazon display doctrine while we have NO catalog API ───────────────────
# PA-API 5.0 is retired (May 2026) and closed to new registrations; the
# successor is the Creators API, whose eligibility floor is 10 qualifying sales
# in the TRAILING 30 DAYS — a recurring bar, not a one-time unlock. Separately,
# 3 qualifying sales in the first 180 days is what keeps the ASSOCIATES ACCOUNT
# alive; that is the clock that expired on us the first time. Until we hold API
# credentials the Associates agreement permits exactly one thing: a tagged link
# to a product detail page. It forbids displaying Amazon price, availability,
# star ratings, review counts, and Amazon-hosted imagery sourced any other way.
#
# THE INVARIANT: the Amazon rung is a LINK, never a price. amazon_cta() returns
# a fixed "See price on Amazon" label with no numeric ever interpolated. If you
# are about to put a number on the Amazon button, you are about to lose the
# account for the second time. The priced 'new' rung is Adorama, whose feed we
# are licensed to display — that asymmetry is deliberate, not an oversight.
#
# ASINs are exempt from the caching ban (storable indefinitely FOR LINKING),
# which is why data/asin_registry.json remains lawful and useful right now.

# ─── Adorama (Partnerize / prf.hn) — the priced 'buy new' affiliate network ──
# Partnerize 'tagging' is a URL WRAP (a deep-link that carries a destination
# adorama.com URL), NOT a query param like amazon/ebay — see partnerize_wrap().
PARTNERIZE_CAMREF = "1101l5Pw9q"
PARTNERIZE_CLICK_BASE = f"https://adorama.prf.hn/click/camref:{PARTNERIZE_CAMREF}"
PARTNERIZE_SHORT = "https://prf.hn/l/PlDklJx/"  # generic homepage fallback

# ─── Site identity (canonical URLs, OG, sitemap) ────────────────────────────
BASE_URL = "https://askmaddi.com"
SITE_NAME = "AskMaddi"


def abs_url(path_or_url):
    """Absolute URL for OG/JSON-LD/sitemap. Pass-through if already absolute."""
    if not path_or_url:
        return ""
    if path_or_url.startswith(("http://", "https://")):
        return path_or_url
    return BASE_URL + ("" if path_or_url.startswith("/") else "/") + path_or_url


def fmt_date_human(iso_str):
    """ISO timestamp -> 'Jun 10, 2026'. Empty string on anything unparseable."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
        return f"{dt:%b} {dt.day}, {dt.year}"
    except (ValueError, TypeError):
        return ""


def days_since(iso_str):
    """Whole days between an ISO timestamp and now, or None if unparseable.

    Computed at RENDER, never read from the card. freshness.staleness_days is
    stored at build time and frozen — it reads 0 on a card five weeks old,
    because it was 0 the moment it was written. A derived value with a clock in
    it must not be persisted. (That field has no readers anywhere in either
    repo; it is scheduled for removal from the factory writer.)
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(str(iso_str).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - dt).days)


def synthesis_asof(card):
    """(date, days_ago) for the REVIEW SYNTHESIS — a different clock from price.

    The two must never be collapsed into one 'Last updated' stamp. Prices
    refresh nightly and cheaply; synthesis requires re-extracting every source
    and re-running the models, which does not scale nightly once the catalog
    reaches hundreds of cards. Showing one date implies the expensive clock
    ticks as fast as the cheap one — which would be advertising staleness to a
    recency-weighting engine, and would simply be untrue.
    """
    fresh = card.get("freshness", {}) or {}
    raw = fresh.get("last_built") or ""
    return fmt_date_human(raw), days_since(raw)


def used_price_asof(card):
    """ISO date the used band was fetched, or '' (honest absence, no fallback
    to build time — a build is not a price observation)."""
    pricing = card.get("pricing", {}) or {}
    used = pricing.get("used_market", {}) or {}
    return used.get("price_updated_at") or ""


def _iso_date(raw):
    """Date-grain (YYYY-MM-DD) from an ISO timestamp, or '' if unparseable."""
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        return ""


def schema_published_date(card):
    """schema.org datePublished — the card's first-build date (freshness.
    created_at). Stable across rebuilds: when the page first entered the web."""
    return _iso_date((card.get("freshness", {}) or {}).get("created_at"))


def schema_modified_date(card):
    """schema.org dateModified — a PAGE-LEVEL 'content last changed' signal for
    recency-weighting answer engines (Lee's ruling 2026-08-10: nightly build).

    Anchored to real data timestamps — max(last_built, used-price refresh) —
    never build-time now(): the nightly rebuild is change-gated ('an unchanged
    price is not an event'), so these advance only on an actual change. That
    keeps this stamp honest AND rides the nightly price tick for priced cards,
    while unpriced cards truthfully show their real last-change date.

    Distinct from the two VISIBLE clocks (synthesis_asof / used_price_asof),
    which stay deliberately separate on the page — this is machine-readable
    page freshness, not a synthesis claim, so collapsing them here is correct."""
    fresh = card.get("freshness", {}) or {}
    used = (card.get("pricing", {}) or {}).get("used_market", {}) or {}
    dates = [d for d in (_iso_date(fresh.get("last_built")),
                         _iso_date(used.get("price_updated_at"))) if d]
    if not dates:
        # No build/price timestamp at all — fall back to first-build so the
        # field is still present and never post-dates publication.
        return schema_published_date(card)
    return max(dates)


def ensure_affiliate_tag(url):
    """Guarantee the affiliate tag on any amazon/ebay URL, idempotently.

    Cards built on-demand carry raw product URLs in pricing.current_new_url
    (cron populates affiliate_url later, or never for fresh builds). Every
    CTA URL must pass through here so no untagged link reaches the page.
    Sets params via urllib so existing tags are overwritten, never doubled.
    """
    if not url:
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = parts.netloc.lower()
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Amazon tagging RESTORED 2026-07-27 (Associates reinstated). Tagging a
    # link is always permitted; it is DISPLAYING Amazon data that is API-gated,
    # and that restriction is enforced in amazon_cta(), not here. eBay is
    # unchanged; Adorama tagging is a URL wrap (partnerize_wrap), not a param.
    if "amazon." in host:
        q["tag"] = AMAZON_TAG
    elif "ebay." in host:
        q["campid"] = EBAY_CAMPID
        q.setdefault("mkcid", "1")
        q.setdefault("mkrid", EBAY_RID)
    else:
        return url
    return urlunsplit(parts._replace(query=urlencode(q)))

# Source-type display grouping for the Sources section.
SOURCE_TYPE_LABELS = {
    "lab": ("\U0001F52C", "Lab & Measurement"),       # microscope
    "pro": ("\U0001F4F9", "Pro Reviews"),             # video camera
    "youtube": ("\U0001F4F9", "YouTube"),
    "blog": ("\U0001F4F0", "Blogs & Publications"),   # newspaper
    "editorial": ("\U0001F4F0", "Editorial"),
    "reddit": ("\U0001F4AC", "Reddit & Forums"),      # speech balloon
    "forum": ("\U0001F4AC", "Forums"),
    "manufacturer": ("\U0001F3ED", "Manufacturer"),   # factory
}
SOURCE_TYPE_FALLBACK = ("\U0001F517", "Other Sources")  # link


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def _reviewer_from_source_id(source_id):
    """Derive a readable reviewer name from a source_id slug.

    e.g. 'youtube-tony-chelsea-northrup-sigma-35-mm-...' -> 'Tony Chelsea Northrup'.
    Strips the leading platform token and any trailing product/year tokens by
    keeping only the name-ish prefix (up to the first product keyword).
    Best-effort: the goal is a tidy attribution, not perfect parsing.
    """
    if not source_id:
        return "source"
    parts = source_id.split("-")
    # drop leading platform token (youtube/blog/reddit/lab/etc.)
    if parts and parts[0] in {"youtube", "blog", "reddit", "lab", "forum", "editorial", "pro", "manufacturer", "yt"}:
        parts = parts[1:]
    # keep tokens until we hit something that looks like product/spec noise
    name_tokens = []
    for tok in parts:
        if tok.isdigit() or re.match(r"^\d", tok) or tok in {"mm", "f", "sigma", "sony", "canon", "nikon", "vs", "review", "lens"}:
            break
        name_tokens.append(tok)
    if not name_tokens:
        name_tokens = parts[:3]
    return " ".join(t.capitalize() for t in name_tokens) or "source"


def pct(pos, total):
    """Single share, rounded independently. Retained for callers that need ONE
    number against an arbitrary denominator. NOT for rendering a pos/neu/neg
    split — use pct_triple, which is the only thing that keeps three shares
    consistent with each other."""
    return round(100 * pos / total) if total else 0


def pct_triple(pos, neu, neg):
    """Integer pos/neu/neg percentages summing to EXACTLY 100.

    Rounding each share independently sums to 99 or 101 whenever two shares
    carry fractional parts above .5 — live on 2 of 11 cards when this landed
    (sigma-35 optical performance printed 71/14/16; sony-a7c mount
    compatibility printed 50/48/3). The stat line is the sentence built to be
    lifted whole by an extractor, so shares that do not total 100 discredit
    exactly the surface that was supposed to be checkable.

    Largest-remainder (Hamilton) apportionment: floor every share, hand the
    leftover point(s) to the largest fractional parts. The index tie-break
    keeps output byte-stable for a given input, which the determinism
    invariant depends on.

    PORTED, NOT INVENTED — this is `_dist_pcts` from the aggregator's
    synthesize_classifier.py, which has rendered the paragraph's shares
    correctly since 2026-07-24. The two live in separate repos and cannot
    import each other, so equivalence is pinned by an exhaustive-grid test
    (test_build_site_pct_triple.py) in the same way select_teaser_axes is
    pinned against axis_roles. If you change one, the test fails until you
    change the other.

    Denominator is the polar total (pos+neu+neg), not sentiment.total, so a
    card whose total field disagrees with its own counts cannot bend the
    shares."""
    denom = (pos or 0) + (neu or 0) + (neg or 0)
    if denom <= 0:
        return (0, 0, 0)
    raw = [100 * (pos or 0) / denom, 100 * (neu or 0) / denom,
           100 * (neg or 0) / denom]
    floors = [int(x) for x in raw]
    order = sorted(range(3), key=lambda i: (raw[i] - floors[i], -i),
                   reverse=True)
    for i in range(100 - sum(floors)):
        floors[order[i]] += 1
    return (floors[0], floors[1], floors[2])


def card_name(card):
    """The product's display name — question-form headings need it in helper
    sections that only receive the card."""
    return (card.get("identity", {}) or {}).get("display_name") or ""


def specs_heading(card):
    """Question-form specs heading, degrading to the unnamed form.

    Two branches rather than string interpolation with a possibly-empty name:
    the naive version renders "What are the  specifications?" with a doubled
    space on any card lacking a display name. A heading is the most-read line
    in its section — it does not get to be almost right."""
    name = card_name(card)
    return (f"What are the {name} specifications?" if name
            else "What are the full specifications?")


def used_price_heading(card):
    """Question-form used-market heading, degrading to the unnamed form."""
    name = card_name(card)
    return (f"What does a used {name} cost?" if name
            else "What does this cost on the used market?")


def sentiment_triple(sent):
    """All three shares, or nothing. Never one alone.

    A single share is not a fact about an axis. Before 2026-07-27 the A7 IV
    card led with "524 claims, 32% positive" on video capability — an axis that
    is 62% NEGATIVE. The number was accurate and the sentence was misleading,
    because a lone positive share reads as mild approval and there is no
    denominator visible to correct it. Worse, that sentence is also the meta
    description and the schema.org description, so the ungrounded figure was
    reaching three surfaces at once.

    Pos/neu/neg together are the only grounded form. The neutral share is not
    filler either: a high neutral rate means the coverage is descriptive rather
    than evaluative, which changes how much weight the other two deserve.

    Returns "" when there is nothing to divide by — no claims means no shares,
    and inventing 0%/0%/0% would assert a measurement that was never made."""
    pos = sent.get("pos", 0) or 0
    neu = sent.get("neu", 0) or 0
    neg = sent.get("neg", 0) or 0
    total = sent.get("total") or (pos + neu + neg)
    if not total:
        return ""
    p, n, g = pct_triple(pos, neu, neg)
    return f"{p}% positive, {n}% neutral, {g}% negative"


def most_discussed_axis(card):
    """The axis carrying the most claims — the one the answer block leads on.

    Ranked by claim VOLUME, not by sentiment. What reviewers spend their words
    on is the honest headline; picking the most favourable axis instead would
    be the editorial thumb on the scale the whole synthesis-not-opinion posture
    exists to avoid."""
    best, best_total = None, 0
    for axis in (card.get("lead_axes") or []) + (card.get("detail_axes") or []):
        sent = axis.get("sentiment") or {}
        total = sent.get("total") or (
            (sent.get("pos", 0) or 0) + (sent.get("neu", 0) or 0)
            + (sent.get("neg", 0) or 0))
        if total > best_total:
            best, best_total = axis, total
    return best


def asof_phrase(card):
    """"As of June 2026" — OUR observation moment, never the sources'.

    The distinction is load-bearing. A heading like "What do reviewers say
    about the A7 IV in 2026?" implies the reviews are from 2026. They are not:
    sources[].publication_date is present on every source and EMPTY on every
    source (55/55 on the A7 IV card), so we hold no publication dates at all.
    The only date we own is ingested_at — when we fetched — and the years
    inferable from source-id slugs run 2021, 2022, 2025 and 2026. The corpus is
    five years wide, so a bare year attached to "what reviewers say" asserts a
    recency the data contradicts.

    Attaching the date to the ANALYSIS instead claims only what we can defend:
    this is the state of the evidence as we read it, on this date. Month grain,
    not day: a synthesis drawn from a multi-week corpus does not earn
    day-level precision, and the exact build date is already on the hero line
    for anyone who wants it.

    Returns "" when there is no synthesis date — no date claim at all beats a
    manufactured one."""
    date, _days = synthesis_asof(card)
    if not date:
        return ""
    try:
        parsed = datetime.strptime(date, "%b %d, %Y")
    except ValueError:
        return ""
    return f"As of {parsed.strftime('%B %Y')}"


def analysis_year(card):
    """The year OUR analysis was built, or None. Not the sources' year."""
    date, _days = synthesis_asof(card)
    try:
        return datetime.strptime(date, "%b %d, %Y").year if date else None
    except ValueError:
        return None


def answer_stat_line(card, source_count):
    """The extractable stat sentence: countable, denominated, dated.

    Literal text near the top of the page, in the plainest possible form, so an
    extractor can lift it whole and a reader can check it. Every number carries
    what it is a share OF."""
    axis = most_discussed_axis(card)
    if axis is None:
        return ""
    sent = axis.get("sentiment") or {}
    total = sent.get("total") or (
        (sent.get("pos", 0) or 0) + (sent.get("neu", 0) or 0)
        + (sent.get("neg", 0) or 0))
    triple = sentiment_triple(sent)
    if not triple:
        return ""
    name = axis.get("display_name") or axis.get("axis_id", "")
    # Two scope qualifiers, doing different jobs:
    #
    #   "among the N reviews we compiled" bounds COVERAGE. We are not
    #   exhaustive and cannot be — this is the slice we assembled, not the
    #   review landscape. "N claims across N sources" was exact and still
    #   overclaimed, because a bare denominator reads as the whole population.
    #
    #   "which we read as" attributes CLASSIFICATION. The counts are exact
    #   within the corpus, but the pos/neu/neg split is a model's reading of
    #   each claim, not a fact the reviewers stated. Owning that is what makes
    #   the number trustworthy rather than merely confident.
    #
    # Both are scope, not hedging: every figure stays concrete and extractable.
    # Softening the numbers themselves would cost the citation value AND the
    # honesty, since the counts are not the uncertain part.
    # On-axis coverage, inherited from the retired paragraph S1 (2026-07-28).
    # The stat line now OWNS the anchor claim outright, so the one datum S1
    # carried that this sentence lacked has to come with it or it is lost:
    # how many of the compiled reviews actually touched this axis. Without it
    # the compiled-review count silently doubles as the on-axis count, which
    # was wrong on all 11 live cards — by 1 to 8 sources.
    #
    # Emitted only when it is BOTH known and smaller than the corpus. Equal
    # counts would render "341 claims from 55 of them" beside "the 55 reviews
    # we compiled", which is noise; a missing count is not written as a guess.
    covered = (axis.get("convergence") or {}).get("source_count")
    on_axis = (f" from {covered} of them"
               if covered and source_count and covered < source_count else "")
    body = (f"among the {source_count} reviews we compiled, {name.lower()} "
            f"drew the most discussion: {total} claims{on_axis}, "
            f"which we read as {triple}.")
    # The date qualifier LEADS. An extractor lifting the first clause gets the
    # scope of the claim with it, rather than a bare statistic that reads as
    # timeless.
    asof = asof_phrase(card)
    return f"{asof}, {body}" if asof else f"{body[0].upper()}{body[1:]}"


META_DESC_LIMIT = 155


def meta_description(card, source_count, name, synth, limit=META_DESC_LIMIT):
    """The <meta description> / og / twitter / schema.org description.

    COMPOSED, never sliced. This was `synth[:155] + "…"`, which cut mid-word on
    all 11 live cards — every one ended "…Reviewers are most posi…". A blind
    slice also inherited whatever the paragraph happened to open with, so the
    single most-syndicated sentence on the site was decided by sentence order
    in a different repo.

    Three tiers, each a WHOLE claim:

      1. A compact form of the anchor stat, built from the same numbers as the
         stat line. Names the product (the stat line does not — it sits under
         an <h1> that already does, but a description travels alone into a
         SERP), and carries the on-axis denominator. The stat line itself
         cannot be reused verbatim: it runs 172-183 chars against a 155 slot.
      2. Whole sentences from the synthesis paragraph, packed greedily and cut
         only at a sentence boundary. Never a partial claim.
      3. The generic line, which asserts only what is always true.

    A description that overruns is truncated by the search engine, so the
    limit is enforced here where we can choose WHERE it ends."""
    axis = most_discussed_axis(card)
    if axis is not None and name:
        sent = axis.get("sentiment") or {}
        total = sent.get("total") or (
            (sent.get("pos", 0) or 0) + (sent.get("neu", 0) or 0)
            + (sent.get("neg", 0) or 0))
        triple = sentiment_triple(sent)
        label = (axis.get("display_name") or axis.get("axis_id", "")).lower()
        covered = (axis.get("convergence") or {}).get("source_count")
        scope = (f"{covered} of {source_count} reviews"
                 if covered and source_count and covered < source_count
                 else f"{source_count} reviews")
        if triple and total and label:
            candidate = (f"{name}: {total} claims on {label} across {scope} "
                         f"we compiled \u2014 {triple}.")
            if len(candidate) <= limit:
                return candidate

    # Tier 2 — whole sentences only. Split on the sentence boundary the
    # templates actually emit ('. '), which leaves intra-token periods such as
    # 'af performance.video' intact.
    if synth:
        packed = ""
        for part in synth.split(". "):
            piece = part if part.endswith(".") else part + "."
            nxt = piece if not packed else f"{packed} {piece}"
            if len(nxt) > limit:
                break
            packed = nxt
        if packed:
            return packed

    return f"{name} \u2014 reviews synthesized from {source_count} sources."


# ─── Pricing helpers (degrade gracefully on missing data) ───────────────────
def adorama_search_url(display_name):
    """Adorama on-site search for a product name — the Adorama analogue of
    ebay_search_url. Lands the buyer on Adorama results scoped to this product;
    partnerize_wrap() then makes it an affiliate link. Swap for an exact product
    URL (pricing.adorama_url) once the Partnerize feed / direct API supplies one."""
    q = urllib.parse.quote_plus(display_name.strip())
    return f"https://www.adorama.com/l/?searchinfo={q}"


def partnerize_wrap(destination_url):
    """Wrap an adorama.com destination in the Partnerize camref deep-link — the
    affiliate 'tag' for Adorama is a URL WRAP, not a query param. Idempotent: an
    already-wrapped prf.hn link returns unchanged; a NON-adorama destination is
    left untouched (never silently mis-attributed to Adorama). Empty → the
    generic homepage short link, so a CTA is never dead."""
    if not destination_url:
        return PARTNERIZE_SHORT
    try:
        host = urllib.parse.urlsplit(destination_url).netloc.lower()
    except ValueError:
        return destination_url
    if "prf.hn" in host:            # already a Partnerize link
        return destination_url
    if "adorama." not in host:      # only wrap Adorama destinations
        return destination_url
    return f"{PARTNERIZE_CLICK_BASE}/destination:{destination_url}"


def ebay_search_url(display_name, category_id=None):
    """Tagged eBay website-search CTA, scoped to `display_name`.

    For RANGE cards (e.g. Peak Design Pro = Lite/Pro/Tall) there is no single
    canonical product page, so a search that shows all variants is the honest
    target — but a bare keyword search drags in accessories (the live Browse
    probe returned a leveling base, a quick-release plate, and a backpack under
    "Peak Design Pro Tripod"). Pinning eBay's category via `_sacat` is the
    durable fix: it leans on eBay's own taxonomy instead of brittle hand-tuned
    `-plate -base` keyword exclusions that silently over-filter and rot. Camera
    Tripods & Monopods = 30093. Affiliate params stamped by ensure_affiliate_tag.
    """
    q = re.sub(r"\s+", "+", display_name.strip().lower())
    sacat = f"&_sacat={category_id}" if category_id else ""
    return (
        f"https://www.ebay.com/sch/i.html?_nkw={q}{sacat}"
        f"&mkcid=1&mkrid={EBAY_RID}&campid={EBAY_CAMPID}"
    )


# eBay leaf category ids for CTA scoping (eBay's own taxonomy, stable).
EBAY_CATEGORY = {
    "support": "30093",   # Camera Tripods & Monopods
}


def ebay_product_url(epid):
    """Direct eBay catalog (EPID) product page — lands the buyer on the catalog
    product page instead of search results. Affiliate params are stamped by
    ensure_affiliate_tag (campid/mkcid/mkrid), same as every other eBay CTA.
    EPID is eBay's durable catalog id; a single listing can vanish but the
    catalog entry persists, so this is the right rung to store in the registry.
    """
    return f"https://www.ebay.com/p/{epid}"


def amazon_product_url(asin):
    """Canonical Amazon detail-page URL for an ASIN. Affiliate params are
    stamped by ensure_affiliate_tag (tag=), same as every other CTA. ASINs are
    the one piece of Amazon data we may store indefinitely, and only for this
    purpose — linking. Never a search URL when an ASIN is known: Amazon strips
    the tag on an in-site click-through from results to a product page."""
    return f"https://www.amazon.com/dp/{asin}"


def amazon_search_url(display_name):
    """Product-scoped Amazon search — the no-ASIN fallback rung. Weaker than a
    /dp/ deep link (see amazon_product_url) but still tagged and still lawful."""
    q = urllib.parse.quote_plus(display_name.strip())
    return f"https://www.amazon.com/s?k={q}"


def amazon_cta(card):
    """Return (label, url) for the Amazon rung, or None when there is no rung.

    Returns None in exactly two cases:
      1. pricing.amazon_absent — the card is VERIFIED not sold on Amazon
         (data/asin_registry.json 'absent' list). Linking a close-match page
         for an absent product is the e930bea wrong-product trap; skip it.
      2. No display_name to scope a search with.

    The label is a CONSTANT. It carries no price, no star rating, no review
    count — see the display-doctrine block at the top of this module. Amazon
    permits the link and forbids the data until we hold Creators API
    credentials, so the honest rendering is an invitation to go look.
    """
    pricing = card.get("pricing", {}) or {}
    if pricing.get("amazon_absent"):
        return None
    name = (card.get("identity", {}) or {}).get("display_name") or ""
    asin = pricing.get("amazon_asin")
    if asin:
        url = amazon_product_url(asin)
    elif name.strip():
        url = amazon_search_url(name)
    else:
        return None
    return "See price on Amazon", ensure_affiliate_tag(url)


def new_cta(card):
    """Return (label, url) for the 'buy new' CTA → Adorama (Partnerize).

    Adorama stays the PRICED new-gear rung and this function stays Amazon-free
    by design. We are licensed to display Adorama's feed pricing; we are not
    licensed to display Amazon's without Creators API credentials. The Amazon
    rung is therefore a separate, deliberately price-free button — amazon_cta()
    — rather than a second destination inside this ladder. Do not merge them.

    Destination preference:
      pre-wrapped prf.hn affiliate_url (feed/registry) > exact Adorama product
      URL (pricing.adorama_url, populated by the Partnerize feed later) >
      a product-scoped Adorama SEARCH.
    All are Partnerize-wrapped (partnerize_wrap: a deep-link, not a query param),
    so every new-CTA click is tracked. eBay remains the USED domain (used_cta);
    Amazon ASIN data may still sit in the card but is never linked.
    """
    pricing = card.get("pricing", {})
    name = card["identity"]["display_name"]
    explicit = pricing.get("affiliate_url")
    if explicit and "prf.hn" in explicit:
        url = explicit  # already an affiliate link (feed/registry) — honour as-is
    else:
        dest = pricing.get("adorama_url") or adorama_search_url(name)
        url = partnerize_wrap(dest)
    price = pricing.get("current_new_usd") or pricing.get("msrp_usd") or 0
    label = f"${int(price)} new" if price and price > 0 else "Check price at Adorama"
    return label, url


def used_cta(card):
    """Return (label, url) for the 'used' CTA, honest about missing price."""
    pricing = card.get("pricing", {})
    used = pricing.get("used_market", {}) or {}
    name = card["identity"]["display_name"]
    ebay_cat = EBAY_CATEGORY.get(card.get("category"))
    url = ensure_affiliate_tag(
        used.get("affiliate_url") or used.get("search_url") or ebay_search_url(name, ebay_cat)
    )
    # Prefer a representative band midpoint if present; else just label "used".
    bands = used.get("bands", {}) or {}
    band_prices = [v for v in bands.values() if isinstance(v, (int, float)) and v > 0]
    if band_prices:
        label = f"from ${int(min(band_prices))} used"
    else:
        label = "See used"
    return label, url


# ─── HTML fragment builders ─────────────────────────────────────────────────
def confidence_badge(level):
    level = (level or "unknown").lower()
    cls = {
        "high": "conf-high",
        "medium": "conf-med",
        "med": "conf-med",
        "low": "conf-low",
    }.get(level, "conf-unknown")
    return f'<span class="conf-badge {cls}">{esc(level)} confidence</span>'


def axis_block(axis):
    """One axis: label + confidence + sentiment bar + counts + top quote."""
    name = axis.get("display_name") or axis.get("axis_id", "")
    sent = axis.get("sentiment", {}) or {}
    pos, neu, neg = sent.get("pos", 0), sent.get("neu", 0), sent.get("neg", 0)
    total = sent.get("total", pos + neu + neg)
    # Bar geometry comes from the SAME apportionment the sentence text uses.
    # Independently rounded widths let the bar contradict the words beside it:
    # sigma-35 rendered pos:71% + neg:16%, leaving a 13% neutral gap under a
    # sentence that said 14% neutral. Neutral is the implicit remainder here,
    # so the three widths total 100 by construction and the bar cannot overflow.
    p, _n_neu, n = pct_triple(pos, neu, neg)

    # Card-face blurb: the pipeline stamps a gated `face_quote` per axis
    # (unambiguous-subset doctrine, 2026-06-10) with a display-ready
    # excerpt and a joined review URL. The renderer shows it or shows
    # NOTHING — it never scans raw evidence refs for display text, so an
    # axis without a gate-passing quote suppresses its blurb and the
    # sentiment bar carries the axis alone. When a URL is present the
    # whole blurb is the link (like what you hear -> click through).
    quote_html = ""
    fq = axis.get("face_quote") or {}
    q = str(fq.get("quote_excerpt") or "").strip()
    if q:
        reviewer = fq.get("reviewer") or _reviewer_from_source_id(fq.get("source_id", ""))
        url = fq.get("url", "")
        cite = f'<span class="quote-cite">\u2014 {esc(reviewer)}</span>'
        if url:
            quote_html = (
                f'<blockquote class="axis-quote axis-quote-linked">'
                f'<a class="quote-link" href="{esc(url)}" target="_blank" '
                f'rel="nofollow noopener">\u201c{esc(q)}\u201d {cite}</a>'
                f'</blockquote>'
            )
        else:
            quote_html = (
                f'<blockquote class="axis-quote">\u201c{esc(q)}\u201d {cite}</blockquote>'
            )

    return f"""
        <div class="detail-axis">
          <div class="detail-axis-head">
            <span class="detail-axis-name">{esc(name)}</span>
          </div>
          <div class="axis-row">
            <div class="axis-bar">
              <div class="bar-pos" style="width:{p}%"></div>
              <div class="bar-neg" style="width:{n}%"></div>
            </div>
            <span class="axis-counts">{pos} pos · {neu} neu · {neg} neg</span>
          </div>
          {quote_html}
        </div>"""


def sources_section(sources):
    """Group sources by type, each a clickable link to the original."""
    groups = {}
    for s in sources:
        st = (s.get("source_type") or s.get("platform") or "other").lower()
        groups.setdefault(st, []).append(s)

    blocks = []
    for st, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        icon, label = SOURCE_TYPE_LABELS.get(st, SOURCE_TYPE_FALLBACK)
        links = []
        for s in items:
            title = s.get("title") or s.get("reviewer") or s.get("source_id", "source")
            reviewer = s.get("reviewer", "")
            url = s.get("url", "")
            byline = f' <span class="src-by">{esc(reviewer)}</span>' if reviewer else ""
            if url:
                links.append(
                    f'<li><a href="{esc(url)}" target="_blank" rel="nofollow noopener">{esc(title)}</a>{byline}</li>'
                )
            else:
                links.append(f"<li>{esc(title)}{byline}</li>")
        blocks.append(
            f'<div class="src-group"><h3 class="src-group-head">{icon} {esc(label)} '
            f'<span class="src-count">({len(items)})</span></h3>'
            f'<ul class="src-list">{"".join(links)}</ul></div>'
        )
    return f'<div class="src-grid">{"".join(blocks)}</div>'


def price_history_section(card):
    used = (card.get("pricing", {}) or {}).get("used_market", {}) or {}
    bands = used.get("bands", {}) or {}
    if not bands:
        return ""
    cells = []
    for band, price in bands.items():
        val = f"${int(price)}" if isinstance(price, (int, float)) and price > 0 else "\u2014"
        cells.append(f'<div class="band"><span class="band-label">{esc(band)}</span><span class="band-price">{val}</span></div>')
    sold = used.get("sold_last_90d", 0)
    sold_line = f'<p class="band-note">{sold} sold in the last 90 days on {esc(used.get("source","eBay"))}.</p>' if sold else ""
    asof = fmt_date_human(used.get("price_updated_at") or "")
    asof_line = f'<p class="band-note">Prices as of {esc(asof)} — lowest active listing per condition.</p>' if asof else ""
    return f"""
      <section class="card-section">
        <h2 class="card-section-head">{esc(used_price_heading(card))}</h2>
        <div class="band-grid">{"".join(cells)}</div>
        {sold_line}
        {asof_line}
      </section>"""


# ─── Specs (product-forward; honest about absence) ──────────────────────────
# Specs are a facts-pipeline output (fact-pipeline §3). On an external card they
# live under `facts.specs` as a {slug: FactValue} map — FactValue is the dict
# {value, low, high, anchor, anchor_source, unit}. Numeric facts carry an honest
# spread (§3.3): the anchor (manufacturer value) is shown first, and a genuine
# [low, high] range is appended when third-party sources widen it. Categorical
# facts use `value`. When absent or empty the section is omitted entirely (same
# discipline as pricing/issue_clusters), never rendered as an empty box.

# Slugs that carry a canonical casing the naive title-case would mangle.
_SPEC_LABEL_OVERRIDES = {
    "iso": "ISO", "gtin": "GTIN", "mpn": "MPN", "af": "AF", "ois": "OIS",
    "ibis": "IBIS", "nd": "ND", "led": "LED", "usb": "USB", "hdmi": "HDMI",
    "fps": "FPS", "mp": "MP", "ev": "EV", "id": "ID",
}


def _spec_label(slug):
    """Humanize a spec slug (e.g. 'sensor_resolution' -> 'Sensor Resolution',
    'iso' -> 'ISO'). Known acronyms keep their canonical casing."""
    words = str(slug).replace("_", " ").replace("-", " ").split()
    return " ".join(_SPEC_LABEL_OVERRIDES.get(w.lower(), w.capitalize())
                    for w in words) or str(slug)


def _fmt_fact_num(n):
    """Render a numeric fact without false precision: drop a trailing '.0'
    (665.0 -> '665') but keep genuine decimals (66.5 -> '66.5')."""
    if isinstance(n, bool) or not isinstance(n, (int, float)):
        return esc(n)
    if isinstance(n, float) and n.is_integer():
        return str(int(n))
    return str(n)


def _fact_display(fv):
    """One FactValue -> display string, or '' if it carries nothing renderable.
    Accepts a plain scalar too (legacy flat {label: value} cards)."""
    if fv is None:
        return ""
    if not isinstance(fv, dict):
        # Legacy flat value — already a display-ready scalar.
        return "" if fv in ("", []) else esc(fv)
    unit = str(fv.get("unit") or "").strip()
    suffix = f" {esc(unit)}" if unit else ""
    anchor, low, high = fv.get("anchor"), fv.get("low"), fv.get("high")
    is_num = lambda x: isinstance(x, (int, float)) and not isinstance(x, bool)
    if is_num(anchor) or is_num(low) or is_num(high):
        primary = anchor if is_num(anchor) else low
        out = f"{_fmt_fact_num(primary)}{suffix}"
        # Only surface the spread when sources genuinely disagree.
        if is_num(low) and is_num(high) and low != high:
            out += f" ({_fmt_fact_num(low)}–{_fmt_fact_num(high)})"
        return out
    value = fv.get("value")
    if value in (None, "", []):
        return ""
    return f"{esc(value)}{suffix}"


def _specs_provenance_line(facts):
    """Subtle 'specs: manufacturer · wikidata' subtitle (§3.2/§8) — the distinct
    sources backing the block, in first-seen order. '' when none recorded."""
    prov = facts.get("provenance") if isinstance(facts, dict) else None
    if not isinstance(prov, dict):
        return ""
    seen = []
    for key, entry in prov.items():
        # This is the "specs:" line — reflect ONLY spec sources. The provenance
        # map also carries identity.* entries (image/gtin/mpn fold, § wire);
        # those must not masquerade as a spec source here.
        if not str(key).startswith("specs."):
            continue
        src = (entry or {}).get("source") if isinstance(entry, dict) else None
        if src and src not in seen:
            seen.append(src)
    if not seen:
        return ""
    return (f'<p class="spec-provenance">specs: '
            f'{esc(" · ".join(seen))}</p>')


def specs_section(card):
    facts = card.get("facts") if isinstance(card.get("facts"), dict) else {}
    # Prefer the external envelope location; fall back to a top-level `specs`
    # for legacy/internal cards that never went through facts_from_card.
    specs = facts.get("specs") or card.get("specs") or {}
    rows = []
    if isinstance(specs, dict):
        items = specs.get("items", specs) if "items" in specs else specs
        if isinstance(items, dict):
            for slug, fv in items.items():
                disp = _fact_display(fv)
                if disp:
                    rows.append((_spec_label(slug), disp))
        elif isinstance(items, list):
            for d in items:
                disp = _fact_display(d.get("value"))
                if disp:
                    rows.append((d.get("label", ""), disp))
    elif isinstance(specs, list):
        for d in specs:
            disp = _fact_display(d.get("value"))
            if disp:
                rows.append((d.get("label", ""), disp))
    if not rows:
        return ""
    cells = "".join(
        f'<div class="spec-row"><span class="spec-label">{esc(label)}</span>'
        f'<span class="spec-value">{value}</span></div>'
        for label, value in rows
    )
    prov_html = _specs_provenance_line(facts)
    return f"""
      <section class="card-section">
        <h2 class="card-section-head">{esc(specs_heading(card))}</h2>
        <div class="spec-grid">{cells}</div>{prov_html}
      </section>"""


def _has_sentiment(axis):
    """True if an axis carries any rated sentiment. Axes with total==0 are
    empty (no reviewer touched them) and are suppressed from the page entirely
    rather than rendered as a 0/0/0 bar."""
    return ((axis.get("sentiment", {}) or {}).get("total", 0) or 0) > 0


# Meta-axes suppressed from the card page entirely (ratified 2026-06-05).
# generation_context is extractor discourse-context, not a product quality —
# rendering it as a sentiment bar misleads. Distinct from TEASER_META_AXES:
# price stays visible on the page, it is only barred from teaser high/low roles.
CARD_HIDDEN_META_AXES = {"generation_context"}


def _card_visible(axis):
    return (_has_sentiment(axis)
            and (axis.get("axis_id") or "") not in CARD_HIDDEN_META_AXES)


# ─── Page assembly ──────────────────────────────────────────────────────────
# ─── schema.org Product/Offer JSON-LD ────────────────────────────────────────
# Honesty discipline carries through markup: we only assert prices we display.
# Used bands (eBay active asks, precision-gated upstream) -> AggregateOffer
# with UsedCondition. No bands -> Product without offers (Sigma-style gating).
# No ratings markup: our pos/neg ratios are not a star scale; inventing a
# mapping to win rich results would be result-picking. Offers only.
def schema_org_jsonld(card, canonical_url, img_url, description):
    ident = card["identity"]
    obj = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": ident["display_name"],
        "url": canonical_url,
    }
    if ident.get("brand"):
        obj["brand"] = {"@type": "Brand", "name": ident["brand"]}
    if img_url:
        obj["image"] = abs_url(img_url)
    if description:
        obj["description"] = description

    # Freshness for recency-weighting answer engines (maddi-distribution §4.8:
    # "the freshness machinery is a GEO weapon iff surfaced"). datePublished is
    # the first-build date; dateModified rides the change-gated nightly rebuild.
    # Both are date-grain and honest — see schema_modified_date. Widely read on
    # Product by consumers even though the property's schema.org home is
    # CreativeWork; schema.org domains are advisory, not enforced.
    published = schema_published_date(card)
    modified = schema_modified_date(card)
    if published:
        obj["datePublished"] = published
    if modified:
        obj["dateModified"] = modified

    used = (card.get("pricing", {}) or {}).get("used_market", {}) or {}
    bands = used.get("bands", {}) or {}
    prices = [v for v in bands.values() if isinstance(v, (int, float)) and v > 0]
    if prices:
        _, used_url = used_cta(card)
        offer = {
            "@type": "AggregateOffer",
            "priceCurrency": "USD",
            "lowPrice": f"{min(prices):.2f}",
            "highPrice": f"{max(prices):.2f}",
            "itemCondition": "https://schema.org/UsedCondition",
            "availability": "https://schema.org/InStock",
            "url": used_url,
        }
        sample = used.get("sample_size")
        if isinstance(sample, int) and sample > 0:
            offer["offerCount"] = sample
        obj["offers"] = offer

    # `</` escaped so card text can never close the script element.
    return json.dumps(obj, indent=2).replace("</", "<\\/")


def render_page(card, image_url=None):
    ident = card["identity"]
    name = ident["display_name"]
    brand = ident.get("brand", "")
    subcat = (ident.get("subcategory") or "").title()
    cat = (ident.get("category") or "").title()
    year = ident.get("year_introduced") or ""
    descriptor = " · ".join([x for x in [brand, f"{subcat} {cat}".strip(), str(year)] if x])

    fresh = card.get("freshness", {}) or {}
    source_count = fresh.get("source_count", len(card.get("sources", [])))

    conf = (card.get("confidence", {}) or {}).get("overall", "unknown")

    # Two clocks, deliberately labelled apart (see synthesis_asof). The hero
    # meta carries the SYNTHESIS date; price recency is rendered separately by
    # asof_html. "Last updated" was ambiguous — a reader takes it for the whole
    # page, so a nightly price tick would appear to vouch for month-old
    # synthesis. Naming the clock is both honest and the stronger claim.
    # Parallel phrasing with the price line ("Used price as of …") is doing
    # real work here: two clauses of identical shape and different nouns read
    # as two distinct facts, where one generic stamp read as a claim about the
    # whole page.
    synth_date, synth_days = synthesis_asof(card)
    if synth_date and synth_days is not None:
        if synth_days == 0:
            synth_meta = f"· Analysis as of {esc(synth_date)} (today)"
        elif synth_days == 1:
            synth_meta = f"· Analysis as of {esc(synth_date)} (1 day ago)"
        else:
            synth_meta = f"· Analysis as of {esc(synth_date)} ({synth_days} days ago)"
    elif synth_date:
        synth_meta = f"· Analysis as of {esc(synth_date)}"
    else:
        synth_meta = ""

    new_label, new_url = new_cta(card)
    used_label, used_url = used_cta(card)
    amazon_rung = amazon_cta(card)

    synth = (card.get("synthesis", {}) or {}).get("consensus_paragraph", "")

    # Phase 1 (maddi-distribution): year interpolated at RENDER, never stored,
    # so the heading rolls over at the year boundary with no rebuild. Kept out
    # of the <h1>: that stays the bare product name, which is the branded-search
    # anchor and the schema.org `name` — a churning heading buys nothing the
    # title tag isn't already getting.
    # Year in the title carries the citation gain, but it must describe OUR
    # analysis rather than the sources: publication_date is empty across every
    # source we hold, and slug-inferable years span 2021-2026. "review in 2026"
    # would claim a recency the corpus contradicts.
    _analysis_year = analysis_year(card)
    title_asof = f", analyzed {_analysis_year}" if _analysis_year else ""
    stat_line = answer_stat_line(card, source_count)

    # Empty axes (sentiment.total == 0) are suppressed entirely — no reviewer
    # touched them, so rendering a 0/0/0 bar is noise, not information.
    # Meta-axes in CARD_HIDDEN_META_AXES are likewise suppressed from the page.
    lead = [a for a in (card.get("lead_axes", []) or []) if _card_visible(a)]
    detail = [a for a in (card.get("detail_axes", []) or []) if _card_visible(a)]

    lead_html = "\n".join(axis_block(a) for a in lead)
    detail_html = "\n".join(axis_block(a) for a in detail)

    # Issue clusters — omit entirely if empty (don't render an empty box).
    clusters = (card.get("synthesis", {}) or {}).get("issue_clusters", []) or []
    clusters_html = ""
    if clusters:
        items = "".join(
            f'<li class="issue"><span class="issue-warn">\u26a0</span> {esc(c.get("label", c.get("aspect","")))} '
            f'<span class="issue-cites">({c.get("citations", c.get("count",0))} citations)</span></li>'
            for c in clusters
        )
        clusters_html = f"""
      <section class="card-section">
        <h2 class="card-section-head">What problems do reviewers report?</h2>
        <ul class="issue-list">{items}</ul>
      </section>"""

    sources_html = sources_section(card.get("sources", []) or [])
    price_html = price_history_section(card)
    specs_html = specs_section(card)

    # Key Axes header only renders if at least one non-empty lead axis survives.
    lead_section = (
        f'''<section class="card-section">
        <h2 class="card-section-head">What do reviewers praise and criticize?</h2>
        <div class="axes-stack">{lead_html}</div>
      </section>''' if lead else ''
    )

    # Product image — use card identity image if present, else a placeholder block.
    # Product image precedence: card's own fields first (Phase 3 will populate
    # identity.image_hero), then an explicitly supplied URL (e.g. the
    # manufacturer shot already in cards-manifest.json), then placeholder.
    img_url = (ident.get("image_hero") or ident.get("image_thumb")
               or card.get("image_thumb") or image_url)
    img_html = (
        f'<img class="hero-product-img" src="{esc(img_url)}" alt="{esc(name)}" loading="eager">'
        if img_url else '<div class="hero-product-img placeholder">No image yet</div>'
    )

    meta_desc = meta_description(card, source_count, name, synth)

    canonical = f"{BASE_URL}/cards/{card['card_id']}/"
    asof_human = fmt_date_human(used_price_asof(card))
    _used_bands = ((card.get("pricing", {}) or {}).get("used_market", {}) or {}).get("bands", {}) or {}
    has_used_band = any(isinstance(v, (int, float)) and v > 0 for v in _used_bands.values())
    asof_html = (
        f'<p class="price-asof">Used price as of {esc(asof_human)} · active listings</p>'
        if (asof_human and has_used_band) else ""
    )
    jsonld = schema_org_jsonld(card, canonical, img_url, meta_desc)
    twitter_card = "summary_large_image" if img_url else "summary"

    # Share block (maddi-distribution: humans post, machines only draft). The
    # card already carries ready-to-paste export.reddit / export.discord (built
    # upstream in phantom-ops card_export.build_export_artifacts). Render them
    # into a JSON payload the /js/share.js clipboard handler reads — never an
    # auto-post, just a copy the person pastes where the question came up. Only
    # rendered when the card actually carries share text.
    _export = card.get("export", {}) or {}
    _share_reddit = _export.get("reddit", "")
    _share_discord = _export.get("discord", "")
    _share_permalink = _export.get("permalink") or canonical
    share_html = ""
    if _share_reddit or _share_discord:
        _share_payload = json.dumps(
            {"reddit": _share_reddit, "discord": _share_discord,
             "permalink": _share_permalink},
            ensure_ascii=False,
        ).replace("<", "\\u003c")  # never let content close the <script> early
        _share_buttons = ""
        if _share_reddit:
            _share_buttons += ('<button class="btn-share" type="button" '
                               'data-share="reddit">Copy for Reddit</button>')
        if _share_discord:
            _share_buttons += ('<button class="btn-share" type="button" '
                               'data-share="discord">Copy for Discord</button>')
        _share_buttons += ('<button class="btn-share btn-share-link" type="button" '
                           'data-share="permalink">Copy link</button>')
        share_html = f"""
      <section class="card-section card-share">
        <h2 class="card-section-head">Share this card</h2>
        <p class="share-intro">Copy a ready-to-paste summary — the consensus, the source count, the link, and an honest “I built this” disclosure — then post it where the question came up.</p>
        <div class="share-actions">{_share_buttons}</div>
        <p class="share-note" data-share-note aria-live="polite"></p>
        <script type="application/json" data-share-payload>{_share_payload}</script>
      </section>"""

    # Phase 0 measurement seam (maddi-distribution v2.0): the BUILD writes the
    # category and per-CTA retailer as data-attributes; the beacon only ever
    # reads them. Category is the card's own lowercase category — never user
    # input — matching the /ping "category only, never the query" line.
    page_category = (ident.get("category") or "unknown").strip().lower()
    new_retailer = retailer_from_url(new_url)
    used_retailer = retailer_from_url(used_url)

    # Amazon rung: omitted ENTIRELY when amazon_cta() returns None (absent-list
    # card, or no name to scope). Same doctrine as empty issue_clusters — we
    # render what the card supports, never an empty box.
    amazon_html = ""
    if amazon_rung:
        az_label, az_url = amazon_rung
        amazon_html = (
            f'<a class="btn-affiliate btn-buy-amazon" href="{esc(az_url)}" '
            f'target="_blank" rel="nofollow noopener sponsored" data-out '
            f'data-retailer="{retailer_from_url(az_url)}" '
            f'data-category="{esc(page_category)}">{esc(az_label)} \u2192</a>'
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{esc(name)} review — {source_count} sources{title_asof} | AskMaddi</title>
  <meta name="description" content="{esc(meta_desc)}">
  <link rel="canonical" href="{esc(canonical)}">
  <meta property="og:title" content="{esc(name)} — AskMaddi">
  <meta property="og:description" content="{esc(meta_desc)}">
  <meta property="og:type" content="product">
  <meta property="og:url" content="{esc(canonical)}">
  <meta property="og:site_name" content="{SITE_NAME}">
  {f'<meta property="og:image" content="{esc(abs_url(img_url))}">' if img_url else ''}
  <meta name="twitter:card" content="{twitter_card}">
  <meta name="twitter:title" content="{esc(name)} — AskMaddi">
  <meta name="twitter:description" content="{esc(meta_desc)}">
  {f'<meta name="twitter:image" content="{esc(abs_url(img_url))}">' if img_url else ''}
  <script type="application/ld+json">
{jsonld}
  </script>
  <link rel="icon" type="image/png" href="/images/logo.png">
  <link rel="stylesheet" href="/css/maddi.css">
  <link rel="stylesheet" href="/css/cards-detail.css">
</head>
<body data-category="{esc(page_category)}">
  <div class="affiliate-disclosure-bar">Disclosure: We earn a commission when you buy through links on this page, at no cost to you.</div>
  <div class="container">

    <header class="header-compact">
      <a href="/" class="logo-title"><img src="/images/logo.png" alt="AskMaddi" class="site-logo">AskMaddi</a>
      <div class="search-box">
        <input type="text" id="detail-search-input" placeholder="Search another product\u2026" onkeydown="if(event.key==='Enter')document.getElementById('detail-search-button').click()">
        <button id="detail-search-button" onclick="location.href='/?q='+encodeURIComponent(document.getElementById('detail-search-input').value)">Ask Maddi</button>
      </div>
    </header>

    <article class="card-detail">

      <section class="card-hero">
        <div class="hero-media">{img_html}</div>
        <div class="hero-body">
          <h1 class="hero-title">{esc(name)}</h1>
          <p class="hero-descriptor">{esc(descriptor)}</p>
          <div class="hero-actions">
            <a class="btn-affiliate btn-buy-new" href="{esc(new_url)}" target="_blank" rel="nofollow noopener sponsored" data-out data-retailer="{new_retailer}" data-category="{esc(page_category)}">{esc(new_label)} \u2192</a>
            <a class="btn-affiliate btn-buy-used" href="{esc(used_url)}" target="_blank" rel="nofollow noopener sponsored" data-out data-retailer="{used_retailer}" data-category="{esc(page_category)}">{esc(used_label)} \u2192</a>
            {amazon_html}
          </div>
          {asof_html}
          <p class="hero-meta">
            Synthesized from <strong>{source_count}</strong> reviewer sources
            {synth_meta}
          </p>
        </div>
      </section>

      {f'''<section class="card-section answer-first">
        <h2 class="card-section-head">What do reviewers say about the {esc(name)}?</h2>
        {f'<p class="answer-stats">{esc(stat_line)}</p>' if stat_line else ''}
        {f'<p class="synthesis-text">{esc(synth)}</p>' if synth else ''}
      </section>''' if (stat_line or synth) else ''}

      {specs_html}

      {lead_section}

      {clusters_html}

      {f'''<section class="card-section">
        <details class="detail-axes-toggle">
          <summary><h2 class="card-section-head inline">More detail — {len(detail)} additional axes</h2></summary>
          <div class="axes-stack">{detail_html}</div>
        </details>
      </section>''' if detail else ''}

      {price_html}

      <section class="card-section" id="sources">
        <h2 class="card-section-head">Which reviews is this based on? <span class="src-total">({source_count})</span></h2>
        <p class="src-intro">Every claim above traces to these original reviews. We don't write opinions \u2014 we synthesize theirs.</p>
        {sources_html}
      </section>
      {share_html}

    </article>

    <section class="card-section subscribe-section">
      <h2 class="card-section-head">New cards weekly</h2>
      <p class="subscribe-intro">Get each new review synthesis as it publishes. No spam, unsubscribe anytime.</p>
      <form data-subscribe class="subscribe-form" autocomplete="off">
        <input type="email" name="email" placeholder="you@example.com" required aria-label="Email address">
        <input type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true" style="position:absolute;left:-9999px;opacity:0;height:0;width:0;">
        <button type="submit">Subscribe</button>
      </form>
      <p class="subscribe-note" aria-live="polite"></p>
    </section>

    <footer class="card-footer">
      <a href="/">\u2190 Back to AskMaddi</a>
      <span>·</span>
      <a href="/mission.html">Our method</a>
      <span>·</span>
      <a href="/why.html">Why AskMaddi</a>
      <span>·</span>
      <a href="/bookmarklet.html">Bookmarklet</a>
    </footer>

  </div>
  <script src="/js/beacon.js" defer></script>
  <script src="/js/share.js" defer></script>
</body>
</html>
"""


# ─── Manifest (teaser grid) regeneration ────────────────────────────────────

# Axes excluded from the highest-rated / biggest-gripe teaser slots. These are
# meta-axes (relative context, value-for-money) — true but uninformative as a
# headline "high" or "low" next to concrete product qualities. They remain
# eligible for the most-discussed slot and always render on the detail page.
# Excluding CATEGORIES of axis by role is a design rule, not result-picking.
TEASER_META_AXES = {"generation_context", "price"}

TEASER_ROLE_MOST = "most_discussed"
TEASER_ROLE_HIGH = "highest_rated"
# Renamed from "biggest_gripe" 2026-07-28. It is computed as the WORST
# POSITIVE RATIO, which is not criticism: an axis can hold the lowest positive
# share while carrying almost no negatives, because neutral claims describe
# rather than judge. sony-a7s-iii shipped mount compatibility tagged
# "biggest gripe" on THREE negative claims out of 67 -- its lowest negative
# share -- because 55% of that axis was neutral.
#
# The metric is kept, because the teaser's job is a coherent comparison:
# highest_rated and lowest_rated are the two ends of ONE measure, and mixing
# a negative-share metric into a three-bar triple would compare incomparable
# things. Only the name was lying. The paragraph's criticism sentence is a
# separate, properly-gated selection (phantom-ops axis_roles.most_criticised).
TEASER_ROLE_LOW = "lowest_rated"


def _teaser_axes_from_card(card, axes):
    """Resolve the card's own axis_roles block into (axis, role) pairs.

    Returns None when the card predates the block or carries it unpopulated,
    which sends the caller to the legacy computation. An axis_id naming an
    axis that is not on the card is treated as no answer rather than a crash:
    a stale block should degrade to recomputation, not take the page down.
    """
    block = card.get("axis_roles") or {}
    if not block.get("computed_by"):
        return None
    by_id = {}
    for a in axes:
        key = a.get("axis_id") or a.get("display_name")
        if key is not None:
            by_id.setdefault(key, a)

    picks, used = [], set()
    for role in (TEASER_ROLE_MOST, TEASER_ROLE_HIGH, TEASER_ROLE_LOW):
        axis_id = block.get(role)
        if axis_id is None:
            continue
        axis = by_id.get(axis_id)
        if axis is None or id(axis) in used:
            continue
        used.add(id(axis))
        picks.append((axis, role))
    return picks or None


def select_teaser_axes(card):
    """Pick three role-based teaser axes (2026-06-03 design):

      1. most_discussed — highest claim volume (meta-axes eligible)
      2. highest_rated  — best pos-ratio among qualifying non-meta axes
      3. lowest_rated   — worst pos-ratio among qualifying non-meta axes
         (the low end of the SAME measure as highest_rated — not a
          criticism claim; see TEASER_ROLE_LOW)

    Qualifying = sentiment.total >= max(15, 0.1 * top axis volume). The
    relative floor scales across corpus sizes; the absolute floor stops a
    4-claim axis headlining a slot on noise.

    Collisions resolve to the next distinct axis (an axis fills one slot
    only). If fewer than three axes qualify, remaining slots fill in plain
    volume order with role=None (renderer shows no label on those).

    Returns a list of (axis_dict, role_or_None), length <= 3.

    THE CARD DECIDES (2026-07-28). A card carrying an `axis_roles` block was
    told which axis fills each slot by the pipeline, using the shared
    selectors in phantom-ops aggregator-build/axis_roles.py. We honour that
    rather than re-deriving it here, because two computations of one selection
    is how "biggest_gripe" came to mean "worst positive ratio" on this side
    with nothing checking it against the other.

    The block below is the FALLBACK, for cards built before the block existed.
    test_build_site.py asserts the two paths agree on every live card, so this
    can be deleted once the catalog has turned over rather than lingering as
    an unexamined second opinion.
    """
    axes = (card.get("lead_axes") or []) + (card.get("detail_axes") or [])
    from_card = _teaser_axes_from_card(card, axes)
    if from_card is not None:
        return from_card
    scored = []
    for a in axes:
        s = a.get("sentiment", {}) or {}
        total = s.get("total", 0) or 0
        if total <= 0:
            continue
        scored.append((a, total, (s.get("pos", 0) or 0) / total))
    if not scored:
        return []
    scored.sort(key=lambda t: t[1], reverse=True)
    floor = max(15, 0.1 * scored[0][1])
    qualifying = [t for t in scored if t[1] >= floor]

    def _aid(a):
        return a.get("axis_id") or a.get("display_name") or id(a)

    picks, used = [], set()

    def take(entry, role):
        used.add(_aid(entry[0]))
        picks.append((entry[0], role))

    if qualifying:
        take(qualifying[0], TEASER_ROLE_MOST)

    def pool():
        return [t for t in qualifying
                if _aid(t[0]) not in used
                and (t[0].get("axis_id") or "") not in TEASER_META_AXES]

    cands = pool()
    if cands:
        take(max(cands, key=lambda t: t[2]), TEASER_ROLE_HIGH)
    cands = pool()
    if cands:
        take(min(cands, key=lambda t: t[2]), TEASER_ROLE_LOW)

    # Sparse-card fallback: fill remaining slots in volume order, unlabeled.
    for t in scored:
        if len(picks) >= 3:
            break
        if _aid(t[0]) not in used:
            take(t, None)

    return picks


_GRID_CATEGORY_RANK = {"body": 0, "lens": 1, "support": 2}


def order_cards_for_grid(cards):
    """Order cards for the homepage grid: newest card first (hero), then the
    rest grouped body -> lens -> support -> other, newest-first within each
    group. Never mutates the input; returns a new list of the same length.
    """
    def created_at(card):
        return (card.get("freshness") or {}).get("created_at") or ""

    def category_rank(card):
        cat = ((card.get("identity") or {}).get("category") or "").lower()
        return _GRID_CATEGORY_RANK.get(cat, 3)

    by_id = sorted(cards, key=lambda c: c["card_id"])
    newest_first = sorted(by_id, key=created_at, reverse=True)

    if not newest_first:
        return []

    hero, rest = newest_first[0], newest_first[1:]
    grouped = sorted(rest, key=category_rank)
    return [hero] + grouped


def teaser_entry(card):
    ident = card["identity"]
    # cards.js reads pricing.new_price / used_price (numbers) and runs its own
    # formatPrice() -> "$899" or "Check price" on zero/null. It appends the
    # " new" / " used" suffix itself, so we hand it raw numerics + URLs, NOT
    # the pre-labelled strings the detail page uses.
    pricing = card.get("pricing", {}) or {}
    new_price = pricing.get("current_new_usd") or pricing.get("msrp_usd") or 0
    _, new_url = new_cta(card)
    used = pricing.get("used_market", {}) or {}
    used_bands = [v for v in (used.get("bands", {}) or {}).values()
                  if isinstance(v, (int, float)) and v > 0]
    used_price = min(used_bands) if used_bands else 0
    _, used_url = used_cta(card)
    # Amazon rung for the matched-card view in search results. URL only — the
    # LABEL is a constant baked into cards.js, never a price, and never derived
    # from card data. Empty string when there is no rung (absent-list card), so
    # cards.js omits the button rather than rendering a dead one.
    az = amazon_cta(card)
    amazon_url = az[1] if az else ""
    top = select_teaser_axes(card)
    return {
        "card_id": card["card_id"],
        "display_name": ident["display_name"],
        "brand": ident.get("brand", ""),
        "category": (ident.get("category") or "").title(),
        "subcategory": (ident.get("subcategory") or "").title(),
        "image_thumb": ident.get("image_thumb") or card.get("image_thumb") or "",
        "source_count": card.get("freshness", {}).get("source_count", len(card.get("sources", []))),
        "confidence": card.get("confidence", {}).get("overall", "unknown"),
        "top_axes": [
            {
                "axis": a.get("display_name") or a.get("axis_id"),
                "pos": (a.get("sentiment", {}) or {}).get("pos", 0),
                # neu rides with its siblings: a teaser bar that carries only
                # pos and neg lets a reader infer the remainder is negative
                # space rather than neutral judgement. Same rule as the card
                # body -- every share travels with the other two.
                "neu": (a.get("sentiment", {}) or {}).get("neu", 0),
                "neg": (a.get("sentiment", {}) or {}).get("neg", 0),
                "total": (a.get("sentiment", {}) or {}).get("total", 0),
                "role": role,
            }
            for a, role in top
        ],
        "pricing": {
            "new_price": int(new_price) if new_price else 0, "new_url": new_url,
            "used_price": int(used_price) if used_price else 0, "used_url": used_url,
            "amazon_url": amazon_url,
        },
        "card_url": f"cards/{card['card_id']}/",
    }


def load_cards(args):
    cards = []
    if args.card:
        cards.append(json.loads(Path(args.card).read_text(encoding="utf-8")))
    if args.cards_dir:
        for p in sorted(Path(args.cards_dir).glob("*.json")):
            cards.append(json.loads(p.read_text(encoding="utf-8")))
    return cards


ASIN_REGISTRY_PATH = Path(__file__).parent.parent / "data" / "asin_registry.json"
EBAY_EPID_REGISTRY_PATH = Path(__file__).parent.parent / "data" / "ebay_epid_registry.json"


def apply_asin_registry(cards):
    """Inject durable Amazon ASINs into each card's pricing block.

    Aggregator rebuilds regenerate data/cards/*.json from extraction output,
    which carries no amazon_asin — so a backfilled ASIN is lost on every
    rebuild, and new_cta() falls through to a search-results URL (the tag
    survives but Amazon strips it on click-through to a product). The registry
    (data/asin_registry.json, keyed by card_id) is the durable source of truth
    re-applied at build time. A card that already carries an ASIN wins (a fresh
    PA-API value should never be clobbered by a static registry entry).
    """
    try:
        reg = json.loads(ASIN_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cards  # registry optional; absence degrades to prior behavior
    asins = reg.get("asins", {})
    absent = set(reg.get("absent", []))
    for card in cards:
        cid = card.get("card_id")
        if cid in absent:
            card.setdefault("pricing", {})["amazon_absent"] = True
        asin = asins.get(cid)
        if not asin:
            continue
        pricing = card.setdefault("pricing", {})
        if not pricing.get("amazon_asin"):
            pricing["amazon_asin"] = asin
    return cards


SKUS_SPINE_PATH = Path(__file__).parent.parent / "data" / "skus.json"


def apply_spine_gtins(cards):
    """Inject the spine's verified GTIN into each card's pricing block.

    Third sibling of the registry-merge pair below, but sourced from the
    LIVE spine (data/skus.json) rather than a hand-kept registry — GTINs
    are machine-verified with provenance receipts and survive rebuilds on
    the spine by construction. The gtin is the cross-source join key (Adorama
    Partnerize feed + Icecat specs); a card already carrying a gtin wins.
    """
    try:
        skus = json.loads(SKUS_SPINE_PATH.read_text(encoding="utf-8")).get("skus", {})
    except (OSError, ValueError):
        return cards  # spine optional here; absence degrades the rung away
    for card in cards:
        entry = skus.get(card.get("card_id")) or {}
        g = entry.get("gtin")
        if g:
            pricing = card.setdefault("pricing", {})
            if not pricing.get("gtin"):
                pricing["gtin"] = g
    return cards


def apply_ebay_epid_registry(cards):
    """Inject durable eBay catalog EPIDs into each card's pricing block.

    The eBay analogue of apply_asin_registry. Same rebuild-survival rationale:
    aggregator rebuilds drop marketplace ids, so the durable EPID lives in
    data/ebay_epid_registry.json (keyed by card_id) and is re-applied here at
    build time. A card that already carries an ebay_epid wins (a live-resolved
    value should never be clobbered by a static registry entry). Absence is
    fine — new_cta() then degrades a no-ASIN card to a tagged eBay search rather
    than an eBay /p/ deep-link, which is still correct and earning-capable.
    """
    try:
        reg = json.loads(EBAY_EPID_REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cards  # registry optional; absence degrades to prior behavior
    epids = reg.get("epids", {})
    for card in cards:
        cid = card.get("card_id")
        epid = epids.get(cid)
        if not epid:
            continue
        pricing = card.setdefault("pricing", {})
        if not pricing.get("ebay_epid"):
            pricing["ebay_epid"] = epid
    return cards


def card_lastmod(card):
    """Most recent observation date for a card: max(content build, price fetch).
    YYYY-MM-DD or '' — sitemap omits lastmod rather than inventing one."""
    fresh = (card.get("freshness", {}) or {}).get("last_built") or ""
    price = used_price_asof(card)
    best = max(d for d in (str(fresh), str(price), "")) if (fresh or price) else ""
    return best[:10]


SITEMAP_STATIC_PAGES = ["/", "/mission.html", "/privacy.html", "/terms.html"]


def write_sitemap(out_dir, cards):
    """browser/sitemap.xml — static pages + every card page, lastmod from card
    data. Derived artifact: regenerates from cards, so Stage 6 additions are
    indexed for free."""
    card_mods = {c["card_id"]: card_lastmod(c) for c in cards}
    home_mod = max([m for m in card_mods.values() if m], default="")

    def url_el(loc, lastmod=""):
        lm = f"\n    <lastmod>{lastmod}</lastmod>" if lastmod else ""
        return f"  <url>\n    <loc>{loc}</loc>{lm}\n  </url>"

    entries = [url_el(BASE_URL + "/", home_mod)]
    entries += [url_el(BASE_URL + p) for p in SITEMAP_STATIC_PAGES[1:]]
    entries += [url_el(f"{BASE_URL}/cards/{cid}/", mod) for cid, mod in sorted(card_mods.items())]

    xml = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
           + "\n".join(entries) + "\n</urlset>\n")
    path = Path(out_dir) / "sitemap.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def retailer_from_url(url):
    """'amazon' | 'ebay' | 'adorama' | 'other' from a CTA href's hostname.

    Feeds the data-retailer attribute the beacon reads; the gateway whitelists
    the same four values server-side (analytics_log.RETAILERS), so a surprise
    hostname degrades to 'other' at both ends rather than ever landing as
    free text. Hostname match, not substring-in-url: a search URL whose QUERY
    mentions amazon must not count as an Amazon click.

    Adorama's new-goods CTAs are Partnerize URL-wraps: the click host is
    'adorama.prf.hn' (deep-link) or 'prf.hn' (the generic short link), with
    'adorama.com' only when an un-wrapped destination slips through. AskMaddi
    uses Partnerize solely for Adorama, so prf.hn maps to 'adorama' for us."""
    try:
        host = urllib.parse.urlparse(url or "").hostname or ""
    except ValueError:
        return "other"
    host = host.lower()
    if host == "amazon.com" or host.endswith(".amazon.com"):
        return "amazon"
    if host == "ebay.com" or host.endswith(".ebay.com"):
        return "ebay"
    if (host == "adorama.com" or host.endswith(".adorama.com")
            or host == "prf.hn" or host.endswith(".prf.hn")):
        return "adorama"
    return "other"


def write_llms_txt(out_dir, cards):
    """browser/llms.txt — the two-minute lottery ticket (maddi-distribution
    v2.0 §10 correction 1: crawlers overwhelmingly skip this file; shipped
    because it costs one function and might matter to future agents, carried
    with ZERO priority weight and no maintenance promise beyond this
    regeneration). Derived artifact like the sitemap: regenerates from cards."""
    lines = [
        "# AskMaddi",
        "",
        "> AskMaddi synthesizes product reviews from dozens of independent",
        "> sources into per-product cards: claim-level sentiment on concrete",
        "> axes, every claim attributed and linked to its original review,",
        "> used-price bands refreshed nightly. We aggregate others' assessments",
        "> and never write our own opinions; a human gate reviews every card",
        "> before publish. Affiliate-supported, disclosed on every page.",
        "",
        "## Cards",
        "",
    ]
    for c in sorted(cards, key=lambda c: c.get("card_id", "")):
        cid = c.get("card_id", "")
        name = ((c.get("identity", {}) or {}).get("display_name") or cid)
        n_src = c.get("freshness", {}).get("source_count",
                                           len(c.get("sources", []) or []))
        lines.append(f"- [{name}]({BASE_URL}/cards/{cid}/): review synthesis"
                     f" from {n_src} sources")
    lines += [
        "",
        "## Method",
        "",
        f"- [Why AskMaddi]({BASE_URL}/why.html): editorial philosophy",
        f"- [Our method]({BASE_URL}/mission.html): how synthesis works",
        "",
    ]
    path = Path(out_dir) / "llms.txt"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser(description="Generate AskMaddi card detail pages.")
    ap.add_argument("--card", help="Path to a single card JSON.")
    ap.add_argument("--cards-dir", help="Directory of card JSONs (*.json).")
    ap.add_argument("--output-dir", default="browser", help="Output root (default: browser/).")
    ap.add_argument("--manifest", action="store_true", help="Also regenerate cards-manifest.json.")
    ap.add_argument("--sitemap", action="store_true", help="Also regenerate sitemap.xml.")
    ap.add_argument("--image-url", help="Fallback product image URL when the card carries none "
                                        "(single-card builds only; card fields take precedence).")
    args = ap.parse_args()

    if not args.card and not args.cards_dir:
        ap.error("provide --card or --cards-dir")

    # ─── Clobber guard (2026-07-09 decision; in code 2026-07-15) ─────────────
    # cards-manifest.json and sitemap.xml are WHOLE-FILE outputs regenerated
    # from only the cards loaded THIS run. Composed with --card that means
    # "replace the site's entire grid/sitemap with this one card" — which is
    # never the intent and shrank the live homepage to a single card on the
    # gate's first real publish (7/03). Publishing a single card correctly is
    # admit-to-corpus THEN full --cards-dir rebuild (admin_surface.
    # build_site_runner does exactly this). Fail loud with the recipe.
    if args.card and (args.manifest or args.sitemap):
        ap.error(
            "--manifest/--sitemap regenerate WHOLE files from only the cards "
            "loaded this run; composed with --card they would replace the "
            "entire grid/sitemap with one entry. Use --cards-dir for "
            "manifest/sitemap builds. To publish one card: admit it into the "
            "corpus dir first, then rebuild with "
            "--cards-dir <corpus> --manifest --sitemap."
        )

    out = Path(args.output_dir)
    cards = load_cards(args)
    if not cards:
        print("No cards found.", file=sys.stderr)
        return 1
    apply_asin_registry(cards)
    apply_ebay_epid_registry(cards)
    apply_spine_gtins(cards)

    written = []
    for card in cards:
        cid = card["card_id"]
        page_dir = out / "cards" / cid
        page_dir.mkdir(parents=True, exist_ok=True)
        page = page_dir / "index.html"
        page.write_text(render_page(card, image_url=args.image_url), encoding="utf-8")
        written.append(str(page))
        print(f"  \u2713 {cid} \u2192 {page}")

    if args.manifest:
        manifest = {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cards": [teaser_entry(c) for c in order_cards_for_grid(cards)],
        }
        mpath = out / "cards-manifest.json"
        mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        print(f"  \u2713 manifest \u2192 {mpath} ({len(cards)} cards)")

    if args.sitemap:
        spath = write_sitemap(out, cards)
        print(f"  \u2713 sitemap \u2192 {spath} ({len(cards)} card urls + {len(SITEMAP_STATIC_PAGES)} static)")
        # llms.txt rides the sitemap flag deliberately: identical whole-file
        # semantics (regenerated from the cards loaded THIS run), so it gets
        # the same --card clobber guard for free and no caller can rebuild
        # one without the other drifting.
        lpath = write_llms_txt(out, cards)
        print(f"  \u2713 llms.txt \u2192 {lpath} ({len(cards)} cards)")

    print(f"\nDone. {len(written)} card page(s) written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

