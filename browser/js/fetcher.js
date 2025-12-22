class Fetcher {
    constructor(gatewayUrl) {
        this.gateway = gatewayUrl;
    }
    
    async getInstructions() {
        const response = await fetch(`${this.gateway}/instructions`);
        if (!response.ok) throw new Error(`Failed to load instructions: ${response.status}`);
        return await response.json();
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