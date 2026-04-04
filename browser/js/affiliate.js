/**
 * Affiliate Link Manager
 * ======================
 * Wraps product URLs with affiliate tracking codes.
 * 
 * Revenue without compromise:
 * - User still goes directly to merchant
 * - Price doesn't change for user
 * - We earn commission on purchase
 * - No tracking of user behavior
 */

class AffiliateManager {
    constructor() {
        // Affiliate configurations
        // Replace 'YOUR_XXX_ID' with actual IDs after approval
        this.programs = {
            'newegg.com': {
                enabled: false,  // Set true after approval
                name: 'Newegg',
                param: 'cm_mmc',
                code: 'AFC-YOUR_NEWEGG_ID',
                commission: '2.5-5%',
                cookieDays: 7
            },
            'ebay.com': {
                enabled: true,
                name: 'eBay Partner Network',
                param: 'campid',
                code: '5339138080',
                // eBay also uses additional params
                extraParams: {
                    'toolid': '10001',
                    'mkevt': '1'
                },
                commission: '1-4%',
                cookieDays: 1
            },
            'bestbuy.com': {
                enabled: false,  // Set true after approval
                name: 'Best Buy Affiliates',
                param: 'irclickid',
                code: 'YOUR_BESTBUY_ID',
                commission: '0.5-1%',
                cookieDays: 1
            },
            'amazon.com': {
                enabled: true,
                name: 'Amazon Associates',
                param: 'tag',
                code: 'askmaddi-20',
                commission: '1-4%',
                cookieDays: 1
            },
            'walmart.com': {
                enabled: false,
                name: 'Walmart Affiliates',
                param: 'wmlspartner',
                code: 'YOUR_WALMART_ID',
                commission: '1-4%',
                cookieDays: 3
            }
        };
        
        // Stats (local only, for your reference)
        this.clickCount = this.loadClickCount();
    }
    
    /**
     * Wrap a URL with affiliate code
     */
    wrapLink(url, sourceDomain) {
        if (!url || url === '#') return url;
        
        // Find matching affiliate program
        const program = this.findProgram(sourceDomain);
        
        if (!program || !program.enabled) {
            return url;  // No affiliate, return original
        }
        
        // Build affiliate URL
        try {
            const urlObj = new URL(url);
            
            // Add main affiliate param
            urlObj.searchParams.set(program.param, program.code);
            
            // Add extra params if any (eBay needs these)
            if (program.extraParams) {
                for (const [key, value] of Object.entries(program.extraParams)) {
                    urlObj.searchParams.set(key, value);
                }
            }
            
            return urlObj.toString();
            
        } catch (e) {
            // URL parsing failed, try string append
            const separator = url.includes('?') ? '&' : '?';
            return `${url}${separator}${program.param}=${program.code}`;
        }
    }
    
    /**
     * Find affiliate program for a domain
     */
    findProgram(domain) {
        if (!domain) return null;
        
        const domainLower = domain.toLowerCase();
        
        for (const [site, program] of Object.entries(this.programs)) {
            if (domainLower.includes(site.replace('.com', ''))) {
                return program;
            }
        }
        
        return null;
    }
    
    /**
     * Track click (local stats only)
     */
    trackClick(sourceDomain) {
        const program = this.findProgram(sourceDomain);
        const key = program ? program.name : 'unknown';
        
        this.clickCount[key] = (this.clickCount[key] || 0) + 1;
        this.clickCount.total = (this.clickCount.total || 0) + 1;
        
        this.saveClickCount();
        
        console.log(`[Affiliate] Click: ${key} (total: ${this.clickCount.total})`);
    }
    
    /**
     * Get click stats (for your dashboard later)
     */
    getStats() {
        return {
            clicks: this.clickCount,
            programs: Object.entries(this.programs).map(([domain, p]) => ({
                domain,
                name: p.name,
                enabled: p.enabled,
                commission: p.commission
            }))
        };
    }
    
    /**
     * Enable a program (call after approval)
     */
    enableProgram(domain, code) {
        if (this.programs[domain]) {
            this.programs[domain].enabled = true;
            this.programs[domain].code = code;
            console.log(`[Affiliate] Enabled: ${domain}`);
        }
    }
    
    /**
     * Load click count from localStorage
     */
    loadClickCount() {
        try {
            const stored = localStorage.getItem('maddi-affiliate-clicks');
            return stored ? JSON.parse(stored) : { total: 0 };
        } catch (e) {
            return { total: 0 };
        }
    }
    
    /**
     * Save click count to localStorage
     */
    saveClickCount() {
        try {
            localStorage.setItem('maddi-affiliate-clicks', JSON.stringify(this.clickCount));
        } catch (e) {
            // Storage not available
        }
    }
}

// Singleton instance
const affiliateManager = new AffiliateManager();

export { AffiliateManager, affiliateManager };