"""
RDF Triple Generator - Convert site manifests to semantic triples
Enables graph-based reasoning and knowledge representation
"""
from typing import Dict, List, Optional
from datetime import datetime
import json


class RDFTripleGenerator:
    """
    Converts site discovery manifests to RDF triples.
    
    Triple format: (subject, predicate, object)
    
    Example:
        (newegg.com, hasField, Product.Name)
        (Product.Name, hasSelector, ".item-title")
        (Product.Name, hasConfidence, 0.95)
    
    Usage:
        generator = RDFTripleGenerator()
        triples = generator.generate_from_manifest(manifest)
        turtle = generator.to_turtle(triples)
    """
    
    def __init__(self, base_uri: str = "http://askmaddi.com/registry#"):
        self.base_uri = base_uri
        self.namespaces = {
            'am': base_uri,
            'rdf': 'http://www.w3.org/1999/02/22-rdf-syntax-ns#',
            'rdfs': 'http://www.w3.org/2000/01/rdf-schema#',
            'xsd': 'http://www.w3.org/2001/XMLSchema#',
        }
    
    def generate_from_manifest(self, manifest: Dict) -> List[tuple]:
        """
        Generate RDF triples from a site manifest.
        
        Args:
            manifest: Site discovery manifest (JSON)
            
        Returns:
            List of (subject, predicate, object) triples
        """
        triples = []
        
        # Site entity
        site_uri = self._make_uri(manifest['domain'])
        
        # Basic site properties
        triples.extend([
            (site_uri, 'rdf:type', 'am:EcommerceSite'),
            (site_uri, 'am:domain', manifest['domain']),
            (site_uri, 'am:homepageUrl', manifest.get('homepage_url', '')),
        ])
        
        # Search interface
        if manifest.get('search', {}).get('found'):
            search_uri = self._make_uri(f"{manifest['domain']}/search")
            triples.extend(self._generate_search_triples(site_uri, search_uri, manifest['search']))
        
        # Product patterns (field selectors)
        if manifest.get('product_patterns'):
            for field, pattern_info in manifest['product_patterns'].items():
                field_uri = self._make_uri(f"{manifest['domain']}/{field}")
                triples.extend(self._generate_field_triples(site_uri, field_uri, field, pattern_info))
        
        # Categories
        if manifest.get('categories'):
            for idx, category in enumerate(manifest['categories']):
                cat_uri = self._make_uri(f"{manifest['domain']}/category/{idx}")
                triples.extend(self._generate_category_triples(site_uri, cat_uri, category))
        
        # Navigation
        if manifest.get('navigation', {}).get('pagination'):
            pag_uri = self._make_uri(f"{manifest['domain']}/pagination")
            triples.extend(self._generate_pagination_triples(site_uri, pag_uri, manifest['navigation']['pagination']))
        
        # Metadata
        if manifest.get('discovered_at'):
            triples.append((site_uri, 'am:discoveredAt', manifest['discovered_at']))
        
        return triples
    
    def _generate_search_triples(self, site_uri: str, search_uri: str, search_info: Dict) -> List[tuple]:
        """Generate triples for search interface"""
        triples = [
            (site_uri, 'am:hasSearchInterface', search_uri),
            (search_uri, 'rdf:type', 'am:SearchInterface'),
        ]
        
        if search_info.get('input_selector'):
            triples.append((search_uri, 'am:inputSelector', search_info['input_selector']))
        if search_info.get('input_type'):
            triples.append((search_uri, 'am:inputType', search_info['input_type']))
        if search_info.get('submit_method'):
            triples.append((search_uri, 'am:submitMethod', search_info['submit_method']))
        if search_info.get('url_pattern'):
            triples.append((search_uri, 'am:urlPattern', search_info['url_pattern']))
        
        return triples
    
    def _generate_field_triples(
        self, 
        site_uri: str, 
        field_uri: str, 
        field_name: str, 
        pattern_info: Dict
    ) -> List[tuple]:
        """Generate triples for a product field pattern"""
        triples = [
            (site_uri, 'am:hasField', field_uri),
            (field_uri, 'rdf:type', 'am:ProductField'),
            (field_uri, 'am:fieldName', field_name),
        ]
        
        # Selectors
        if pattern_info.get('selectors'):
            for selector in pattern_info['selectors']:
                triples.append((field_uri, 'am:selector', selector))
        
        # Confidence
        if 'confidence' in pattern_info:
            triples.append((field_uri, 'am:confidence', f"{pattern_info['confidence']:.2f}"))
        
        # Extraction method
        if pattern_info.get('extraction_method'):
            triples.append((field_uri, 'am:extractionMethod', pattern_info['extraction_method']))
        
        # Sample values (for validation)
        if pattern_info.get('sample_values'):
            for sample in pattern_info['sample_values'][:3]:  # Max 3 samples
                triples.append((field_uri, 'am:sampleValue', sample))
        
        return triples
    
    def _generate_category_triples(self, site_uri: str, cat_uri: str, category: Dict) -> List[tuple]:
        """Generate triples for a category link"""
        triples = [
            (site_uri, 'am:hasCategory', cat_uri),
            (cat_uri, 'rdf:type', 'am:Category'),
        ]
        
        if category.get('text'):
            triples.append((cat_uri, 'am:categoryName', category['text']))
        if category.get('url'):
            triples.append((cat_uri, 'am:categoryUrl', category['url']))
        if 'confidence' in category:
            triples.append((cat_uri, 'am:confidence', f"{category['confidence']:.2f}"))
        
        return triples
    
    def _generate_pagination_triples(self, site_uri: str, pag_uri: str, pagination: Dict) -> List[tuple]:
        """Generate triples for pagination"""
        triples = [
            (site_uri, 'am:hasPagination', pag_uri),
            (pag_uri, 'rdf:type', 'am:Pagination'),
        ]
        
        if pagination.get('type'):
            triples.append((pag_uri, 'am:paginationType', pagination['type']))
        if pagination.get('next_selector'):
            triples.append((pag_uri, 'am:nextSelector', pagination['next_selector']))
        if pagination.get('url_pattern'):
            triples.append((pag_uri, 'am:urlPattern', pagination['url_pattern']))
        
        return triples
    
    def _make_uri(self, local_name: str) -> str:
        """Create a URI from a local name"""
        # Clean the local name
        clean = local_name.replace('http://', '').replace('https://', '')
        clean = clean.replace('/', '_').replace('.', '_').replace(':', '_')
        return f"am:{clean}"
    
    def to_turtle(self, triples: List[tuple]) -> str:
        """
        Convert triples to Turtle (TTL) format.
        
        Turtle is a human-readable RDF serialization format.
        """
        lines = []
        
        # Prefixes
        lines.append("# AskMaddi Registry - RDF Triples")
        lines.append(f"# Generated: {datetime.now().isoformat()}\n")
        
        for prefix, uri in self.namespaces.items():
            lines.append(f"@prefix {prefix}: <{uri}> .")
        lines.append("")
        
        # Group by subject
        subjects = {}
        for s, p, o in triples:
            if s not in subjects:
                subjects[s] = []
            subjects[s].append((p, o))
        
        # Write triples
        for subject, predicates in subjects.items():
            lines.append(f"{subject}")
            for i, (pred, obj) in enumerate(predicates):
                is_last = i == len(predicates) - 1
                
                # Format object (add quotes for strings, keep URIs as-is)
                if obj.startswith('am:') or obj.startswith('rdf:') or obj.startswith('rdfs:'):
                    formatted_obj = obj
                elif obj.replace('.', '').replace('-', '').isdigit():
                    formatted_obj = obj  # Numeric
                else:
                    # String - escape quotes and wrap
                    formatted_obj = f'"{obj.replace(chr(34), chr(92)+chr(34))}"'
                
                separator = " ." if is_last else " ;"
                lines.append(f"    {pred} {formatted_obj}{separator}")
            lines.append("")
        
        return "\n".join(lines)
    
    def to_json_ld(self, triples: List[tuple]) -> Dict:
        """
        Convert triples to JSON-LD format.
        
        JSON-LD is JSON-based linked data format - easier to work with in JS.
        """
        # Group by subject
        entities = {}
        
        for s, p, o in triples:
            if s not in entities:
                entities[s] = {
                    '@id': s,
                    '@type': []
                }
            
            # Handle type specially
            if p == 'rdf:type':
                entities[s]['@type'].append(o)
            else:
                # Convert predicate to JSON-LD key
                key = p.split(':')[1] if ':' in p else p
                
                if key not in entities[s]:
                    entities[s][key] = []
                
                entities[s][key].append(o)
        
        # Convert lists with single items to single values
        for entity in entities.values():
            for key, value in entity.items():
                if key not in ['@id', '@type'] and isinstance(value, list) and len(value) == 1:
                    entity[key] = value[0]
        
        return {
            '@context': {
                'am': self.base_uri,
                'rdf': self.namespaces['rdf'],
                'rdfs': self.namespaces['rdfs'],
            },
            '@graph': list(entities.values())
        }
    
    def validate_triples(self, triples: List[tuple]) -> Dict:
        """
        Validate triple structure and completeness.
        
        Returns:
            {
                'valid': bool,
                'errors': List[str],
                'warnings': List[str],
                'stats': Dict
            }
        """
        errors = []
        warnings = []
        
        # Check for empty
        if not triples:
            errors.append("No triples generated")
            return {'valid': False, 'errors': errors, 'warnings': warnings, 'stats': {}}
        
        # Check structure
        for i, triple in enumerate(triples):
            if len(triple) != 3:
                errors.append(f"Triple {i} has {len(triple)} elements, expected 3")
            
            s, p, o = triple
            
            # Subject should be URI
            if not (s.startswith('am:') or s.startswith('http')):
                warnings.append(f"Subject '{s}' doesn't look like a URI")
            
            # Predicate should be URI
            if not (':' in p or p.startswith('http')):
                warnings.append(f"Predicate '{p}' doesn't look like a URI")
        
        # Check required properties
        subjects = {t[0] for t in triples}
        has_types = any(t[1] == 'rdf:type' for t in triples)
        
        if not has_types:
            warnings.append("No rdf:type declarations found")
        
        # Stats
        stats = {
            'total_triples': len(triples),
            'unique_subjects': len(subjects),
            'unique_predicates': len({t[1] for t in triples}),
            'avg_triples_per_subject': len(triples) / len(subjects) if subjects else 0
        }
        
        return {
            'valid': len(errors) == 0,
            'errors': errors,
            'warnings': warnings,
            'stats': stats
        }


class ManifestToRegistryConverter:
    """
    High-level converter from discovery manifests to registry entries.
    Combines RDF generation with practical registry format.
    """
    
    def __init__(self):
        self.rdf_generator = RDFTripleGenerator()
    
    def convert(self, manifest: Dict) -> Dict:
        """
        Convert manifest to registry entry.
        
        Returns both practical JSON format AND semantic RDF triples.
        """
        # Generate RDF triples
        triples = self.rdf_generator.generate_from_manifest(manifest)
        
        # Validate
        validation = self.rdf_generator.validate_triples(triples)
        
        # Create registry entry
        entry = {
            'domain': manifest['domain'],
            'name': manifest.get('name', manifest['domain']),
            'homepage_url': manifest.get('homepage_url', ''),
            
            # Search
            'search': manifest.get('search', {}),
            
            # Fields (simplified for browser use)
            'fields': self._simplify_fields(manifest.get('product_patterns', {})),
            
            # Containers
            'containers': self._extract_containers(manifest),
            
            # Metadata
            'discovered_at': manifest.get('discovered_at', datetime.now().isoformat()),
            'quality_score': manifest.get('quality_score', 0),
            'validated': manifest.get('validated', False),
            
            # RDF representation
            'rdf': {
                'triples_count': len(triples),
                'turtle': self.rdf_generator.to_turtle(triples),
                'json_ld': self.rdf_generator.to_json_ld(triples),
            },
            
            # Validation
            'validation': validation
        }
        
        return entry
    
    def _simplify_fields(self, product_patterns: Dict) -> Dict:
        """Simplify field patterns for browser use"""
        simplified = {}
        
        for field, info in product_patterns.items():
            simplified[field] = {
                'selectors': info.get('selectors', []),
                'confidence': info.get('confidence', 0.0),
                'method': info.get('extraction_method', 'text')
            }
        
        return simplified
    
    def _extract_containers(self, manifest: Dict) -> List[str]:
        """Extract container selectors from manifest"""
        containers = []
        
        # From navigation
        if manifest.get('navigation', {}).get('container_selector'):
            containers.append(manifest['navigation']['container_selector'])
        
        # Add common fallbacks
        containers.extend([
            '[data-product-id]',
            '[data-sku]',
            '.product-item',
            '.item'
        ])
        
        # Deduplicate while preserving order
        seen = set()
        unique_containers = []
        for c in containers:
            if c not in seen:
                seen.add(c)
                unique_containers.append(c)
        
        return unique_containers