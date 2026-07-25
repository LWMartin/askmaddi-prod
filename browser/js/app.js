import { Fetcher } from './fetcher.js';
import { Extractor } from './extractor.js';
import { Deduper } from './deduper.js';
import { Ranker } from './ranker.js';
import { UI } from './ui.js';
import { loadManifest, matchCards, renderMatchedCards } from './cards.js';

class AskMaddi {
    constructor() {
        this.gateway = '';
        this.manifests = null;
        this.fetcher = new Fetcher(this.gateway);
        this.extractor = new Extractor();
        this.deduper = new Deduper();
        this.ranker = new Ranker();
        this.ui = new UI();
        this.currentQuery = '';
        this.isSearching = false;
        this.cardManifest = null;
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
            this.manifestError = error;
        }

        // Preload card manifest for instant matching
        this.cardManifest = await loadManifest();
        
        this.ui.showState('landing');
        console.log('AskMaddi ready!');

        // Deep-link: if arriving with ?q=… (e.g. from a card detail page's
        // search bar), populate the input and run the search automatically.
        const params = new URLSearchParams(window.location.search);
        const incomingQuery = params.get('q');
        if (incomingQuery && incomingQuery.trim()) {
            const input = document.getElementById('search-input');
            if (input) {
                input.value = incomingQuery.trim();
                this.handleSearch();
            }
        }

        // Win 3: Preload extraction model silently in background
        // If model is cached, this is instant. If not, starts download
        // so it's warm by the time user searches.
        this.extractor.needsSetup().then(needs => {
            if (!needs) {
                this.extractor.warmup && this.extractor.warmup();
            }
        });
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
        
        if (!this.manifests || !this.manifests.sites) {
            this.ui.showError('Could not load source configuration. Please refresh the page.');
            return;
        }
        
        this.currentQuery = query;
        this.isSearching = true;
        console.log('Searching for:', query);
        
        // === WIN 1: Instant card matches ===
        // Check card manifest FIRST — zero latency, shows results immediately
        const matchedCards = matchCards(query, this.cardManifest);
        
        // Switch to results state IMMEDIATELY (not after scraper completes)
        const sites = Object.entries(this.manifests.sites);
        this.ui.showState('results');
        this.ui.initStreamingResults(query, sites.length);
        // Adorama cross-check: a Partnerize-wrapped Adorama on-site search beside
        // the eBay results — mirrors the card 'buy new' CTA (build_site.py
        // adorama_search_url + partnerize_wrap; camref 1101l5Pw9q). Amazon retired
        // here 2026-07-25: Associates dropped (3-buy threshold missed, reapply
        // pending), so a live askmaddi-20 tag was untracked AND a ToS risk — same
        // reason the card path dropped it 2026-07-24. This buyer-intent slot now
        // points at the active relationship. Re-add an Amazon cross-check once
        // reapproval lands with a valid tag.
        const az = document.getElementById('adorama-crosscheck');
        if (az) {
            const dest = 'https://www.adorama.com/l/?searchinfo='
                + encodeURIComponent(query).replace(/%20/g, '+');
            az.href = 'https://adorama.prf.hn/click/camref:1101l5Pw9q/destination:' + dest;
            az.style.display = 'inline';
        }
        
        // Show matched review cards instantly (user sees content in <100ms)
        if (matchedCards.length > 0) {
            renderMatchedCards(matchedCards, '#matched-cards');
        } else {
            const mc = document.getElementById('matched-cards');
            if (mc) mc.style.display = 'none';
        }
        
        // Populate the results search bar with current query
        const resultsInput = document.getElementById('results-search-input');
        if (resultsInput) resultsInput.value = query;
        
        // === WIN 2: Stream scraper results as they arrive ===
        try {
            await this.streamSearch(query, sites);
        } catch (error) {
            console.error('Search failed:', error);
            // Don't switch to error state if we already have card matches showing
            if (matchedCards.length === 0) {
                this.ui.showError(error.message);
            }
        } finally {
            this.isSearching = false;
        }
    }
    
    async streamSearch(query, sites) {
        const allProducts = [];
        const diagnostics = {};
        let sourcesComplete = 0;

        // eBay is served by the official Browse API, not the scrape loop.
        // Pull it out of `sites` so it isn't double-fetched via /proxy.
        const scrapeSites = sites.filter(
            ([, manifest]) => (manifest.domain || '').toLowerCase() !== 'ebay.com'
        );
        const hasEbay = sites.length !== scrapeSites.length;
        const totalSources = scrapeSites.length + (hasEbay ? 1 : 0);

        // Fire all site fetches in parallel — each one renders as it arrives
        const fetchPromises = scrapeSites.map(async ([siteName, manifest]) => {
            const diag = { site: siteName, status: 'ok', htmlBytes: 0, containers: 0, products: 0 };
            try {
                this.ui.updateStreamingSource(siteName, 'fetching');
                
                const searchUrl = manifest.search.url_template.replace('{query}', encodeURIComponent(query));
                const html = await this.fetcher.fetchViaProxy(searchUrl);
                diag.htmlBytes = html ? html.length : 0;
                
                this.ui.updateStreamingSource(siteName, 'extracting');
                
                const products = await this.extractor.extract(html, manifest);
                diag.products = products.length;
                
                products.forEach(p => {
                    p.source = manifest.name;
                    p.sourceDomain = manifest.domain;
                });
                
                // === Stream: append these products to the grid NOW ===
                if (products.length > 0) {
                    allProducts.push(...products);
                    const ranked = this.ranker.rank([...allProducts], query);
                    this.ui.replaceProductGrid(ranked, query);
                }
                
                this.ui.updateStreamingSource(siteName, 'done');
                diagnostics[siteName] = diag;
                
            } catch (error) {
                console.error(`Failed to fetch ${siteName}:`, error);
                diag.status = 'error';
                diag.error = error.message;
                diagnostics[siteName] = diag;
                this.ui.updateStreamingSource(siteName, 'error');
            } finally {
                sourcesComplete++;
                this.ui.updateStreamingProgress(sourcesComplete, totalSources);
            }
        });

        // === eBay via official Browse API (parallel branch, not /proxy) ===
        if (hasEbay) {
            const diag = { site: 'eBay', status: 'ok', htmlBytes: 0, containers: 0, products: 0 };
            fetchPromises.push((async () => {
                try {
                    this.ui.updateStreamingSource('eBay', 'fetching');
                    const products = await this.fetcher.searchEbay(query);
                    diag.products = products.length;

                    products.forEach(p => {
                        p.source = 'eBay';
                        p.sourceDomain = 'ebay.com';
                    });

                    if (products.length > 0) {
                        allProducts.push(...products);
                        const ranked = this.ranker.rank([...allProducts], query);
                        this.ui.replaceProductGrid(ranked, query);
                    }

                    this.ui.updateStreamingSource('eBay', 'done');
                    diagnostics['eBay'] = diag;
                } catch (error) {
                    console.error('Failed to fetch eBay:', error);
                    diag.status = 'error';
                    diag.error = error.message;
                    diagnostics['eBay'] = diag;
                    this.ui.updateStreamingSource('eBay', 'error');
                } finally {
                    sourcesComplete++;
                    this.ui.updateStreamingProgress(sourcesComplete, totalSources);
                }
            })());
        }

        // Wait for all to complete
        await Promise.all(fetchPromises);
        
        // === Final pass: full dedup + rank ===
        if (allProducts.length > 0) {
            const deduped = this.deduper.deduplicate(allProducts);
            const ranked = this.ranker.rank(deduped, query);
            this.ui.replaceProductGrid(ranked, query);
            this.ui.finalizeResults(ranked.length, allProducts.length, deduped.length);
        } else if (allProducts.length === 0) {
            console.warn('ZERO RESULTS — diagnostics:', JSON.stringify(diagnostics, null, 2));
            this.ui.showEmptyResults(diagnostics);
        }
        
        this.sendAnalytics(query, sites.length);
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
