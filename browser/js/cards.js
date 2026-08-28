/* AskMaddi — Review Card Grid Renderer
 * Loads cards-manifest.json and renders teaser cards on the homepage.
 * Also exports matching utilities for search results integration (Phase 5).
 */

import { relevance } from './precise.js?v=4';

const MANIFEST_URL = 'cards-manifest.json';
let _manifestCache = null;

// Category display metadata for the anchor nav + section headings. Keyed by the
// card's own lowercase category. Order here is only a fallback — the buckets
// follow the manifest, which build_site already groups body -> lens -> support.
const CATEGORY_META = {
    body: { id: 'cat-bodies', label: 'Bodies' },
    lens: { id: 'cat-lenses', label: 'Lenses' },
    support: { id: 'cat-support', label: 'Support' },
};

/**
 * Load the card manifest (cached after first fetch).
 */
export async function loadManifest() {
    if (_manifestCache) return _manifestCache;
    try {
        const resp = await fetch(MANIFEST_URL);
        if (!resp.ok) return null;
        _manifestCache = await resp.json();
        return _manifestCache;
    } catch (e) {
        console.warn('Cards manifest not available:', e.message);
        return null;
    }
}

/**
 * Render a single sentiment axis bar.
 * Axes may carry a `role` (most_discussed / highest_rated / lowest_rated)
 * rendered as a micro-label above the bar. Entries without a role (older
 * manifest entries, sparse-card fallback fills) render exactly as before.
 */
const AXIS_ROLE_LABELS = {
    most_discussed: ['most discussed', 'role-volume'],
    highest_rated: ['highest rated', 'role-high'],
    // Renamed from biggest_gripe 2026-07-28: the role is the low end of the
    // same positive-share measure as highest_rated, not a criticism claim.
    lowest_rated: ['lowest rated', 'role-low'],
};

function renderAxisBar(axis) {
    const { pos, neg, total } = axis;
    if (!total) return '';
    // neu arrives on the manifest from 2026-07-28; older entries are derived
    // rather than assumed away, so the triple is always complete.
    const neu = axis.neu != null ? axis.neu : Math.max(0, total - pos - neg);
    const posPct = Math.round((pos / total) * 100);
    const negPct = Math.round((neg / total) * 100);
    const neuPct = Math.max(0, 100 - posPct - negPct);
    // The visible figure is a positive share in a 36px slot. It travels with
    // its siblings in the accessible label so the number is never a bare,
    // unqualified share -- same rule the card body follows.
    const grounded = `${posPct}% positive, ${neuPct}% neutral, ${negPct}% negative of ${total} claims`;

    const roleMeta = AXIS_ROLE_LABELS[axis.role];
    const roleHtml = roleMeta
        ? `<span class="axis-role ${roleMeta[1]}">${roleMeta[0]}</span>`
        : '';

    return `
        <div class="axis-group">
            ${roleHtml}
            <div class="axis-row">
                <span class="axis-label">${axis.axis}</span>
                <div class="axis-bar">
                    <div class="bar-pos" style="width: ${posPct}%"></div>
                    <div class="bar-neg" style="width: ${negPct}%"></div>
                </div>
                <span class="axis-pct" title="${grounded}" aria-label="${grounded}">${posPct}%</span>
            </div>
        </div>
    `;
}

/**
 * Format price for display. Returns "Check price" if zero/null.
 */
function formatPrice(price) {
    if (!price || price <= 0) return 'Check price';
    return '$' + price.toLocaleString('en-US', { maximumFractionDigits: 0 });
}

/**
 * Render a single teaser card.
 */
function renderTeaserCard(card, opts = {}) {
    const axes = (card.top_axes || []).slice(0, 3).map(renderAxisBar).join('');
    const heroClass = opts.hero ? ' teaser-card--hero' : '';
    const heroBadge = opts.hero ? '<span class="hero-badge">★ Latest</span>' : '';

    const imageContent = card.image_thumb
        ? `<img src="${card.image_thumb}" alt="${card.display_name}" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='block'"><div class="image-placeholder" style="display:none">📷</div>`
        : `<div class="image-placeholder">📷</div>`;

    const newLabel = formatPrice(card.pricing?.new_price);
    const usedLabel = formatPrice(card.pricing?.used_price);

    const newUrl = card.pricing?.new_url || '#';
    const usedUrl = card.pricing?.used_url || '#';

    // Amazon rung — omitted entirely when the build emitted no URL (absent-list
    // card). The label is a CONSTANT: we hold no Creators API credentials, so
    // Amazon price / rating / review count may not be displayed. Never swap
    // this for formatPrice(...) — see the doctrine block in build_site.py.
    const amazonUrl = card.pricing?.amazon_url || '';
    const amazonBtn = amazonUrl
        ? `<a href="${amazonUrl}" target="_blank" rel="nofollow noopener sponsored"
              data-out data-retailer="amazon" class="btn-affiliate btn-buy-amazon">
               See price on Amazon
           </a>`
        : '';

    return `
        <article class="teaser-card${heroClass}">
            ${heroBadge}
            <div class="card-image">
                ${imageContent}
                <a href="${card.card_url || '#'}#sources" class="source-badge" title="See all ${card.source_count} source reviews">${card.source_count} reviews</a>
            </div>
            <div class="card-body">
                <h3 class="product-name">${card.display_name}</h3>
                <p class="product-meta">${card.brand} · ${card.category} · ${card.subcategory}</p>

                <div class="axes-list">
                    ${axes}
                </div>

                <div class="card-actions">
                    <a href="${newUrl}" target="_blank" rel="noopener" class="btn-affiliate btn-buy-new">
                        ${newLabel} new
                    </a>
                    <a href="${usedUrl}" target="_blank" rel="noopener" class="btn-affiliate btn-buy-used">
                        ${usedLabel} used
                    </a>
                    ${amazonBtn}
                </div>
            </div>
            <a href="${card.card_url || '#'}" class="card-link">View full review card →</a>
        </article>
    `;
}

/**
 * Render the card grid into a container element.
 */
export async function renderCardGrid(containerSelector) {
    const manifest = await loadManifest();
    if (!manifest || !manifest.cards || manifest.cards.length === 0) return;

    const container = document.querySelector(containerSelector);
    if (!container) return;

    // Own the whole review-cards region: the first (newest) card is pulled out
    // as a full-width hero, the rest are grouped into category sections with an
    // anchor nav. Hidden by default until we have cards.
    const section = container.closest('.review-cards');
    if (!section) return;
    section.style.display = 'block';

    const cards = manifest.cards;
    const hero = cards[0];
    const rest = cards.slice(1);

    // Bucket the rest by category, preserving the manifest order (build_site
    // already grouped body -> lens -> support, newest-first within each).
    const buckets = [];
    const byKey = {};
    for (const card of rest) {
        const key = (card.category || 'other').toLowerCase();
        const meta = CATEGORY_META[key]
            || { id: 'cat-' + key.replace(/\s+/g, '-'), label: (card.category || 'Other') };
        if (!byKey[key]) {
            byKey[key] = { key, id: meta.id, label: meta.label, cards: [] };
            buckets.push(byKey[key]);
        }
        byKey[key].cards.push(card);
    }

    // Nav only earns its space when there's more than one section to jump to.
    const navHtml = buckets.length > 1
        ? `<nav class="cat-nav" aria-label="Jump to category">
               ${buckets.map(b =>
                   `<a href="#${b.id}" data-cat="${b.id}">${b.label}<span class="cat-count">${b.cards.length}</span></a>`
               ).join('')}
           </nav>`
        : '';

    const heroHtml = `
        <div class="hero-feature">${renderTeaserCard(hero, { hero: true })}</div>
        <hr class="hero-rule">`;

    const sectionsHtml = buckets.map(b => `
        <section class="cat-section" id="${b.id}">
            <h3 class="cat-heading">${b.label}</h3>
            <div class="card-grid">${b.cards.map(c => renderTeaserCard(c)).join('')}</div>
        </section>`).join('');

    section.innerHTML =
        '<h2 class="section-heading">Recent review comparisons</h2>'
        + navHtml + heroHtml + sectionsHtml;

    _initScrollSpy(section);
    _initToTop();
}

// Highlight the nav link for the category section currently in view.
function _initScrollSpy(section) {
    const nav = section.querySelector('.cat-nav');
    if (!nav || !('IntersectionObserver' in window)) return;
    const links = new Map();
    nav.querySelectorAll('a[data-cat]').forEach(a => links.set(a.dataset.cat, a));
    const obs = new IntersectionObserver((entries) => {
        entries.forEach(e => {
            if (!e.isIntersecting) return;
            links.forEach(a => a.classList.remove('active'));
            const a = links.get(e.target.id);
            if (a) a.classList.add('active');
        });
    }, { rootMargin: '-40% 0px -55% 0px', threshold: 0 });
    section.querySelectorAll('.cat-section').forEach(s => obs.observe(s));
}

// A floating back-to-top button, injected once and shown past a scroll threshold.
function _initToTop() {
    let btn = document.getElementById('to-top');
    if (!btn) {
        btn = document.createElement('button');
        btn.id = 'to-top';
        btn.className = 'to-top';
        btn.type = 'button';
        btn.setAttribute('aria-label', 'Back to top');
        btn.innerHTML = '↑';
        btn.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));
        document.body.appendChild(btn);
    }
    const onScroll = () => btn.classList.toggle('visible', window.scrollY > 400);
    window.addEventListener('scroll', onScroll, { passive: true });
    onScroll();
}

/**
 * Match a search query against card identity fields, using the SAME
 * model-anchored relevance as the marketplace results (precise.js `relevance`):
 * every model token must match exactly and glued forms are normalized, so
 * "r6ii" and "r6 ii" both surface the Canon R6 II card — and only the R6 II,
 * not the R6 III. Returns matching cards, best-first.
 */
export function matchCards(query, manifest) {
    if (!manifest || !manifest.cards || !query) return [];

    const scored = manifest.cards.map(card => {
        const haystack = [
            card.display_name,
            card.brand,
            card.category,
            card.subcategory
        ].filter(Boolean).join(' ');
        return { card, s: relevance(query, haystack) };
    });

    return scored
        .filter(x => x.s > 0)          // relevance() rejects with -1
        .sort((a, b) => b.s - a.s)
        .map(x => x.card);
}

/**
 * Render matched cards in the search results area.
 * Call this from the results state when cards match the query.
 */
export function renderMatchedCards(cards, containerSelector) {
    const container = document.querySelector(containerSelector);
    if (!container || !cards.length) {
        if (container) container.style.display = 'none';
        return;
    }

    container.style.display = 'block';
    container.innerHTML = `
        <h3 class="section-heading">★ AskMaddi Review Cards</h3>
        <div class="card-grid">
            ${cards.map(renderTeaserCard).join('')}
        </div>
    `;
}
