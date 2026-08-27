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

function tokens(s) {
    return ((s || '').toLowerCase().match(/[a-z0-9]+/g)) || [];
}

function matchesQuery(queryTokens, name) {
    // whole-token: every query token must equal some name token
    const nt = new Set(tokens(name));
    return queryTokens.every(qt => nt.has(qt));
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
    const q = tokens(query);
    if (q.length === 0) return products.slice();

    // relevance gate (whole-token) — drops a7R for "a7 iv" and unrelated junk
    const relevant = products.filter(p => matchesQuery(q, p.name));
    const canonical = relevant.filter(p => !isAccessory(p.name));
    const accessory = relevant.filter(p => isAccessory(p.name));

    const lane = (adorama, cond) =>
        canonical.filter(p => isAdorama(p) === adorama && conditionClass(p) === cond);
    const lanes = [
        lane(true, 'new'),                       // Adorama New
        lane(false, 'new'),                      // eBay New
        lane(true, 'used'),                      // Adorama Used (empty in practice)
        lane(false, 'used').slice(0, USED_LANE_CAP), // eBay Used (capped)
    ];

    return roundRobin(lanes).concat(accessory.slice(0, ACCESSORY_TAIL_CAP));
}
