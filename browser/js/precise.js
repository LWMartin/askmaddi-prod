/**
 * Lane A precision — client-side streaming Sieve, applied AS results stream in.
 * ==================================================================
 * Runs in the browser over the combined eBay+Adorama rows on every source
 * arrival, so precision never blocks the stream. The heavy model rung (Qwen3
 * arbitration) stays OUT of the hot path; everything here is cheap + pure.
 *
 * Rungs (all incremental — arrangeResults() re-runs on each arrival):
 *   1. normalize   glued model tokens split ("a7iv" -> "a7 iv", "r6ii" -> "r6 ii")
 *   2. relevance   MODEL-ANCHORED: every model token in the query (a7, iv, r6,
 *                  90d, m4 …) MUST whole-token match the title, so "a7 iv" drops
 *                  BOTH "a7r iv" (a7 != a7r) AND "a7 iii" (iv != iii). Descriptor
 *                  words (sony, camera) score but aren't mandatory and tolerate a
 *                  1-edit typo ("sonny" ~ "sony"). Queries with NO model token
 *                  (NL/category: "travel tripod under $400") fall back to coverage
 *                  so they never go empty.
 *   3. classify    accessory markers ("for", "compatible", …) demote to a capped tail
 *   4. dedup       merge near-identical listings of ONE product (Jaccard >= 0.85)
 *                  into a single card carrying every listing + a "from $X" best
 *                  price — kills the "same body shown 25x" flood. (Honest dedup;
 *                  the browser has no GTIN join, so it mirrors the server's
 *                  keyless Jaccard tail, not the identity merge.)
 *   5. rank        score desc, New before Used on ties, then price asc. (Replaces
 *                  the old forced source round-robin, which buried the best match.)
 *
 * Mirrors gateway/search_cells.py + search_lane_a.py (duplicate, don't import —
 * the browser has no python). Keep the two in sync when either changes.
 */

const ACCESSORY_MARKERS = [
    /\bfor\b/, /\bcompatible\b/, /\bfits\b/, /\breplacement\b/,
    /\bfor\s+use\s+with\b/, /\baftermarket\b/,
];
const USED_CANONICAL_CAP = 12;  // cap Used listings so eBay volume can't flood
const ACCESSORY_TAIL_CAP = 8;
const JACCARD_THRESHOLD = 0.85; // near-identical titles collapse to one product

// Filler/qualifier words that appear in NL queries but never in product titles
// ("travel tripod UNDER $400"). Dropped so they don't starve the match.
const STOPWORDS = new Set([
    'under', 'over', 'below', 'above', 'less', 'than', 'with', 'and', 'the',
    'for', 'best', 'top', 'cheap', 'cheapest', 'budget', 'in', 'on', 'of', 'a',
    'to', 'or', 'my', 'me', 'buy', 'new', 'used', 'vs',
]);

// Model-suffix tokens that get glued onto a model root in the wild ("a7iv",
// "r6ii", "a7m4"). Split so they match the spaced form. Deliberately EXCLUDES
// bare single letters (so "a7r" is NEVER split into "a7"+"r" — a7r is its own
// model, distinct from a7).
const GLUED_SUFFIX = /^([a-z]?[a-z]*\d+)(ii|iii|iv|vi|v|m\d+|mark\d+)$/;

// A token is a MODEL token if it carries a digit (a7, r6, 90d, 50mm, m4) or is a
// roman-numeral generation marker (ii..vi). These are the identity of the product
// and must all be present; descriptor words are soft.
const ROMAN = new Set(['ii', 'iii', 'iv', 'v', 'vi']);

function rawTokens(s) {
    return ((s || '').toLowerCase().match(/[a-z0-9]+/g)) || [];
}

// Tokenize AND split glued model suffixes, applied to both queries and titles so
// "a7iv" (title) and "a7 iv" (query) land on the same tokens.
function tokens(s) {
    const out = [];
    for (const t of rawTokens(s)) {
        const m = t.match(GLUED_SUFFIX);
        if (m) { out.push(m[1], m[2]); } else { out.push(t); }
    }
    return out;
}

function isModelToken(t) {
    // an alphanumeric model code (a7, r6, 90d, 50mm, m4) or a roman generation
    // marker (ii..vi). A PURE integer is NOT a model token — it's a price/year
    // ("tripod under 400", "2023 version") and must never become a hard gate.
    return (/[a-z]/.test(t) && /\d/.test(t)) || ROMAN.has(t);
}

// Split query tokens into the mandatory model set and the soft descriptor set.
function splitQuery(query) {
    const qt = tokens(query).filter(t => !STOPWORDS.has(t));
    const model = [], descriptor = [];
    for (const t of qt) {
        if (/^\d+$/.test(t)) continue;               // pure integer: price/year noise
        if (isModelToken(t)) model.push(t); else descriptor.push(t);
    }
    return { model, descriptor };
}

// Levenshtein <= 1 (fast path): equal, or exactly one insert/delete/substitute.
function within1(a, b) {
    if (a === b) return true;
    const la = a.length, lb = b.length;
    if (Math.abs(la - lb) > 1) return false;
    if (la === lb) {           // one substitution
        let diff = 0;
        for (let i = 0; i < la; i++) if (a[i] !== b[i] && ++diff > 1) return false;
        return diff === 1;
    }
    // one insert/delete: walk the shorter against the longer
    const [s, l] = la < lb ? [a, b] : [b, a];
    let i = 0, j = 0, edits = 0;
    while (i < s.length && j < l.length) {
        if (s[i] === l[j]) { i++; j++; }
        else { if (++edits > 1) return false; j++; }
    }
    return true;
}

function modelMatch(qt, nameTokens) {
    // EXACT whole-token only — no plural, no fuzz. Model codes are never
    // pluralized, and a one-char slip is a DIFFERENT product: a7 must not match
    // a7r/a7s, iv must not match vi. This exactness is the variant firewall.
    return nameTokens.includes(qt);
}

function descriptorMatch(qt, nameTokens) {
    // exact OR a single-edit typo (only for tokens long enough that a 1-edit
    // neighbourhood is meaningful — keeps short brand fragments strict).
    return nameTokens.some(nt =>
        nt === qt || nt === qt + 's' || qt === nt + 's' ||
        (qt.length >= 4 && nt.length >= 4 && within1(qt, nt)));
}

/**
 * Relevance score for one title. Returns -1 to REJECT (a required model token is
 * absent), else a non-negative score (higher = better). Descriptor-only queries
 * (no model token) fall back to token coverage so NL/category never goes empty.
 */
function score(model, descriptor, name) {
    const nt = tokens(name);
    if (nt.length === 0) return -1;

    if (model.length > 0) {
        for (const mt of model) if (!modelMatch(mt, nt)) return -1;  // hard gate
        // all model tokens present; rank by how many descriptors also land
        let s = model.length * 10;
        for (const dt of descriptor) if (descriptorMatch(dt, nt)) s += 1;
        return s;
    }

    // No model token in the query → coverage over descriptors, keep >=1 match.
    let hits = 0;
    for (const dt of descriptor) if (descriptorMatch(dt, nt)) hits++;
    return hits > 0 ? hits : -1;
}

/**
 * Model-anchored relevance of `text` to `query`, exported so card matching
 * (cards.js) uses the SAME logic as product ranking — every model token must
 * match exactly (r6ii/a7iv normalized first), so a query surfaces only the right
 * generation. Returns -1 to reject, else a non-negative score (higher = better).
 */
export function relevance(query, text) {
    const { model, descriptor } = splitQuery(query);
    if (model.length === 0 && descriptor.length === 0) return -1;
    return score(model, descriptor, text);
}

function isAccessory(name) {
    const l = (name || '').toLowerCase();
    return ACCESSORY_MARKERS.some(re => re.test(l));
}

function isAdorama(p) {
    return p.source === 'Adorama' || p.seller === 'Adorama';
}

function conditionClass(p) {
    return (p.condition || '').toString().trim().toLowerCase() === 'new' ? 'new' : 'used';
}

function priceFloat(p) {
    const digits = (p == null ? '' : String(p)).replace(/[^0-9.]/g, '');
    const v = parseFloat(digits);
    return isNaN(v) ? Infinity : v;
}

function formatPrice(v) {
    if (v == null || !isFinite(v)) return null;
    return '$' + Math.round(v).toLocaleString('en-US');
}

// Jaccard over title token sets.
function jaccard(aTok, bTok) {
    if (!aTok.size && !bTok.size) return 1;
    if (!aTok.size || !bTok.size) return 0;
    let inter = 0;
    for (const t of aTok) if (bTok.has(t)) inter++;
    return inter / (aTok.size + bTok.size - inter);
}

/**
 * Merge near-identical listings of one product into a single card. Preserves the
 * cheapest New (else cheapest overall) as the representative, attaches every
 * listing as `sources` (so the UI shows "N sources"), and sets `bestPrice`.
 */
function dedup(scored) {
    const clusters = [];  // {tok, rows:[{p,s}]}
    for (const item of scored) {
        const tok = new Set(tokens(item.p.name));
        let placed = false;
        for (const c of clusters) {
            if (jaccard(tok, c.tok) >= JACCARD_THRESHOLD) { c.rows.push(item); placed = true; break; }
        }
        if (!placed) clusters.push({ tok, rows: [item] });
    }
    return clusters.map(c => {
        // representative: New first, then cheapest
        const ordered = c.rows.slice().sort((a, b) => {
            const an = conditionClass(a.p) === 'new' ? 0 : 1;
            const bn = conditionClass(b.p) === 'new' ? 0 : 1;
            if (an !== bn) return an - bn;
            return priceFloat(a.p.price) - priceFloat(b.p.price);
        });
        const rep = { ...ordered[0].p };
        const prices = c.rows.map(r => priceFloat(r.p.price)).filter(isFinite);
        const best = prices.length ? Math.min(...prices) : null;
        if (c.rows.length > 1) {
            rep.sources = ordered.map(r => ({
                url: r.p.url, price: r.p.price, condition: r.p.condition,
                seller: r.p.seller, source: r.p.source,
            }));
            rep.bestPrice = best != null ? 'from ' + formatPrice(best) : rep.price;
        } else if (best != null) {
            rep.bestPrice = formatPrice(best);
        }
        rep._score = ordered[0].s;
        rep._new = conditionClass(rep) === 'new' ? 0 : 1;
        rep._price = best != null ? best : priceFloat(rep.price);
        return rep;
    });
}

/**
 * Arrange the accumulated results for display. Pure; safe to call on every source
 * arrival. Returns the ordered product list (deduped canonical, best-first, then
 * a capped compatible/third-party tail).
 */
export function arrangeResults(query, products, opts = {}) {
    const usedCap = opts.usedCap != null ? opts.usedCap : USED_CANONICAL_CAP;
    const tailCap = opts.tailCap != null ? opts.tailCap : ACCESSORY_TAIL_CAP;
    const { model, descriptor } = splitQuery(query);
    if (model.length === 0 && descriptor.length === 0) return products.slice();

    const canon = [], acc = [];
    for (const p of products) {
        const s = score(model, descriptor, p.name);
        if (s < 0) continue;                       // rejected: missing a model token
        (isAccessory(p.name) ? acc : canon).push({ p, s });
    }

    // Dedup + rank canonical: score desc, New before Used, price asc.
    let merged = dedup(canon).sort((a, b) =>
        (b._score - a._score) || (a._new - b._new) || (a._price - b._price));

    // Cap Used so eBay's volume can't bury the shelf after New leads.
    const used = merged.filter(p => p._new === 1);
    if (used.length > usedCap) {
        const keepUsed = new Set(used.slice(0, usedCap));
        merged = merged.filter(p => p._new === 0 || keepUsed.has(p));
    }

    const tail = acc.sort((a, b) => b.s - a.s)
        .slice(0, tailCap)
        .map(x => ({ ...x.p, bestPrice: formatPrice(priceFloat(x.p.price)) || x.p.price }));

    return merged.concat(tail);
}
