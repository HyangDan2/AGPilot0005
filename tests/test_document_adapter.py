"""Unit tests for DocumentAdapter, AGToolsHttpAdapter, and CompositeDocumentAdapter."""
import pytest
from unittest.mock import patch, MagicMock
from app.adapters.document import (
    MockDocumentAdapter,
    AGToolsHttpAdapter,
    CompositeDocumentAdapter,
)

@pytest.mark.asyncio
async def test_mock_document_adapter():
    adapter = MockDocumentAdapter()
    
    # 1. Outline
    outline = await adapter.get_outline("hd2lab-guide")
    assert outline["doc_id"] == "hd2lab-guide"
    assert len(outline["outline"]) == 4

    # 2. Not found
    not_found = await adapter.get_outline("non-existent")
    assert not_found.get("error") is True

    # 3. Read section
    sec = await adapter.read_section("hd2lab-guide", "sec-2")
    assert "AI Gateway" in sec["content"]
    assert "chk-2-1" in sec["chunk_ids"]

    # 4. Search
    sr = await adapter.search("hd2lab-guide", "routing", limit=2)
    assert sr["total_matches"] >= 1
    assert "sec-2-1" == sr["results"][0]["section_id"]

    # 5. Read chunk
    chk = await adapter.read_chunk("hd2lab-guide", "chk-1-1")
    assert chk["chunk_id"] == "chk-1-1"
    assert chk["token_count"] == 42

    # 6. Figure
    fig = await adapter.get_figure_info("hd2lab-guide", "fig-hd2lab-logo")
    assert fig["figure_id"] == "fig-hd2lab-logo"
    assert "Brand Logo" in fig["caption"]

@pytest.mark.asyncio
async def test_agtools_http_adapter_error_handling():
    # Target nonexistent port to test network failure gracefully
    adapter = AGToolsHttpAdapter(base_url="http://127.0.0.1:59999/api/v1", token="test-token", timeout=1.0)
    
    # Composite adapter should catch error and fall back to mock
    mock = MockDocumentAdapter()
    composite = CompositeDocumentAdapter(http_adapter=adapter, mock_adapter=mock)
    
    outline = await composite.get_outline("hd2lab-guide")
    assert outline["doc_id"] == "hd2lab-guide"
    assert len(outline["outline"]) >= 1

    search_res = await composite.search("hd2lab-guide", "Gateway")
    assert search_res["total_matches"] >= 1

@pytest.mark.asyncio
async def test_agtools_http_adapter_mocked_success():
    adapter = AGToolsHttpAdapter(base_url="http://agtools.test/api/v1", token="test-token")
    
    with patch("httpx.AsyncClient.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "document_id": "doc-live-1",
            "status": "ready",
            "sections": [{"section_id": "s1", "title": "Chapter 1", "level": 1}]
        }
        mock_get.return_value = mock_resp

        res = await adapter.get_outline("doc-live-1")
        assert res["doc_id"] == "doc-live-1"
        assert res["status"] == "ready"
        assert len(res["outline"]) == 1
