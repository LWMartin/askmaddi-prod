/**
 * Site-Specific Centroids
 * =======================
 * Real examples from each site that teach the model
 * what products look like on that specific site.
 * 
 * These are computed once and cached in the browser.
 */

const SITE_EXAMPLES = {
    newegg: {
        domain: "newegg.com",
        product_name: [
            "ASUS ROG Strix G16 (2024) Gaming Laptop, 16\" 16:10 FHD 165Hz",
            "Sony WH-1000XM5 Wireless Industry Leading Noise Canceling Headphones",
            "Samsung 990 PRO 2TB PCIe 4.0 NVMe M.2 Internal SSD",
            "Logitech G Pro X Superlight Wireless Gaming Mouse",
            "CORSAIR Vengeance RGB 32GB (2x16GB) DDR5 6000MHz",
            "EVGA SuperNOVA 850 G6, 80 Plus Gold 850W",
            "LG 27GP850-B 27 Inch Ultragear QHD Nano IPS 1ms Gaming Monitor"
        ],
        price: [
            "$1,549.99",
            "$348.00",
            "$179.99",
            "$149.99",
            "$114.99",
            "$129.99",
            "$449.99",
            "$89.99"
        ],
        not_product_name: [
            "Add to cart",
            "Shop Now",
            "FREE SHIPPING",
            "View Details",
            "Compare",
            "Sold by Newegg",
            "Ships from United States"
        ]
    },
    
    bestbuy: {
        domain: "bestbuy.com",
        product_name: [
            "Samsung - 65\" Class QN85D Series Neo QLED 4K Smart Tizen TV",
            "Apple - MacBook Air 15\" Laptop - M3 chip - 8GB Memory - 256GB SSD",
            "Sony - WH-1000XM5 Wireless Noise-Canceling Over-the-Ear Headphones",
            "LG - 77\" Class C4 Series OLED evo 4K UHD Smart webOS TV",
            "Dyson - V15 Detect Extra Cordless Vacuum - Yellow/Nickel",
            "KitchenAid - Artisan Series 5 Quart Tilt-Head Stand Mixer"
        ],
        price: [
            "$1,599.99",
            "$1,299.00",
            "$399.99",
            "$2,499.99",
            "$749.99",
            "$449.99"
        ],
        not_product_name: [
            "Add to Cart",
            "Save",
            "Open-Box",
            "See All Buying Options",
            "Free shipping",
            "Pick up today",
            "Best Buy member"
        ]
    },
    
    ebay: {
        domain: "ebay.com",
        product_name: [
            "Apple iPhone 15 Pro Max 256GB Natural Titanium Unlocked Very Good",
            "NVIDIA GeForce RTX 4090 24GB GDDR6X Graphics Card",
            "Sony PlayStation 5 Slim Console Digital Edition - White",
            "Dyson V11 Animal Cordless Vacuum Cleaner - Purple - Refurbished",
            "Samsung Galaxy Watch 6 Classic 47mm Bluetooth Smart Watch",
            "Bose QuietComfort Ultra Headphones with Spatial Audio - Black"
        ],
        price: [
            "$899.99",
            "$1,599.00",
            "$399.99",
            "$299.99",
            "$249.00",
            "$379.00",
            "$45.99",
            "$1,234.56"
        ],
        not_product_name: [
            "Shop on eBay",
            "Brand New",
            "Free returns",
            "Buy It Now",
            "or Best Offer",
            "from United States",
            "eBay Refurbished",
            "Sponsored"
        ]
    }
};

// Negative examples that should NEVER be product names
const UNIVERSAL_NEGATIVES = {
    product_name: [
        "Add to cart",
        "Buy now",
        "Free shipping",
        "In stock",
        "Out of stock",
        "Ships from",
        "Sold by",
        "See details",
        "View more",
        "Loading...",
        "Please wait",
        "Sign in",
        "Create account",
        "Customer reviews",
        "Frequently bought together",
        "Sponsored",
        "Advertisement",
        "Ad"
    ]
};


class CentroidManager {
    constructor() {
        this.centroids = {};
        this.negativeCentroids = {};
        this.embedder = null;
        this.cacheKey = 'maddi-centroids-v1';
    }
    
    /**
     * Initialize with an embedding function
     */
    async init(embedFunction) {
        this.embedder = embedFunction;
        
        // Try to load from cache
        const cached = await this.loadFromCache();
        if (cached) {
            console.log('Loaded centroids from cache');
            this.centroids = cached.centroids;
            this.negativeCentroids = cached.negatives;
            return;
        }
        
        // Compute fresh centroids
        console.log('Computing site centroids...');
        await this.computeAllCentroids();
        
        // Cache for next time
        await this.saveToCache();
        console.log('Centroids computed and cached');
    }
    
    /**
     * Compute centroids for all sites
     */
    async computeAllCentroids() {
        // Compute site-specific centroids
        for (const [siteName, examples] of Object.entries(SITE_EXAMPLES)) {
            this.centroids[siteName] = {};
            
            for (const [fieldType, texts] of Object.entries(examples)) {
                if (fieldType === 'domain') continue;
                
                const embeddings = [];
                for (const text of texts) {
                    const emb = await this.embedder(text);
                    if (emb) embeddings.push(emb);
                }
                
                if (embeddings.length > 0) {
                    this.centroids[siteName][fieldType] = this.averageEmbeddings(embeddings);
                }
            }
            
            console.log(`Computed centroids for ${siteName}:`, Object.keys(this.centroids[siteName]));
        }
        
        // Compute universal negative centroids
        for (const [fieldType, texts] of Object.entries(UNIVERSAL_NEGATIVES)) {
            const embeddings = [];
            for (const text of texts) {
                const emb = await this.embedder(text);
                if (emb) embeddings.push(emb);
            }
            
            if (embeddings.length > 0) {
                this.negativeCentroids[fieldType] = this.averageEmbeddings(embeddings);
            }
        }
    }
    
    /**
     * Get centroid for a site and field type
     */
    getCentroid(siteName, fieldType) {
        // Try site-specific first
        const siteKey = siteName.toLowerCase().replace(/[^a-z]/g, '');
        
        if (this.centroids[siteKey]?.[fieldType]) {
            return this.centroids[siteKey][fieldType];
        }
        
        // Fall back to any site's centroid as general reference
        for (const site of Object.values(this.centroids)) {
            if (site[fieldType]) {
                return site[fieldType];
            }
        }
        
        return null;
    }
    
    /**
     * Get negative centroid for a field type
     */
    getNegativeCentroid(fieldType) {
        return this.negativeCentroids[fieldType] || null;
    }
    
    /**
     * Score how well text matches a field type for a specific site
     */
    async scoreMatch(text, siteName, fieldType) {
        const embedding = await this.embedder(text);
        if (!embedding) return { score: 0, isNegative: false };
        
        const positiveCentroid = this.getCentroid(siteName, fieldType);
        const negativeCentroid = this.getNegativeCentroid(fieldType);
        
        let positiveScore = 0;
        let negativeScore = 0;
        
        if (positiveCentroid) {
            positiveScore = this.cosineSimilarity(embedding, positiveCentroid);
        }
        
        if (negativeCentroid) {
            negativeScore = this.cosineSimilarity(embedding, negativeCentroid);
        }
        
        // Check site-specific negatives
        const siteKey = siteName.toLowerCase().replace(/[^a-z]/g, '');
        const siteNegativeCentroid = this.centroids[siteKey]?.['not_' + fieldType];
        if (siteNegativeCentroid) {
            const siteNegScore = this.cosineSimilarity(embedding, siteNegativeCentroid);
            negativeScore = Math.max(negativeScore, siteNegScore);
        }
        
        // If it's closer to negative examples, reject it
        const isNegative = negativeScore > positiveScore + 0.1;
        
        // Final score penalizes negative matches
        const finalScore = isNegative ? 0 : positiveScore - (negativeScore * 0.5);
        
        return { 
            score: Math.max(0, finalScore), 
            isNegative,
            positiveScore,
            negativeScore
        };
    }
    
    /**
     * Average multiple embeddings
     */
    averageEmbeddings(embeddings) {
        if (embeddings.length === 0) return null;
        
        const dim = embeddings[0].length;
        const avg = new Array(dim).fill(0);
        
        for (const emb of embeddings) {
            for (let i = 0; i < dim; i++) {
                avg[i] += emb[i];
            }
        }
        
        for (let i = 0; i < dim; i++) {
            avg[i] /= embeddings.length;
        }
        
        return avg;
    }
    
    /**
     * Cosine similarity
     */
    cosineSimilarity(a, b) {
        if (!a || !b) return 0;
        
        let dotProduct = 0;
        let normA = 0;
        let normB = 0;
        
        for (let i = 0; i < a.length; i++) {
            dotProduct += a[i] * b[i];
            normA += a[i] * a[i];
            normB += b[i] * b[i];
        }
        
        return dotProduct / (Math.sqrt(normA) * Math.sqrt(normB));
    }
    
    /**
     * Save to Cache API
     */
    async saveToCache() {
        try {
            const cache = await caches.open('maddi-centroids');
            const data = JSON.stringify({
                centroids: this.centroids,
                negatives: this.negativeCentroids,
                timestamp: Date.now()
            });
            await cache.put(this.cacheKey, new Response(data));
        } catch (e) {
            console.warn('Failed to cache centroids:', e);
        }
    }
    
    /**
     * Load from Cache API
     */
    async loadFromCache() {
        try {
            const cache = await caches.open('maddi-centroids');
            const response = await cache.match(this.cacheKey);
            if (response) {
                const data = await response.json();
                // Check if cache is less than 7 days old
                if (Date.now() - data.timestamp < 7 * 24 * 60 * 60 * 1000) {
                    return data;
                }
            }
        } catch (e) {
            console.warn('Failed to load cached centroids:', e);
        }
        return null;
    }
}

export { CentroidManager, SITE_EXAMPLES };