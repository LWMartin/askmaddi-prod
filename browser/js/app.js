import { Fetcher } from './fetcher.js';
import { Extractor } from './extractor.js';
import { Deduper } from './deduper.js';
import { Ranker } from './ranker.js';
import { UI } from './ui.js';
import { loadManifest, matchCards, renderMatchedCards } from './cards.js';
import { arrangeResults } from './precise.js?v=3';

// === Lane A precise-search switch (2026-08-27) ===
// false → legacy client-side fan-out (streamSearch). true → route ALL search
// through the server-side /search Sieve (eBay Used + Adorama New, classified /
// ranked / deduped / bucketed). Until flipped, opt-in PER QUERY via ?precise=1
// for testing. Reversible: the legacy path is untouched.
const PRECISE_SEARCH = true;

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
        // Retailer cross-checks: buyer-intent exits beside the results.
        //
        // Adorama — a Partnerize-wrapped on-site search, mirroring the card
        // 'buy new' CTA (build_site.py adorama_search_url + partnerize_wrap;
        // camref 1101l5Pw9q). This is the PRICED relationship: we are licensed
        // to display Adorama feed pricing.
        //
        // Amazon — RESTORED 2026-07-27 (Associates reinstated, askmaddi20-20)
        // as a LINK ONLY. We hold no Creators API credentials, so displaying
        // Amazon price, availability, star rating, review count or imagery is
        // not permitted — which is why Amazon was also dropped from the
        // gateway's ENABLED_SITES: we no longer scrape it at all. A tagged
        // search link surfaces nothing of Amazon's and still earns the click.
        // NEVER interpolate a price into this label.
        const az = document.getElementById('adorama-crosscheck');
        if (az) {
            const dest = 'https://www.adorama.com/l/?searchinfo='
                + encodeURIComponent(query).replace(/%20/g, '+');
            az.href = 'https://adorama.prf.hn/click/camref:1101l5Pw9q/destination:' + dest;
            az.style.display = 'inline';
        }
        const amz = document.getElementById('amazon-crosscheck');
        if (amz) {
            amz.href = 'https://www.amazon.com/s?k='
                + encodeURIComponent(query).replace(/%20/g, '+')
                + '&tag=askmaddi20-20';
            amz.style.display = 'inline';
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
            const usePrecise = PRECISE_SEARCH ||
                new URLSearchParams(window.location.search).has('precise');
            if (usePrecise) {
                await this.preciseSearch(query);
            } else {
                await this.streamSearch(query, sites);
            }
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

    async preciseSearch(query) {
        // Lane A STREAMING: fan out both sanctioned sources in parallel, render
        // as each arrives, and BYPASS a slow link via a per-source timeout —
        // "print as found", never block on the slowest. Precision (whole-token
        // relevance + accessory demotion + source x condition interleave) is
        // cheap and runs client-side on every arrival, so it never blocks the
        // stream. Heavy Sieve rungs (spine identity, Qwen3) are out of the hot
        // path by design.
        const products = [];
        const render = () => {
            const arranged = arrangeResults(query, products);
            if (arranged.length > 0) this.ui.replaceProductGrid(arranged, query);
        };
        const withTimeout = (p, ms) => Promise.race([
            p,
            new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), ms)),
        ]);
        const pull = async (name, domain, fn) => {
            this.ui.updateStreamingSource(name, 'fetching');
            try {
                const items = await withTimeout(fn(), 7000);   // slow link → bypass
                items.forEach(it => { it.source = name; it.sourceDomain = domain; });
                products.push(...items);
                render();                                      // print as found
                this.ui.updateStreamingSource(name, 'done');
            } catch (e) {
                console.error(`${name} source failed:`, e);
                this.ui.updateStreamingSource(name, 'error');
            }
        };
        await Promise.all([
            pull('Adorama', 'adorama.com', () => this.fetcher.searchAdorama(query)),
            pull('eBay', 'ebay.com', () => this.fetcher.searchEbay(query)),
        ]);
        const arranged = arrangeResults(query, products);
        if (arranged.length > 0) {
            this.ui.replaceProductGrid(arranged, query);
            this.ui.finalizeResults(arranged.length, products.length, arranged.length);
        } else {
            this.ui.showEmptyResults({ query, sources: products.length });
        }
        this.sendAnalytics(query, 2);
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
