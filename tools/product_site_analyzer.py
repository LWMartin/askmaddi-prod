"""
Product Site Analyzer - Automated e-commerce site structure discovery
Discovers navigation patterns, product selectors, and site capabilities
"""
from typing import Dict, List, Optional, Tuple
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from sentence_transformers import SentenceTransformer
import logging

from product_field_library import ProductFieldLibrary

logger = logging.getLogger(__name__)


class ProductSiteAnalyzer:
    """
    Analyzes e-commerce sites to discover:
    - Product card containers
    - Field selectors (name, price, image, etc.)
    - Search interface
    - Pagination patterns
    - Category navigation
    
    Usage:
        analyzer = ProductSiteAnalyzer(field_library, model)
        manifest = analyzer.analyze_site(driver, test_query="laptop")
    """
    
    def __init__(self, field_library: ProductFieldLibrary, model: SentenceTransformer):
        self.library = field_library
        self.model = model
    
    def analyze_site(self, driver, test_query: str = "test") -> Dict:
        """
        Complete site analysis - discovers all patterns needed for registry.
        
        Args:
            driver: Selenium WebDriver instance
            test_query: Query to test search functionality
            
        Returns:
            Site manifest with all discovered patterns
        """
        logger.info(f"🔍 Analyzing site: {driver.current_url}")
        
        manifest = {
            'domain': self._extract_domain(driver.current_url),
            'homepage_url': driver.current_url,
            'search': self._discover_search(driver),
            'categories': self._discover_categories(driver),
            'product_patterns': {},
            'navigation': {},
            'discovered_at': None  # Will be set by caller
        }
        
        # Test search if we found search interface
        if manifest['search']['found']:
            logger.info(f"✅ Found search interface, testing with query: '{test_query}'")
            if self._execute_test_search(driver, manifest['search'], test_query):
                # Now on search results page - analyze product structure
                product_analysis = self._analyze_product_page(driver)
                manifest['product_patterns'] = product_analysis['patterns']
                manifest['navigation']['pagination'] = product_analysis['pagination']
                manifest['navigation']['results_info'] = product_analysis['results_info']
        else:
            logger.warning("⚠️ No search interface found, trying category navigation")
            # TODO: Implement category-based discovery
        
        return manifest
    
    def _extract_domain(self, url: str) -> str:
        """Extract clean domain from URL"""
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.netloc.replace('www.', '')
    
    def _discover_search(self, driver) -> Dict:
        """
        Discover search interface - input field and submit mechanism.
        
        Returns:
            {
                'found': bool,
                'input_selector': str,
                'input_type': 'id' | 'name' | 'class' | 'css',
                'submit_method': 'enter' | 'button' | 'form',
                'submit_selector': str (if button),
                'url_pattern': str (if direct URL construction works)
            }
        """
        logger.info("🔎 Discovering search interface...")
        
        search_info = {
            'found': False,
            'input_selector': None,
            'input_type': None,
            'submit_method': None,
            'submit_selector': None,
            'url_pattern': None
        }
        
        # Find all input elements
        inputs = driver.find_elements(By.TAG_NAME, 'input')
        
        for input_elem in inputs:
            # Get input attributes
            input_name = input_elem.get_attribute('name') or ''
            input_id = input_elem.get_attribute('id') or ''
            input_class = input_elem.get_attribute('class') or ''
            input_placeholder = input_elem.get_attribute('placeholder') or ''
            input_type = input_elem.get_attribute('type') or 'text'
            
            # Skip non-text inputs
            if input_type not in ['text', 'search']:
                continue
            
            # Combine clues
            clues = ' '.join([input_name, input_id, input_class, input_placeholder]).lower()
            
            # Check if this looks like a search field using semantic matching
            canonical, confidence, pattern = self.library.find_pattern_match(
                clues, 
                self.model
            )
            
            if canonical == 'Navigation.SearchBox' and confidence > 0.60:
                logger.info(f"✅ Found search input: {pattern} (confidence: {confidence:.2%})")
                
                # Determine best selector
                if input_id:
                    search_info['input_selector'] = input_id
                    search_info['input_type'] = 'id'
                elif input_name:
                    search_info['input_selector'] = input_name
                    search_info['input_type'] = 'name'
                else:
                    search_info['input_selector'] = input_class
                    search_info['input_type'] = 'class'
                
                # Find submit mechanism
                submit_info = self._find_submit_mechanism(driver, input_elem)
                search_info.update(submit_info)
                
                search_info['found'] = True
                break
        
        if not search_info['found']:
            logger.warning("❌ No search interface discovered")
        
        return search_info
    
    def _find_submit_mechanism(self, driver, input_elem: WebElement) -> Dict:
        """Find how to submit the search (button, enter, or form)"""
        submit_info = {
            'submit_method': 'enter',  # Default
            'submit_selector': None
        }
        
        try:
            # Try to find search button near input
            parent = input_elem.find_element(By.XPATH, '..')
            buttons = parent.find_elements(By.TAG_NAME, 'button')
            
            for button in buttons:
                button_text = button.text.lower()
                button_type = button.get_attribute('type') or ''
                button_class = button.get_attribute('class') or ''
                
                if any(word in button_text for word in ['search', 'go', 'find']):
                    submit_info['submit_method'] = 'button'
                    submit_info['submit_selector'] = button.get_attribute('class') or button.get_attribute('id')
                    logger.info(f"✅ Found submit button: {button_text}")
                    break
                elif button_type == 'submit':
                    submit_info['submit_method'] = 'button'
                    submit_info['submit_selector'] = button.get_attribute('class') or button.get_attribute('id')
                    break
        except:
            pass
        
        return submit_info
    
    def _execute_test_search(self, driver, search_info: Dict, query: str) -> bool:
        """
        Execute a test search to navigate to results page.
        
        Returns:
            True if search succeeded, False otherwise
        """
        try:
            # Find input element
            if search_info['input_type'] == 'id':
                input_elem = driver.find_element(By.ID, search_info['input_selector'])
            elif search_info['input_type'] == 'name':
                input_elem = driver.find_element(By.NAME, search_info['input_selector'])
            else:
                input_elem = driver.find_element(By.CLASS_NAME, search_info['input_selector'])
            
            # Clear and type query
            input_elem.clear()
            input_elem.send_keys(query)
            
            import time
            time.sleep(1)
            
            # Submit
            if search_info['submit_method'] == 'button' and search_info['submit_selector']:
                button = driver.find_element(By.CLASS_NAME, search_info['submit_selector'])
                button.click()
            else:
                from selenium.webdriver.common.keys import Keys
                input_elem.send_keys(Keys.RETURN)
            
            time.sleep(3)  # Wait for results to load
            
            logger.info(f"✅ Test search executed, now at: {driver.current_url}")
            return True
            
        except Exception as e:
            logger.error(f"❌ Test search failed: {e}")
            return False
    
    def _discover_categories(self, driver) -> List[Dict]:
        """
        Discover category/navigation links.
        Returns list of category links found.
        """
        categories = []
        
        # Find all links
        links = driver.find_elements(By.TAG_NAME, 'a')
        
        for link in links[:50]:  # Limit to first 50 to avoid noise
            try:
                link_text = link.text.strip()
                link_href = link.get_attribute('href') or ''
                link_class = link.get_attribute('class') or ''
                
                if not link_text or len(link_text) > 30:
                    continue
                
                # Check if looks like category link
                clues = f"{link_text} {link_class}".lower()
                canonical, confidence, pattern = self.library.find_pattern_match(
                    clues,
                    self.model
                )
                
                if canonical == 'Navigation.CategoryLink' and confidence > 0.55:
                    categories.append({
                        'text': link_text,
                        'url': link_href,
                        'confidence': confidence
                    })
            except:
                continue
        
        logger.info(f"📂 Found {len(categories)} potential category links")
        return categories[:10]  # Top 10 categories
    
    def _analyze_product_page(self, driver) -> Dict:
        """
        Analyze product results page to discover:
        - Product container pattern
        - Field selectors for each product attribute
        - Pagination
        """
        logger.info("📦 Analyzing product page structure...")
        
        analysis = {
            'patterns': {},
            'pagination': None,
            'results_info': {}
        }
        
        # Step 1: Find product containers
        containers = self._find_product_containers(driver)
        
        if not containers:
            logger.warning("❌ No product containers found")
            return analysis
        
        logger.info(f"✅ Found {len(containers)} product containers")
        
        # Step 2: Analyze first few products to discover field patterns
        sample_size = min(5, len(containers))
        field_patterns = self._extract_field_patterns(containers[:sample_size])
        
        analysis['patterns'] = field_patterns
        
        # Step 3: Discover pagination
        analysis['pagination'] = self._discover_pagination(driver)
        
        return analysis
    
    def _find_product_containers(self, driver) -> List[WebElement]:
        """
        Find product container elements (cards/items).
        Uses semantic matching on class names and structure.
        """
        candidates = []
        
        # Try common container patterns from library
        container_patterns = self.library.get_field_info('Container.ProductCard')
        
        for pattern in container_patterns['patterns'][:10]:  # Try top patterns
            try:
                elements = driver.find_elements(By.CLASS_NAME, pattern)
                if elements and len(elements) > 2:  # Should have multiple products
                    logger.info(f"✅ Found containers using pattern: {pattern}")
                    return elements
            except:
                continue
        
        # Fallback: Find repeating structures
        # Look for divs with data attributes (common for product items)
        for attr in ['data-asin', 'data-product-id', 'data-item-id', 'data-sku']:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, f'[{attr}]')
                if elements and len(elements) > 2:
                    logger.info(f"✅ Found containers using attribute: {attr}")
                    return elements
            except:
                continue
        
        # Try li tags with product-related classes (Best Buy pattern)
        try:
            elements = driver.find_elements(By.CSS_SELECTOR, 'li[class*="product"]')
            if elements and len(elements) > 2:
                logger.info(f"✅ Found containers using: li[class*=\"product\"]")
                return elements
        except:
            pass
        
        logger.warning("⚠️ Fallback: Using generic div search")
        # Last resort: find divs that repeat
        divs = driver.find_elements(By.TAG_NAME, 'div')
        # TODO: Implement smarter heuristic for finding repeating product divs
        
        return []
    
    def _extract_field_patterns(self, containers: List[WebElement]) -> Dict[str, Dict]:
        """
        Extract selector patterns for product fields by analyzing sample containers.
        
        Returns:
            {
                'Product.Name': {
                    'selectors': [list of CSS selectors],
                    'confidence': float,
                    'sample_values': [list of extracted values]
                },
                ...
            }
        """
        field_patterns = {}
        
        # Fields to discover
        target_fields = [
            'Product.Name',
            'Product.Price',
            'Product.Image',
            'Product.URL',
            'Product.Rating',
            'Product.Brand'
        ]
        
        for field in target_fields:
            patterns = self._discover_field_selectors(containers, field)
            if patterns:
                field_patterns[field] = patterns
        
        return field_patterns
    
    def _discover_field_selectors(
        self, 
        containers: List[WebElement], 
        canonical_field: str
    ) -> Optional[Dict]:
        """
        Discover CSS selectors for a specific field by trying known patterns.
        
        Returns:
            {
                'selectors': [ordered list of selectors, most reliable first],
                'confidence': float,
                'sample_values': [extracted samples],
                'extraction_method': 'text' | 'attribute'
            }
        """
        field_info = self.library.get_field_info(canonical_field)
        if not field_info:
            return None
        
        patterns = field_info['patterns'][:15]  # Try top 15 patterns
        successful_selectors = []
        sample_values = []
        
        for pattern in patterns:
            success_count = 0
            samples = []
            
            for container in containers:
                try:
                    # Try as class
                    elem = container.find_element(By.CLASS_NAME, pattern)
                    
                    # Extract value based on field type
                    if field_info.get('field_type') == 'image_url':
                        value = elem.get_attribute('src')
                    elif field_info.get('field_type') == 'url':
                        value = elem.get_attribute('href')
                    else:
                        value = elem.text.strip()
                    
                    if value:
                        success_count += 1
                        samples.append(value)
                except:
                    continue
            
            # If this selector worked for most containers, it's good
            if success_count >= len(containers) * 0.6:  # 60% success rate
                successful_selectors.append(f'.{pattern}')
                sample_values = samples
                
                logger.info(f"  ✅ {canonical_field}: found selector .{pattern} ({success_count}/{len(containers)} success)")
                break
        
        if not successful_selectors:
            logger.warning(f"  ❌ {canonical_field}: no reliable selector found")
            return None
        
        return {
            'selectors': successful_selectors,
            'confidence': success_count / len(containers),
            'sample_values': sample_values[:3],  # Top 3 samples
            'extraction_method': 'attribute' if field_info.get('attribute') else 'text'
        }
    
    def _discover_pagination(self, driver) -> Optional[Dict]:
        """
        Discover pagination patterns (next button, page numbers).
        
        Returns:
            {
                'type': 'button' | 'url_param' | 'load_more',
                'next_selector': str,
                'url_pattern': str (if applicable)
            }
        """
        pagination = {
            'found': False,
            'type': None,
            'next_selector': None,
            'url_pattern': None
        }
        
        # Look for "next" buttons/links
        links = driver.find_elements(By.TAG_NAME, 'a')
        
        for link in links:
            try:
                link_text = link.text.lower().strip()
                link_class = link.get_attribute('class') or ''
                link_aria = link.get_attribute('aria-label') or ''
                
                clues = f"{link_text} {link_class} {link_aria}".lower()
                
                if any(word in clues for word in ['next', 'next page', '›', '>', 'more']):
                    pagination['found'] = True
                    pagination['type'] = 'button'
                    pagination['next_selector'] = link.get_attribute('class') or link.text
                    logger.info(f"✅ Found pagination: {link_text or link_class}")
                    break
            except:
                continue
        
        # Check URL for page parameters
        current_url = driver.current_url
        if 'page=' in current_url or 'p=' in current_url:
            pagination['url_pattern'] = 'page_param'
        
        return pagination if pagination['found'] else None


class ProductExtractor:
    """
    Extracts product data using discovered patterns.
    Similar to DataExtractor but for products.
    """
    
    def __init__(self, field_library: ProductFieldLibrary):
        self.library = field_library
    
    def extract_products(
        self, 
        driver, 
        container_selector: str,
        field_patterns: Dict[str, Dict]
    ) -> List[Dict]:
        """
        Extract products using discovered patterns.
        
        Args:
            driver: Selenium WebDriver
            container_selector: CSS selector for product containers
            field_patterns: Discovered field patterns from analyzer
            
        Returns:
            List of normalized product dictionaries
        """
        products = []
        
        try:
            containers = driver.find_elements(By.CSS_SELECTOR, container_selector)
            
            for container in containers:
                product = self._extract_single_product(container, field_patterns)
                if product:
                    products.append(product)
        except Exception as e:
            logger.error(f"Error extracting products: {e}")
        
        return products
    
    def _extract_single_product(
        self, 
        container: WebElement, 
        field_patterns: Dict
    ) -> Optional[Dict]:
        """Extract single product using field patterns"""
        product = {}
        
        for field, pattern_info in field_patterns.items():
            try:
                selectors = pattern_info['selectors']
                extraction_method = pattern_info.get('extraction_method', 'text')
                
                for selector in selectors:
                    try:
                        elem = container.find_element(By.CSS_SELECTOR, selector)
                        
                        if extraction_method == 'attribute':
                            # Determine which attribute
                            if 'Image' in field:
                                value = elem.get_attribute('src')
                            elif 'URL' in field:
                                value = elem.get_attribute('href')
                            else:
                                value = elem.text.strip()
                        else:
                            value = elem.text.strip()
                        
                        if value:
                            product[field] = value
                            break
                    except:
                        continue
            except:
                continue
        
        # Require at minimum a name
        return product if 'Product.Name' in product else None