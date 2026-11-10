"""Builtin Document Tools: get_outline, read_section, search, read_chunk, get_figure_info."""
from typing import Any, Dict
from app.adapters.document import document_adapter

async def get_outline(doc_id: str) -> Dict[str, Any]:
    """Retrieves the outline and hierarchical section list of a document."""
    return await document_adapter.get_outline(doc_id)

async def read_section(doc_id: str, section_id: str) -> Dict[str, Any]:
    """Reads the full text content of a specific section in a document."""
    return await document_adapter.read_section(doc_id, section_id)

async def search(doc_id: str, query: str, limit: int = 5) -> Dict[str, Any]:
    """Searches document content using keyword/semantic full-text search with ranked snippet chunks."""
    return await document_adapter.search(doc_id=doc_id, query=query, limit=limit)

async def read_chunk(doc_id: str, chunk_id: str) -> Dict[str, Any]:
    """Reads an exact token-bounded chunk of text with its metadata and source location."""
    return await document_adapter.read_chunk(doc_id=doc_id, chunk_id=chunk_id)

async def get_figure_info(doc_id: str, figure_id: str) -> Dict[str, Any]:
    """Retrieves visual asset metadata, caption, slide/page location, and asset download URL for a figure."""
    return await document_adapter.get_figure_info(doc_id=doc_id, figure_id=figure_id)
