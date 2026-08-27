/**
 * Lane A precision — client-side, cheap, applied AS results stream in.
 * ==================================================================
 * The heavy Sieve rungs (spine identity-resolve, Qwen3 arbitration) are OUT of
 * the hot path. What's left is cheap and runs in the browser over the combined
 * eBay+Adorama results on every source arrival, so precision never blocks the
 * stream:
 *   - relevance: WHOLE-TOKEN match (query "a7 iv" matches "a7"/"A7 IV", never "a7r")
 *   - classify:  accessory markers ("for", "compatible", …) demote to a capped tail
 *   - arrange:   source x condition round-robin (Adorama-New, eBay-New, eBay-Used,
 *                repeat) so New leads with a Used comparison up top
 * Mirrors gateway/search_cells.py (duplicate, don't import — the browser has no
 * python). Keep the two in sync when either changes.
 */

const ACCESSORY_MARKERS = [
    /\bfor\b/, /\bcompatible\b/, /\bfits\b/, /\breplacement\b/,
    /\bfor\s+use\s+with\b/, /\baftermarket\b/,
];
const USED_LANE_CAP = 10;   // cap Used listings so eBay volume can't flood
const ACCESSORY_TAIL_CAP = 8;

// Filler/qualifier words that appear in NL queries but never in product titles
// ("travel tripod UNDER $400"). Dropped so they don't starve the match.
const STOPWORDS = new Set([
    'under', 'over', 'below', 'above', 'less', 'than', 'with', 'and', 'the',
    'for', 'best', 'top', 'cheap', 'cheapest', 'budget', 'in', 'on', 'of', 'a',
    'to', 'or', 'my', 'me', 'buy', 'new', 'used', 'vs',
]);

function tokens(s) {
    return ((s || '').toLowerCase().match(/[a-z0-9]+/g)) || [];
}

function queryTokens(query) {
    return tokens(query).filter(t => !STOPWORDS.has(t) && !/^\d+$/.test(t));
}

function tmatch(qt, nt) {
    // equality with simple singular/plural tolerance; never a substring merge,
    // so "a7" never matches "a7r".
    if (qt === nt) return true;
    return qt === nt + 's' || nt === qt + 's' || qt === nt + 'es' || nt === qt + 'es';
}

function relevanceScore(qTokens, name) {
    const nt = tokens(name);
    let s = 0;
    for (const qt of qTokens) if (nt.some(n => tmatch(qt, n))) s++;
    return s;
}

function isAccessory(name) {
    const l = (name || '').toLowerCase();
    return ACCESSORY_MARKERS.some(re => re.test(l));
}

function isAdorama(p) {
    return p.source === 'Adorama' || p.seller === 'Adorama';
}

function conditionClass(p) {
    return (p.condition || '').toString().toLowerCase() === 'new' ? 'new' : 'used';
}

function roundRobin(lanes) {
    const out = [];
    const idx = lanes.map(() => 0);
    let remaining = lanes.reduce((a, l) => a + l.length, 0);
    while (remaining) {
        for (let k = 0; k < lanes.length; k++) {
            if (idx[k] < lanes[k].length) {
                out.push(lanes[k][idx[k]++]);
                remaining--;
            }
        }
    }
    return out;
}

/**
 * Arrange the accumulated results for display. Pure; safe to call on every
 * source arrival. Returns the ordered product list (canonical interleave first,
 * then a capped compatible/third-party tail).
 */
export function arrangeResults(query, products) {
    const q = queryTokens(query);
    if (q.length === 0) return products.slice();

    // Coverage relevance: keep anything matching >=1 query token, rank by how
    // many matched. Handles NL/category queries ("travel tripod under $400")
    // without going empty, while whole-token matching keeps "a7 iv" off "a7r".
    const scored = products
        .map(p => ({ p, s: relevanceScore(q, p.name) }))
        .filter(x => x.s > 0);

    const canonical = scored.filter(x => !isAccessory(x.p.name));
    const accessory = scored.filter(x => isAccessory(x.p.name));

    const lane = (adorama, cond) => canonical
        .filter(x => isAdorama(x.p) === adorama && conditionClass(x.p) === cond)
        .sort((a, b) => b.s - a.s)          // best matches lead within the lane
        .map(x => x.p);
    const lanes = [
        lane(true, 'new'),                       // Adorama New
        lane(false, 'new'),                      // eBay New
        lane(true, 'used'),                      // Adorama Used (empty in practice)
        lane(false, 'used').slice(0, USED_LANE_CAP), // eBay Used (capped)
    ];

    const tail = accessory.sort((a, b) => b.s - a.s)
        .slice(0, ACCESSORY_TAIL_CAP).map(x => x.p);
    return roundRobin(lanes).concat(tail);
}
