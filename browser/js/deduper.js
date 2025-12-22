class Deduper {
    constructor() {
        this.similarityThreshold = 0.75;
    }
    
    deduplicate(products) {
        if (products.length === 0) return [];
        
        const clusters = [];
        
        for (const product of products) {
            let foundCluster = false;
            for (const cluster of clusters) {
                if (this.isSimilar(product, cluster.primary)) {
                    cluster.duplicates.push(product);
                    foundCluster = true;
                    break;
                }
            }
            if (!foundCluster) {
                clusters.push({ primary: product, duplicates: [] });
            }
        }
        
        return clusters.map(cluster => this.mergeCluster(cluster));
    }
    
    isSimilar(a, b) {
        if (!a.name || !b.name) return false;
        return this.textSimilarity(a.name.toLowerCase(), b.name.toLowerCase()) >= this.similarityThreshold;
    }
    
    textSimilarity(a, b) {
        const wordsA = new Set(a.split(/\s+/).filter(w => w.length > 2));
        const wordsB = new Set(b.split(/\s+/).filter(w => w.length > 2));
        if (wordsA.size === 0 || wordsB.size === 0) return 0;
        
        const intersection = new Set([...wordsA].filter(x => wordsB.has(x)));
        const union = new Set([...wordsA, ...wordsB]);
        return intersection.size / union.size;
    }
    
    mergeCluster(cluster) {
        const primary = { ...cluster.primary };
        const allProducts = [cluster.primary, ...cluster.duplicates];
        
        primary.sources = allProducts.map(p => ({
            name: p.source,
            domain: p.sourceDomain,
            price: p.price,
            url: p.url
        }));
        
        const prices = allProducts.map(p => this.parsePrice(p.price)).filter(p => p !== null);
        if (prices.length > 0) {
            primary.bestPrice = '$' + Math.min(...prices).toFixed(2);
        }
        
        primary.duplicateCount = cluster.duplicates.length;
        return primary;
    }
    
    parsePrice(priceStr) {
        if (!priceStr) return null;
        const match = priceStr.match(/[\d,]+\.?\d*/);
        return match ? parseFloat(match[0].replace(/,/g, '')) : null;
    }
}

export { Deduper };