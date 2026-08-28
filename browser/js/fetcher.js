class Fetcher {
    constructor(gatewayUrl) {
        this.gateway = gatewayUrl;
    }
    
    async getInstructions() {
        const response = await fetch(`${this.gateway}/instructions`);
        if (!response.ok) throw new Error(`Failed to load instructions: ${response.status}`);
        return await response.json();
    }
    
    async searchEbay(query, limit = 25) {
        // Official eBay Browse API (server-side). Returns structured listings
        // with EPN affiliate attribution baked into each URL — no scrape, no
        // proxy, no Akamai. Response: { count, items: [{name, price, currency,
        // image, url, condition, seller}] }. `limit` is raised by "Show more".
        const response = await fetch(
            `${this.gateway}/ebay/search?q=${encodeURIComponent(query)}&limit=${limit}`
        );
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `eBay search failed: ${response.status}`);
        }
        const data = await response.json();
        return Array.isArray(data.items) ? data.items : [];
    }

    async searchAdorama(query, limit = 25) {
        // Adorama feed index (server-side). Fast lookup, no Sieve. Same
        // { count, items:[{name, price, currency, image, url, condition,
        // seller, gtin, mpn, brand, model}] } shape as searchEbay — the New
        // lane, streamed in parallel with eBay. `limit` is raised by "Show more".
        const response = await fetch(
            `${this.gateway}/adorama/search?q=${encodeURIComponent(query)}&limit=${limit}`
        );
        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            throw new Error(error.error || `Adorama search failed: ${response.status}`);
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