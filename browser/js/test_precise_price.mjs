/**
 * Node smoke test for the precise.js price rung (parity with
 * gateway/search_cells.py + phantom-ops ingest/search_price.py).
 * Run: node browser/js/test_precise_price.mjs
 */
import {
    parsePriceConstraint, applyPriceFilter, priceConstraintLabel, arrangeResults,
} from './precise.js';

let fails = 0;
function eq(got, want, msg) {
    const g = JSON.stringify(got), w = JSON.stringify(want);
    if (g !== w) { console.error(`FAIL ${msg}\n  got  ${g}\n  want ${w}`); fails++; }
}

// --- parse ------------------------------------------------------------------
eq(parsePriceConstraint('mirrorless under $400'), { op: 'lte', value: 400 }, 'under $400');
eq(parsePriceConstraint('under 2k'), { op: 'lte', value: 2000 }, 'k suffix');
eq(parsePriceConstraint('below $1,500'), { op: 'lte', value: 1500 }, 'comma');
eq(parsePriceConstraint('lens <= 800'), { op: 'lte', value: 800 }, '<= symbol');
eq(parsePriceConstraint('pro body over 3000'), { op: 'gte', value: 3000 }, 'over');
eq(parsePriceConstraint('between 200 and 500'), { op: 'range', lo: 200, hi: 500 }, 'between');
eq(parsePriceConstraint('$800 to $1,200 lens'), { op: 'range', lo: 800, hi: 1200 }, 'to range');
eq(parsePriceConstraint('gimbal $300-$450'), { op: 'range', lo: 300, hi: 450 }, 'dash range');
eq(parsePriceConstraint('between 500 and 200'), { op: 'range', lo: 200, hi: 500 }, 'range orders asc');
// negatives + focal-notation guard
eq(parsePriceConstraint('sony a7 iv mirrorless'), null, 'no constraint');
eq(parsePriceConstraint('canon 90d'), null, 'bare model number');
eq(parsePriceConstraint('24-70mm lens'), null, 'focal dash not price');
eq(parsePriceConstraint('24 to 70mm f/2.8'), null, 'focal to not price');
eq(parsePriceConstraint(''), null, 'empty');

// --- filter -----------------------------------------------------------------
const rows = [
    { name: 'A', price: '$300' }, { name: 'B', price: '450.00' },
    { name: 'C', price: '$1,200' }, { name: 'D' },
];
eq(applyPriceFilter(rows, { op: 'lte', value: 500 }).kept.map(r => r.name), ['A', 'B', 'D'], 'lte keeps unpriced');
eq(applyPriceFilter(rows, { op: 'lte', value: 500 }).filtered.map(r => r.name), ['C'], 'lte filtered');
eq(applyPriceFilter(rows, { op: 'gte', value: 400 }).kept.map(r => r.name), ['B', 'C', 'D'], 'gte');
eq(applyPriceFilter(rows, { op: 'range', lo: 400, hi: 1000 }).kept.map(r => r.name), ['B', 'D'], 'range');
eq(applyPriceFilter(rows, null).kept.length, 4, 'null passthrough');
// _price (dedup best price) wins over display price
eq(applyPriceFilter([{ name: 'X', price: '$5000', _price: 350 }], { op: 'lte', value: 500 }).kept.length, 1, 'uses _price');

// --- labels -----------------------------------------------------------------
eq(priceConstraintLabel({ op: 'lte', value: 400 }), 'Under $400', 'label lte');
eq(priceConstraintLabel({ op: 'gte', value: 3000 }), 'Over $3,000', 'label gte');
eq(priceConstraintLabel({ op: 'range', lo: 800, hi: 1200 }), '$800–$1,200', 'label range');

// --- arrangeResults end-to-end (filter applied; ignorePrice bypasses) -------
const prods = [{ name: 'Sony A7 IV Body', price: '$2999', condition: 'New', seller: 'eBay' }];
eq(arrangeResults('sony a7 iv under $2500', prods).length, 0, 'arrange filters >budget');
eq(arrangeResults('sony a7 iv under $2500', prods, { ignorePrice: true }).length, 1, 'ignorePrice bypasses');

if (fails) { console.error(`\n${fails} FAILED`); process.exit(1); }
console.log('all precise price tests passed');
