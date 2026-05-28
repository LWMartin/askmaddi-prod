/**
 * UI Module — v0.2 Streaming Results
 * ====================================
 * Supports incremental result rendering as sources complete.
 */

import { affiliateManager } from './affiliate.js';

class UI {
    constructor() {
        this.states = {
            landing: document.getElementById('state-landing'),
            loading: document.getElementById('state-loading'),
            results: document.getElementById('state-results'),
            error: document.getElementById('state-error'),
            setup: document.getElementById('state-setup')
        };
    }
    
    showState(stateName) {
        Object.values(this.states).forEach(el => el.classList.remove('active'));
        if (this.states[stateName]) this.states[stateName].classList.add('active');
    }
    
    updateSetupProgress(percent, statusText) {
        const fill = document.getElementById('setup-progress-fill');
        const status = document.getElementById('setup-status');
        if (fill) fill.style.width = `${percent}%`;
        if (status) status.textContent = statusText;
    }
    
    // --- Legacy loading state methods (kept for compatibility) ---
    
    updateLoadingStatus(sources) {
        const container = document.getElementById('loading-status');
        if (!container) return;
        container.innerHTML = sources.map(source => 
            `<span class="source" data-source="${source.name}">${source.name}...</span>`
        ).join('');
    }
    
    updateSourceStatus(sourceName, status) {
        const el = document.querySelector(`[data-source="${sourceName}"]`);
        if (!el) return;
        el.classList.remove('done', 'error');
        if (status === 'done') {
            el.classList.add('done');
            el.textContent = sourceName;
        } else if (status === 'error') {
            el.classList.add('error');
            el.textContent = `${sourceName}: failed`;
        } else {
            el.textContent = `${sourceName}: ${status}...`;
        }
    }
    
    // --- Streaming Results (v0.2) ---
    
    /**
     * Initialize the results view for streaming.
     * Called immediately when search starts — before any results arrive.
     */
    initStreamingResults(query, sourceCount) {
        document.getElementById('search-echo').textContent = query;
        document.getElementById('result-count').textContent = '...';
        document.getElementById('sources-checked').textContent = `Searching ${sourceCount} source${sourceCount !== 1 ? 's' : ''}...`;
        document.getElementById('dupes-hidden').textContent = '';
        
        const grid = document.getElementById('results-grid');
        grid.innerHTML = `
            <div id="streaming-indicator" style="grid-column: 1/-1; padding: 1.5rem; text-align: center;">
                <div class="spinner" style="width: 32px; height: 32px; margin: 0 auto 0.75rem;"></div>
                <p style="color: var(--color-text-muted, #999); font-size: 14px;" id="streaming-status">
                    Fetching product listings...
                </p>
            </div>
        `;
    }
    
    /**
     * Update per-source status during streaming.
     */
    updateStreamingSource(sourceName, status) {
        const statusEl = document.getElementById('streaming-status');
        if (!statusEl) return;
        
        if (status === 'fetching') {
            statusEl.textContent = `Fetching from ${sourceName}...`;
        } else if (status === 'extracting') {
            statusEl.textContent = `Extracting products from ${sourceName}...`;
        } else if (status === 'done') {
            statusEl.textContent = `${sourceName} complete`;
        } else if (status === 'error') {
            statusEl.textContent = `${sourceName} unavailable, continuing...`;
        }
    }
    
    /**
     * Update progress counter as sources complete.
     */
    updateStreamingProgress(complete, total) {
        document.getElementById('sources-checked').textContent = 
            complete >= total 
                ? `Checked ${total} source${total !== 1 ? 's' : ''}` 
                : `Checked ${complete} of ${total} sources...`;
    }
    
    /**
     * Replace the product grid with a new set of ranked products.
     * Called each time a source completes and adds products.
     */
    replaceProductGrid(products, query) {
        const grid = document.getElementById('results-grid');
        
        // Remove the streaming indicator if it's still there
        const indicator = document.getElementById('streaming-indicator');
        if (indicator) indicator.remove();
        
        // Update count
        document.getElementById('result-count').textContent = products.length;
        
        // Render all products (re-render on each source completion for proper ranking)
        grid.innerHTML = products.map(product => this.renderProductCard(product)).join('');
        
        this.attachClickHandlers();
    }
    
    /**
     * Final cleanup after all sources complete.
     */
    finalizeResults(finalCount, totalFound, afterDedup) {
        document.getElementById('result-count').textContent = finalCount;
        
        const dupeCount = totalFound - afterDedup;
        document.getElementById('dupes-hidden').textContent = 
            dupeCount > 0 ? `${dupeCount} duplicate${dupeCount !== 1 ? 's' : ''} hidden` : '';
    }
    
    /**
     * Show empty results with diagnostics.
     */
    showEmptyResults(diagnostics) {
        const grid = document.getElementById('results-grid');
        document.getElementById('result-count').textContent = '0';
        
        // Remove streaming indicator
        const indicator = document.getElementById('streaming-indicator');
        if (indicator) indicator.remove();
        
        if (diagnostics) {
            const diagLines = Object.entries(diagnostics).map(([site, d]) => {
                if (d.status === 'error') return `${site}: fetch error — ${d.error}`;
                if (d.htmlBytes === 0) return `${site}: empty response (0 bytes)`;
                if (d.htmlBytes < 5000) return `${site}: small response (${d.htmlBytes} bytes — possible block)`;
                return `${site}: got ${d.htmlBytes} bytes but extracted 0 products`;
            });
            grid.innerHTML = `<div style="grid-column: 1/-1; padding: 2rem; text-align: center; color: var(--color-text-muted, #666);">
                <p style="margin-bottom: 1rem;">No products found from retailer sources.</p>
                <pre style="text-align: left; font-size: 0.85rem; max-width: 500px; margin: 0 auto;">${diagLines.join('\n')}</pre>
            </div>`;
        }
    }
    
    // --- Legacy displayResults (kept as fallback) ---
    
    displayResults(results) {
        document.getElementById('result-count').textContent = results.products.length;
        document.getElementById('search-echo').textContent = results.query;
        document.getElementById('sources-checked').textContent = `Checked ${results.sourcesChecked} sources`;
        
        const dupeCount = results.totalFound - results.afterDedup;
        document.getElementById('dupes-hidden').textContent = dupeCount > 0 ? `${dupeCount} duplicates hidden` : '';
        
        const grid = document.getElementById('results-grid');
        
        if (results.products.length === 0 && results.diagnostics) {
            const diagLines = Object.entries(results.diagnostics).map(([site, d]) => {
                if (d.status === 'error') return `${site}: fetch error — ${d.error}`;
                if (d.htmlBytes === 0) return `${site}: empty response (0 bytes)`;
                if (d.htmlBytes < 5000) return `${site}: suspiciously small response (${d.htmlBytes} bytes)`;
                return `${site}: got ${d.htmlBytes} bytes but extracted 0 products`;
            });
            grid.innerHTML = `<div style="grid-column: 1/-1; padding: 2rem; text-align: center; color: var(--color-text-muted, #666);">
                <p style="margin-bottom: 1rem;">No products found. Diagnostic info:</p>
                <pre style="text-align: left; font-size: 0.85rem; max-width: 500px; margin: 0 auto;">${diagLines.join('\n')}</pre>
            </div>`;
        } else {
            grid.innerHTML = results.products.map(product => this.renderProductCard(product)).join('');
        }
        
        this.attachClickHandlers();
    }
    
    // --- Product Card Rendering ---
    
    renderProductCard(product) {
        const rawUrl = product.url || (product.sources && product.sources[0]?.url) || '#';
        const sourceDomain = product.sourceDomain || this.extractDomain(rawUrl);
        const url = affiliateManager.wrapLink(rawUrl, sourceDomain);
        
        const price = product.bestPrice || product.price || 'See price';
        const sourceText = product.sources && product.sources.length > 1 
            ? `${product.sources.length} sources` 
            : `via ${product.source}`;
        
        return `
            <div class="product-card" data-domain="${this.escapeHtml(sourceDomain)}">
                <div class="image-container">
                    ${product.image 
                        ? `<img src="${this.escapeHtml(product.image)}" alt="${this.escapeHtml(product.name)}" loading="lazy">` 
                        : '<div class="no-image">No image</div>'}
                </div>
                <div class="card-body">
                    <h3 class="product-name">
                        <a href="${this.escapeHtml(url)}" target="_blank" rel="noopener" class="affiliate-link">
                            ${this.escapeHtml(product.name)}
                        </a>
                    </h3>
                    <div class="price">${this.escapeHtml(price)}</div>
                    <span class="source">${sourceText}</span>
                </div>
            </div>
        `;
    }
    
    attachClickHandlers() {
        document.querySelectorAll('.affiliate-link').forEach(link => {
            link.addEventListener('click', (e) => {
                const card = e.target.closest('.product-card');
                const domain = card?.dataset?.domain;
                if (domain) {
                    affiliateManager.trackClick(domain);
                }
            });
        });
    }
    
    extractDomain(url) {
        try {
            return new URL(url).hostname;
        } catch (e) {
            return '';
        }
    }
    
    showError(message) {
        document.getElementById('error-text').textContent = message;
        this.showState('error');
    }
    
    escapeHtml(text) {
        if (!text) return '';
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }
}

export { UI };
