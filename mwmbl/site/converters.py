"""Converters between structured content and Document format"""
from mwmbl.site.schemas import structured_content_pb2 as pb
from mwmbl.tinysearchengine.indexer import Document
import logging

logger = logging.getLogger(__name__)

# Default max lengths (can be overridden by settings)
DEFAULT_MAX_EXTRACT_LENGTH = 1000


def structured_to_document(content: pb.StructuredContent) -> Document:
    """
    Convert structured content to Document for backward compatibility.
    
    Args:
        content: StructuredContent Protobuf message
        
    Returns:
        Document object
    """
    if content.HasField('docs_python'):
        return docs_python_to_document(content.url, content.docs_python)
    elif content.HasField('github'):
        return github_to_document(content.url, content.github)
    else:
        raise ValueError(f"Unknown structured content type for URL: {content.url}")


def docs_python_to_document(url: str, py: pb.DocsPythonOrgContent) -> Document:
    """
    Convert Python docs structured content to Document.
    
    Args:
        url: The URL
        py: DocsPythonOrgContent message
        
    Returns:
        Document object
    """
    max_extract_length = DEFAULT_MAX_EXTRACT_LENGTH
    
    title = py.definition
    
    # Build extract from description and version info
    extract_parts = []
    
    # Add description (truncate if needed)
    if py.description:
        desc = py.description[:max_extract_length]
        extract_parts.append(desc)
    
    # Add version info
    if py.version:
        extract_parts.append(f"(Python {py.version})")
    
    if py.added_in_version:
        extract_parts.append(f"Added in {py.added_in_version}.")
    
    if py.deprecated_in_version:
        extract_parts.append(f"Deprecated in {py.deprecated_in_version}.")
    
    # Add change history (first change only to save space)
    if py.changed_in_version:
        first_change = py.changed_in_version[0]
        change_desc = first_change.description[:100] if first_change.description else ""
        extract_parts.append(f"Changed in {first_change.version}: {change_desc}")
    
    extract = " ".join(extract_parts)[:max_extract_length]
    
    return Document(title=title, url=url, extract=extract)


def github_to_document(url: str, gh: pb.GitHubContent) -> Document:
    """
    Convert GitHub structured content to Document.
    
    Args:
        url: The URL
        gh: GitHubContent message
        
    Returns:
        Document object
    """
    max_extract_length = DEFAULT_MAX_EXTRACT_LENGTH
    
    title = gh.title
    
    # Build metadata prefix
    metadata_parts = []
    if gh.stars > 0:
        metadata_parts.append(f"⭐ {gh.stars}")
    if gh.language:
        metadata_parts.append(gh.language)
    if gh.license:
        metadata_parts.append(gh.license)
    
    # Combine metadata and extract
    extract = gh.extract
    if metadata_parts:
        extract = f"[{' | '.join(metadata_parts)}] {extract}"
    
    # Add repository description if different from extract
    if gh.description and gh.description not in gh.extract:
        extract = f"{gh.description} - {extract}"
    
    extract = extract[:max_extract_length]
    
    return Document(title=title, url=url, extract=extract)
