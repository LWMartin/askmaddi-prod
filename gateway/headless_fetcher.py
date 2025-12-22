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
        options.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')
        
        try:
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
            raise


# Site-specific configurations
SITE_CONFIG = {
    'ebay.com': {
        'needs_headless': False,
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