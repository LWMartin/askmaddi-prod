import { Fetcher } from './fetcher.js';
import { Extractor } from './extractor.js';
import { Deduper } from './deduper.js';
import { Ranker } from './ranker.js';
import { UI } from './ui.js';

class AskMaddi {
    constructor() {
        this.gateway = 'http://localhost:5000';
        this.manifests = null;
        this.fetcher = new Fetcher(this.gateway);
        this.extractor = new Extractor();
        this.deduper = new Deduper();
        this.ranker = new Ranker();
        this.ui = new UI();
        this.currentQuery = '';
        this.isSearching = false;
    }
    
    async init() {
        console.log('Initializing AskMaddi...');
        this.bindEvents();
        
        const needsSetup = await this.extractor.needsSetup();
        if (needsSetup) {
            this.ui.showState('setup');
            await this.extractor.setup((progress, status) => {
                this.ui.updateSetupProgress(progress, status);
            });
        }
        
        try {
            this.manifests = await this.fetcher.getInstructions();
            console.log('Loaded manifests:', Object.keys(this.manifests.sites));
        } catch (error) {
            console.error('Failed to load manifests:', error);
        }
        
        this.ui.showState('landing');
        console.log('AskMaddi ready!');
    }
    
    bindEvents() {
        document.getElementById('search-button').addEventListener('click', () => this.handleSearch());
        
        document.getElementById('search-input').addEventListener('keypress', (e) => {
            if (e.key === 'Enter') this.handleSearch();
        });
        
        document.querySelectorAll('.example-chip').forEach(chip => {
            chip.addEventListener('click', () => {
                document.getElementById('search-input').value = chip.dataset.query;
                this.handleSearch();
            });
        });
        
        document.getElementById('new-search').addEventListener('click', () => {
            this.ui.showState('landing');
            document.getElementById('search-input').value = '';
            document.getElementById('search-input').focus();
        });
        
        document.getElementById('cancel-search').addEventListener('click', () => this.cancelSearch());
        document.getElementById('retry-search').addEventListener('click', () => this.handleSearch());
    }
    
    async handleSearch() {
        const input = document.getElementById('search-input');
        const query = input.value.trim();
        
        if (!query || this.isSearching) return;
        
        this.currentQuery = query;
        this.isSearching = true;
        
        console.log('Searching for:', query);
        this.ui.showState('loading');
        
        try {
            const results = await this.search(query);
            this.displayResults(results);
        } catch (error) {
            console.error('Search failed:', error);
            this.ui.showError(error.message);
        } finally {
            this.isSearching = false;
        }
    }
    
    async search(query) {
    const allProducts = [];
    const sites = Object.entries(this.manifests.sites);
    
    this.ui.updateLoadingStatus(sites.map(([name]) => ({ name, status: 'pending' })));
    
    // Fetch ALL sites in parallel
    const fetchPromises = sites.map(async ([siteName, manifest]) => {
        try {
            this.ui.updateSourceStatus(siteName, 'fetching');
            
            const searchUrl = manifest.search.url_template.replace('{query}', encodeURIComponent(query));
            const html = await this.fetcher.fetchViaProxy(searchUrl);
            
            this.ui.updateSourceStatus(siteName, 'extracting');
            
            const products = await this.extractor.extract(html, manifest);
            
            products.forEach(p => {
                p.source = manifest.name;
                p.sourceDomain = manifest.domain;
            });
            
            this.ui.updateSourceStatus(siteName, 'done');
            return products;
            
        } catch (error) {
            console.error(`Failed to fetch ${siteName}:`, error);
            this.ui.updateSourceStatus(siteName, 'error');
            return [];
        }
    });
    
    // Wait for all to complete
    const results = await Promise.all(fetchPromises);
    
    // Flatten results
    for (const products of results) {
        allProducts.push(...products);
    }
    
    this.sendAnalytics(query, sites.length);
    
    const deduped = this.deduper.deduplicate(allProducts);
    const ranked = this.ranker.rank(deduped, query);
    
    return {
        query,
        products: ranked,
        totalFound: allProducts.length,
        afterDedup: deduped.length,
        sourcesChecked: sites.length
    };
}
    
    displayResults(results) {
        this.ui.showState('results');
        this.ui.displayResults(results);
    }
    
    cancelSearch() {
        this.isSearching = false;
        this.ui.showState('landing');
    }
    
    sendAnalytics(query, sourceCount) {
        const category = this.detectCategory(query);
        
        fetch(`${this.gateway}/ping`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ category, source_count: sourceCount })
        }).catch(() => {});
    }
    
    detectCategory(query) {
        const q = query.toLowerCase();
        if (/headphone|earbud|speaker|audio/.test(q)) return 'audio';
        if (/laptop|computer|pc|monitor/.test(q)) return 'computers';
        if (/phone|tablet|ipad/.test(q)) return 'mobile';
        if (/keyboard|mouse|webcam/.test(q)) return 'accessories';
        return 'other';
    }
}

const app = new AskMaddi();
document.addEventListener('DOMContentLoaded', () => app.init());