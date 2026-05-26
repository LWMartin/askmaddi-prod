"""
Headless Fetcher
================
Uses undetected-chromedriver to fetch JS-rendered pages.
Source sites see our server, not the user.

Privacy: User IP never touches source sites.
"""

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import random


class HeadlessFetcher:
    """
    Manages a pool of headless Chrome instances for fetching JS-rendered pages.
    """
    
    def __init__(self):
        self.driver = None
        self.initialized = False
    
    def _detect_chrome_major(self):
        """Detect installed Chrome major version so uc fetches a matching driver.
        
        uc by default grabs the latest chromedriver, which can be ahead of the
        system Chrome (e.g. driver 149 vs Chrome 148.x → SessionNotCreated).
        Pinning version_main keeps the pair aligned through auto-updates.
        Returns int major version, or None if detection fails (uc falls back to default).
        """
        import shutil
        import subprocess
        chrome_path = (
            shutil.which('google-chrome')
            or shutil.which('chromium')
            or shutil.which('chromium-browser')
        )
        if not chrome_path:
            print("[headless] WARN: no chrome binary found, uc will use default driver")
            return None
        try:
            out = subprocess.check_output(
                [chrome_path, '--version'], stderr=subprocess.STDOUT, timeout=5
            ).decode().strip()
            # Output looks like: "Google Chrome 148.0.7778.96"
            parts = out.split()
            for token in parts:
                if token[0].isdigit() and '.' in token:
                    major = int(token.split('.')[0])
                    print(f"[headless] detected Chrome major {major} from: {out}")
                    return major
            print(f"[headless] WARN: could not parse Chrome version from: {out}")
            return None
        except Exception as e:
            print(f"[headless] WARN: Chrome version detect failed: {type(e).__name__}: {e}")
            return None
    
    def start(self):
        """Initialize the headless browser"""
        if self.initialized:
            return
        
        print("Starting headless browser...")
        
        options = uc.ChromeOptions()
        options.add_argument('--headless=new')
        options.add_argument('--disable-gpu')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_argument('--window-size=1920,1080')
        # No manual user-agent — let Chrome use its real UA string.
        # Hardcoding Chrome/120 while actual binary is 148 is a TLS/UA
        # fingerprint mismatch that CDN bot-detection (Akamai, Cloudflare) catches.
        
        chrome_major = self._detect_chrome_major()
        
        try:
            if chrome_major is not None:
                self.driver = uc.Chrome(options=options, version_main=chrome_major)
            else:
                self.driver = uc.Chrome(options=options)
            self.initialized = True
            print("Headless browser ready!")
        except Exception as e:
            print(f"Failed to start headless browser: {e}")
            raise
    
    def stop(self):
        """Cleanup"""
        if self.driver:
            try:
                self.driver.quit()
            except:
                pass
            self.driver = None
            self.initialized = False
    
    def is_healthy(self):
        """Check if the driver is still responsive."""
        if not self.initialized or not self.driver:
            return False
        try:
            # Quick probe — if the session is dead this throws
            _ = self.driver.title
            return True
        except Exception:
            return False

    def _reinitialize(self):
        """Kill the old driver and start fresh."""
        print("[headless] Driver unhealthy — reinitializing...")
        self.stop()
        self.start()

    def fetch(self, url, wait_for_selector=None, wait_time=3):
        """
        Fetch a URL and return the rendered HTML.
        
        Args:
            url: The URL to fetch
            wait_for_selector: CSS selector to wait for (optional)
            wait_time: Seconds to wait for JS to render
        
        Returns:
            Rendered HTML string
        """
        if not self.initialized:
            self.start()
        
        # Health check before fetch — catches stale/crashed driver
        if not self.is_healthy():
            self._reinitialize()
        
        try:
            print(f"Fetching: {url[:80]}...")
            
            self.driver.get(url)
            
            # Wait for specific element if provided
            if wait_for_selector:
                try:
                    WebDriverWait(self.driver, 10).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, wait_for_selector))
                    )
                except:
                    pass  # Continue anyway
            
            # Additional wait for JS to render
            time.sleep(wait_time + random.uniform(0.5, 1.5))
            
            # Scroll to trigger lazy loading
            self.driver.execute_script("window.scrollTo(0, 500)")
            time.sleep(0.5)
            
            # Get rendered HTML
            html = self.driver.page_source
            
            print(f"Fetched {len(html)} bytes from {url[:50]}...")
            return html
            
        except Exception as e:
            print(f"Fetch error: {e}")
            # If the driver died mid-fetch, mark for reinit on next call
            if not self.is_healthy():
                print("[headless] Driver died during fetch — will reinitialize on next request")
                self.stop()
            raise


# Site-specific configurations
SITE_CONFIG = {
    'ebay.com': {
        'needs_headless': True,   # 2026-05-26: eBay now returns 403 on simple fetch
        'wait_for': '.s-item__title',
        'wait_time': 3
    },
    'amazon.com': {
        'needs_headless': True,
        'wait_for': '[data-component-type="s-search-result"]',
        'wait_time': 3
    },
    'walmart.com': {
        'needs_headless': True,
        'wait_for': '[data-item-id]',
        'wait_time': 4
    },
    'bestbuy.com': {
        'needs_headless': True,  # Works with simple fetch
        'wait_for': None,
        'wait_time': 0
    },
    'newegg.com': {
        'needs_headless': False,  # Works with simple fetch
        'wait_for': None,
        'wait_time': 0
    }
}


def get_site_config(url):
    """Get configuration for a URL's domain"""
    for domain, config in SITE_CONFIG.items():
        if domain in url:
            return config
    
    # Default: try simple fetch first
    return {
        'needs_headless': False,
        'wait_for': None,
        'wait_time': 0
    }


def needs_headless(url):
    """Check if URL requires headless browser"""
    config = get_site_config(url)
    return config.get('needs_headless', False)