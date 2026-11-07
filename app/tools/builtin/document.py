"""Builtin Document Tools: document.get_outline, document.read_section."""
from typing import Any, Dict
from app.adapters.document import document_adapter

async def get_outline(doc_id: str) -> Dict[str, Any]:
    """Retrieves the outline and hierarchical section list of a document."""
    return await document_adapter.get_outline(doc_id)

async def read_section(doc_id: str, section_id: str) -> Dict[str, Any]:
    """Reads the full text content of a specific section in a document."""
    return await document_adapter.read_section(doc_id, section_id)
