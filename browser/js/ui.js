/**
 * UI Module
 * =========
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
    
    updateLoadingStatus(sources) {
        const container = document.getElementById('loading-status');
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
    
    displayResults(results) {
        document.getElementById('result-count').textContent = results.products.length;
        document.getElementById('search-echo').textContent = results.query;
        document.getElementById('sources-checked').textContent = `Checked ${results.sourcesChecked} sources`;
        
        const dupeCount = results.totalFound - results.afterDedup;
        document.getElementById('dupes-hidden').textContent = dupeCount > 0 ? `${dupeCount} duplicates hidden` : '';
        
        const grid = document.getElementById('results-grid');
        grid.innerHTML = results.products.map(product => this.renderProductCard(product)).join('');
        
        // Attach click handlers for affiliate tracking
        this.attachClickHandlers();
    }
    
    renderProductCard(product) {
        const rawUrl = product.url || (product.sources && product.sources[0]?.url) || '#';
        const sourceDomain = product.sourceDomain || this.extractDomain(rawUrl);
        
        // Wrap with affiliate code
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
    
    /**
     * Track affiliate clicks
     */
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
    
    /**
     * Extract domain from URL
     */
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
