/**
 * Extractor Module - v2 with Site-Specific Centroids
 * ===================================================
 * Uses Transformers.js + trained centroids for each site.
 */

import { CentroidManager } from './centroids.js';

class Extractor {
    constructor() {
        this.parser = new DOMParser();
        this.modelLoaded = false;
        this.featureExtractor = null;
        this.centroidManager = new CentroidManager();
        this.modelName = 'Xenova/all-MiniLM-L6-v2';
    }
    
    /**
     * Check if first-time setup is needed
     */
    async needsSetup() {
        try {
            const cache = await caches.open('maddi-models');
            const cached = await cache.match(this.modelName);
            return !cached;
        } catch (e) {
            return true;
        }
    }
    
    /**
     * First-time setup - download and cache model
     */
    async setup(progressCallback) {
        progressCallback(5, 'Loading Transformers.js...');
        
        try {
            const { pipeline, env } = await import(
                'https://cdn.jsdelivr.net/npm/@xenova/transformers@2.17.0'
            );
            
            env.cacheDir = 'maddi-models';
            env.allowLocalModels = false;
            
            progressCallback(15, 'Downloading intelligence model (23MB)...');
            
            this.featureExtractor = await pipeline(
                'feature-extraction',
                this.modelName,
                { 
                    progress_callback: (progress) => {
                        if (progress.status === 'downloading') {
                            const pct = Math.round((progress.loaded / progress.total) * 60) + 15;
                            progressCallback(pct, `Downloading: ${Math.round(progress.loaded / 1024 / 1024)}MB`);
                        }
                    }
                }
            );
            
            progressCallback(80, 'Learning site patterns...');
            
            // Initialize centroid manager with our embed function
            await this.centroidManager.init(this.embed.bind(this));
            
            // Mark model as cached
            const cache = await caches.open('maddi-models');
            await cache.put(this.modelName, new Response('loaded'));
            
            this.modelLoaded = true;
            progressCallback(100, 'Ready!');
            
            console.log('Semantic extraction with site centroids ready!');
            
        } catch (error) {
            console.error('Failed to load model:', error);
            progressCallback(100, 'Using fallback mode');
        }
    }
    
    /**
     * Ensure model is loaded (for subsequent visits)
     */
    async ensureLoaded() {
        if (this.modelLoaded) return;
        
        try {
            const { pipeline, env } = await import(
                'https://cdn.jsdelivr.net/npm/@xenova/transformers@2.17.0'
            );
            
            env.cacheDir = 'maddi-models';
            
            this.featureExtractor = await pipeline(
                'feature-extraction',
                this.modelName
            );
            
            await this.centroidManager.init(this.embed.bind(this));
            this.modelLoaded = true;
            
        } catch (error) {
            console.error('Failed to load model:', error);
        }
    }
    
    /**
     * Get embedding for text
     */
    async embed(text) {
        if (!this.featureExtractor) return null;
        
        const result = await this.featureExtractor(text, {
            pooling: 'mean',
            normalize: true
        });
        
        return Array.from(result.data);
    }
    
    /**
     * Extract products from HTML
     */
    async extract(html, manifest) {
    await this.ensureLoaded();
    
    const doc = this.parser.parseFromString(html, 'text/html');
    const products = [];
    const siteName = manifest.name;
    
    const containers = this.findContainers(doc, manifest.extraction.container_hints);
    
    console.log(`Found ${containers.length} containers on ${siteName}`);
    
    for (const container of containers) {
        let product;
        
        if (this.modelLoaded) {
            product = await this.extractProductSemantic(container, manifest.extraction.fields, siteName);
            
            // If semantic found nothing, fall back to CSS
            if (!product.name) {
                product = this.extractProductCSS(container, manifest.extraction.fields);
                if (product.name) {
                    console.log(`[${siteName}] CSS fallback used for: ${product.name.substring(0, 40)}...`);
                }
            }
        } else {
            product = this.extractProductCSS(container, manifest.extraction.fields);
        }
        
        if (product.name && product.name.length > 3) {
            products.push(product);
        }
    }
    
    console.log(`Extracted ${products.length} products from ${siteName} (semantic: ${this.modelLoaded})`);
    return products;
}
    
    /**
     * Find product containers
     */
    findContainers(doc, hints) {
        for (const hint of hints) {
            try {
                const elements = doc.querySelectorAll(hint);
                if (elements.length > 0) {
                    const valid = Array.from(elements).filter(el => 
                        (el.textContent || '').length > 50
                    );
                    if (valid.length > 0) return valid;
                }
            } catch (e) {
                continue;
            }
        }
        return [];
    }
    
    /**
     * Semantic extraction using site-specific centroids
     */
    async extractProductSemantic(container, fieldConfigs, siteName) {
        const product = {};
        const candidates = this.getCandidateElements(container);
        
        for (const [fieldName, config] of Object.entries(fieldConfigs)) {
            const value = await this.findFieldSemantic(candidates, config, siteName, container);
            if (value) {
                product[fieldName] = value;
            }
        }
        
        return product;
    }
    
    /**
     * Get candidate elements from container
     */
    getCandidateElements(container) {
        const candidates = [];
        
        // Text elements
        const textElements = container.querySelectorAll('span, div, p, h1, h2, h3, h4, a, strong, b');
        for (const el of textElements) {
            const text = el.textContent?.trim();
            if (text && text.length > 2 && text.length < 500) {
                candidates.push({ element: el, text, type: 'text' });
            }
        }
        
        // Images
        const images = container.querySelectorAll('img');
        for (const img of images) {
            const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
            if (src) {
                candidates.push({ element: img, text: img.getAttribute('alt') || '', type: 'image', src });
            }
        }
        
        // Links
        const links = container.querySelectorAll('a[href]');
        for (const link of links) {
            const href = link.getAttribute('href');
            if (href && href.length > 5) {
                candidates.push({ element: link, text: link.textContent?.trim(), type: 'link', href });
            }
        }
        
        return candidates;
    }
    
    /**
     * Find field using semantic matching with site centroids
     */
    async findFieldSemantic(candidates, config, siteName, container) {
        const semanticType = config.semantic_type;
        
        // Images
        if (semanticType === 'product_image') {
            const imgCandidates = candidates.filter(c => c.type === 'image');
            if (imgCandidates.length > 0) {
                return this.normalizeUrl(imgCandidates[0].src);
            }
        }
        
        // URLs
        if (semanticType === 'product_url') {
            const linkCandidates = candidates.filter(c => c.type === 'link' && c.text?.length > 10);
            if (linkCandidates.length > 0) {
                return this.normalizeUrl(linkCandidates[0].href);
            }
        }
        
        // Text fields - use centroid matching
        const textCandidates = candidates.filter(c => 
            (c.type === 'text' || c.type === 'link') && c.text
        );
        
        let bestMatch = null;
        let bestScore = 0.15;  // Minimum threshold
        
        // Limit candidates to check
        const toCheck = textCandidates.slice(0, 10);
        
        for (const candidate of toCheck) {
    // Skip obvious non-matches by length
    if (semanticType === 'product_name') {
        if (candidate.text.length < 15 || candidate.text.length > 250) continue;
        // Skip if it's just numbers or very short words
        if (!/[a-zA-Z]{3,}/.test(candidate.text)) continue;
    }
    if (semanticType === 'price') {
        if (!candidate.text.match(/\$[\d,.]+/)) continue;  // Must have $ and numbers
    }
    
    // ... rest of scoring
            
            const result = await this.centroidManager.scoreMatch(
                candidate.text,
                siteName,
                semanticType
            );
            
            if (!result.isNegative && result.score > bestScore) {
                bestScore = result.score;
                bestMatch = candidate;
            }
        }
        
        if (bestMatch) {
            if (semanticType === 'price') {
                return this.normalizePrice(bestMatch.text);
            }
            return bestMatch.text.replace(/\s+/g, ' ').trim();
        }
        
        // Fallback to CSS
        return this.extractFieldCSS(container, config);
    }
    
    /**
     * CSS fallback extraction
     */
    extractProductCSS(container, fieldConfigs) {
        const product = {};
        for (const [fieldName, config] of Object.entries(fieldConfigs)) {
            const value = this.extractFieldCSS(container, config);
            if (value) product[fieldName] = value;
        }
        return product;
    }
    
    extractFieldCSS(container, config) {
        for (const selector of config.fallback_selectors || []) {
            try {
                const element = container.querySelector(selector);
                if (element) {
                    if (config.semantic_type === 'product_image') {
                        const src = element.getAttribute('src') || element.getAttribute('data-src');
                        if (src) return this.normalizeUrl(src);
                    }
                    if (config.semantic_type === 'product_url') {
                        const href = element.getAttribute('href');
                        if (href) return this.normalizeUrl(href);
                    }
                    if (config.semantic_type === 'price') {
                        return this.normalizePrice(element.textContent.trim());
                    }
                    const text = element.textContent.trim();
                    if (text) return text.replace(/\s+/g, ' ');
                }
            } catch (e) {
                continue;
            }
        }
        return null;
    }
    
    normalizePrice(text) {
        const match = text.match(/[\$€£]?\s*[\d,]+\.?\d*/);
        if (match) {
            let price = match[0].trim();
            if (!/^[\$€£]/.test(price)) price = '$' + price;
            return price;
        }
        return text;
    }
    
    normalizeUrl(url) {
        if (!url) return null;
        if (url.startsWith('http')) return url;
        if (url.startsWith('//')) return 'https:' + url;
        return url;
    }
}

export { Extractor };