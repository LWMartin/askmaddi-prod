/* AskMaddi — Review Card Grid Renderer
 * Loads cards-manifest.json and renders teaser cards on the homepage.
 * Also exports matching utilities for search results integration (Phase 5).
 */

const MANIFEST_URL = 'cards-manifest.json';
let _manifestCache = null;

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
 * Axes may carry a `role` (most_discussed / highest_rated / biggest_gripe)
 * rendered as a micro-label above the bar. Entries without a role (older
 * manifest entries, sparse-card fallback fills) render exactly as before.
 */
const AXIS_ROLE_LABELS = {
    most_discussed: ['most discussed', 'role-volume'],
    highest_rated: ['highest rated', 'role-high'],
    biggest_gripe: ['biggest gripe', 'role-low'],
};

function renderAxisBar(axis) {
    const { pos, neg, total } = axis;
    if (!total) return '';
    const posPct = Math.round((pos / total) * 100);
    const negPct = Math.round((neg / total) * 100);

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
                <span class="axis-pct">${posPct}%</span>
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
function renderTeaserCard(card) {
    const axes = (card.top_axes || []).slice(0, 3).map(renderAxisBar).join('');

    const imageContent = card.image_thumb
        ? `<img src="${card.image_thumb}" alt="${card.display_name}" loading="lazy" onerror="this.style.display='none';this.nextElementSibling.style.display='block'"><div class="image-placeholder" style="display:none">📷</div>`
        : `<div class="image-placeholder">📷</div>`;

    const newLabel = formatPrice(card.pricing?.new_price);
    const usedLabel = formatPrice(card.pricing?.used_price);

    const newUrl = card.pricing?.new_url || '#';
    const usedUrl = card.pricing?.used_url || '#';

    return `
        <article class="teaser-card">
            <div class="card-image">
                ${imageContent}
                <span class="source-badge">${card.source_count} reviews</span>
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

    // Show the section (hidden by default if no cards)
    const section = container.closest('.review-cards');
    if (section) section.style.display = 'block';

    container.innerHTML = manifest.cards.map(renderTeaserCard).join('');
}

/**
 * Match a search query against card identity fields.
 * Returns matching cards, sorted by relevance (token match count).
 */
export function matchCards(query, manifest) {
    if (!manifest || !manifest.cards || !query) return [];

    const tokens = query.toLowerCase().split(/\s+/).filter(t => t.length > 1);
    if (tokens.length === 0) return [];

    const scored = manifest.cards.map(card => {
        const haystack = [
            card.display_name,
            card.brand,
            card.category,
            card.subcategory
        ].join(' ').toLowerCase();

        const matches = tokens.filter(t => haystack.includes(t));
        return { card, score: matches.length };
    });

    return scored
        .filter(s => s.score >= Math.min(2, tokens.length))
        .sort((a, b) => b.score - a.score)
        .map(s => s.card);
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
