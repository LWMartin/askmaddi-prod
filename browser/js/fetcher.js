class Fetcher {
    constructor(gatewayUrl) {
        this.gateway = gatewayUrl;
    }
    
    async getInstructions() {
        const response = await fetch(`${this.gateway}/instructions`);
        if (!response.ok) throw new Error(`Failed to load instructions: ${response.status}`);
        return await response.json();
    }
    
    async searchEbay(query) {
        // Official eBay Browse API (server-side). Returns structured listings
        // with EPN affiliate attribution baked into each URL — no scrape, no
        // proxy, no Akamai. Response: { count, items: [{name, price, currency,
        // image, url, condition, seller}] }.
        const response = await fetch(
            `${this.gateway}/ebay/search?q=${encodeURIComponent(query)}`
        );
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `eBay search failed: ${response.status}`);
        }
        const data = await response.json();
        return Array.isArray(data.items) ? data.items : [];
    }

    async fetchViaProxy(url) {
        const response = await fetch(`${this.gateway}/proxy`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url })
        });
        
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `Proxy failed: ${response.status}`);
        }
        
        return await response.text();
    }
}

export { Fetcher };