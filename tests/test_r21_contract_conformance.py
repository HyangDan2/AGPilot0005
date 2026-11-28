"""`dk/v1` contract freeze: golden field sets and cross-repo conformance.

The contract is the deliverable of WP-01. Two producers implement it (AGCore as reference,
AGTools as engine). This test pins the wire field names so a rename is a deliberate, reviewed
change, and — when the AGTools source is reachable in the same sandbox — compares both copies
model by model.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from app.knowledge import contracts


GOLDEN = {
    "DocumentRef": {
        "namespace", "document_id", "name", "mime_type", "revision", "content_hash",
        "size_bytes", "status", "index_state", "version", "metadata",
    },
    "Locator": {
        "kind", "page", "slide", "sheet", "cell", "range", "section", "paragraph", "row",
        "shape", "image", "char_start", "char_end", "path",
    },
    "Citation": {"citation_id", "document", "locator", "quote", "content_hash", "revision_status"},
    "ReadRequest": {
        "document_id", "revision", "mode", "pages", "slide", "sheet", "cell_range", "section",
        "max_chars", "max_rows", "include_tables", "include_images", "include_notes", "cursor",
    },
    "ReadResponse": {
        "contract_version", "document", "mode", "nodes", "citations", "truncated", "next_cursor",
        "warnings", "stats",
    },
    "SearchRequest": {"query", "mode", "top_k", "filters", "document_ids", "allow_degraded", "rerank"},
    "SearchHit": {
        "hit_id", "document", "locator", "excerpt", "score", "keyword_score", "vector_score",
        "matched_by", "citation", "chunk_id",
    },
    "SearchResponse": {
        "contract_version", "query", "mode_requested", "mode_effective", "degraded",
        "degradation_reason", "provider_status", "total_matches", "duplicates_removed", "results",
        "warnings",
    },
    "UpdateOperation": {
        "op", "find", "replace", "all_occurrences", "paragraph", "slide", "from_index", "to_index",
        "sheet", "cell", "cell_range", "value", "formula", "rows", "chart_type", "title", "anchor",
        "text", "level", "layout", "bullets", "image_base64", "asset_id", "width_cm", "metadata",
    },
    "CapabilityManifest": {
        "contract_version", "contracts_accepted", "extensions", "service", "build", "generated_at",
        "formats", "search", "vision", "artifacts", "limits", "error_codes",
    },
}


@pytest.mark.parametrize("model_name", sorted(GOLDEN))
def test_wire_fields_are_frozen(model_name):
    model = getattr(contracts, model_name)
    assert set(model.model_fields) == GOLDEN[model_name]


def test_contract_version_and_extension_ids_are_additive():
    assert contracts.CONTRACT_VERSION == "dk/v1"
    assert "provider-federation" in contracts.CONTRACT_EXTENSIONS
    assert "page-citation" in contracts.CONTRACT_EXTENSIONS


def test_error_code_table_is_complete_and_maps_to_http():
    codes = {code.value for code in contracts.KnowledgeErrorCode}
    assert {
        "document_not_found", "revision_conflict", "encrypted_document", "corrupt_content",
        "unsupported_format", "provider_unavailable", "vision_unavailable", "integrity_mismatch",
    } <= codes
    for code in contracts.KnowledgeErrorCode:
        assert code in contracts.KnowledgeError.HTTP_STATUS


def _agtools_contract_path() -> Path | None:
    candidate = os.environ.get("AGTOOLS_CONTRACT_PATH")
    if candidate and Path(candidate).exists():
        return Path(candidate)
    for base in (Path("/workspace/worktrees"), Path("/workspace/repos")):
        path = base / "r21-AGPilot0006" / "app" / "knowledge" / "contracts.py"
        if path.exists():
            return path
        path = base / "AGPilot0006" / "app" / "knowledge" / "contracts.py"
        if path.exists():
            return path
    return None


def test_agtools_copy_matches_when_sources_are_present():
    path = _agtools_contract_path()
    if path is None:
        pytest.skip("AGTools source not present in this sandbox")
    spec = importlib.util.spec_from_file_location("agtools_dk_contract", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["agtools_dk_contract"] = module
    spec.loader.exec_module(module)
    assert module.CONTRACT_VERSION == contracts.CONTRACT_VERSION
    assert [code.value for code in module.KnowledgeErrorCode] == [
        code.value for code in contracts.KnowledgeErrorCode
    ]
    for name in GOLDEN:
        assert set(getattr(module, name).model_fields) == set(
            getattr(contracts, name).model_fields
        ), name
