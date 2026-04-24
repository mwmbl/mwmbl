"""Registry for site-specific parsers"""
from typing import Optional, Callable
from urllib.parse import urlparse
import logging

logger = logging.getLogger(__name__)

# Parser functions will be registered here
# Format: {domain: parse_function}
_PARSER_REGISTRY: dict[str, Callable] = {}


def register_parser(domain: str):
    """
    Decorator to register a parser for a domain.
    
    Usage:
        @register_parser('docs.python.org')
        def parse_docs_python_org(url: str, html: str) -> list[StructuredContent]:
            ...
    """
    def decorator(func: Callable):
        _PARSER_REGISTRY[domain] = func
        logger.info(f"Registered parser for {domain}")
        return func
    return decorator


def get_parser_for_url(url: str) -> Optional[Callable]:
    """
    Get the appropriate parser for a URL.
    
    Args:
        url: The URL to find a parser for
        
    Returns:
        Parser function if found, None otherwise
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc
        
        # Try exact match first
        if domain in _PARSER_REGISTRY:
            return _PARSER_REGISTRY[domain]
        
        # Try without www prefix
        if domain.startswith('www.'):
            domain_without_www = domain[4:]
            if domain_without_www in _PARSER_REGISTRY:
                return _PARSER_REGISTRY[domain_without_www]
        
        return None
        
    except Exception as e:
        logger.error(f"Error parsing URL {url}: {e}")
        return None


def list_registered_parsers() -> list[str]:
    """List all registered parser domains"""
    return list(_PARSER_REGISTRY.keys())
