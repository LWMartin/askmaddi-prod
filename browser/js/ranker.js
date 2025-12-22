class Ranker {
    constructor() {
        this.weights = { price: 0.4, hasImage: 0.2, hasRating: 0.2, nameQuality: 0.2 };
    }
    
    rank(products, query) {
        const scored = products.map(product => ({
            ...product,
            score: this.scoreProduct(product, query)
        }));
        scored.sort((a, b) => b.score - a.score);
        return scored;
    }
    
    scoreProduct(product, query) {
        let score = 0;
        
        if (product.price || product.bestPrice) {
            const price = this.parsePrice(product.bestPrice || product.price);
            if (price && price > 0) score += Math.min(1, 100 / price) * this.weights.price;
        }
        
        if (product.image) score += this.weights.hasImage;
        if (product.rating) score += this.weights.hasRating;
        if (product.name) score += Math.min(1, product.name.length / 80) * this.weights.nameQuality;
        
        if (product.name && query) {
            score += this.queryRelevance(product.name, query) * 0.3;
        }
        
        if (product.sources && product.sources.length > 1) {
            score += 0.1 * Math.min(product.sources.length, 3);
        }
        
        return score;
    }
    
    queryRelevance(name, query) {
        const nameWords = new Set(name.toLowerCase().split(/\s+/));
        const queryWords = query.toLowerCase().split(/\s+/).filter(w => w.length > 2);
        if (queryWords.length === 0) return 0;
        return queryWords.filter(w => nameWords.has(w)).length / queryWords.length;
    }
    
    parsePrice(priceStr) {
        if (!priceStr) return null;
        const match = priceStr.match(/[\d,]+\.?\d*/);
        return match ? parseFloat(match[0].replace(/,/g, '')) : null;
    }
}

export { Ranker };