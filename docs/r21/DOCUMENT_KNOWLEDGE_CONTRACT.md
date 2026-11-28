# dk/v1 — Document Knowledge Contract (R21-PREDEV, frozen)

Status · **frozen for R21 pre-development** · owner AGCore (`app/knowledge/contracts.py`) ·
implementations AGCore (federation) and AGTools (engine) · wire version `dk/v1`

This document is the WP-01 deliverable. Everything AGCore, AGTools, HD2AI and the Gateway
exchange about documents is defined here. The Python models are the normative artifact: this
text explains them and the reasons behind each rule.

---

## 1. Why a new contract instead of extending the R2-W workspace contract

The R2-W workspace contract (`r2-w/v1+a02`) already defines files, revisions, chunks, search and
citations **inside AGCore**. It cannot describe PDF page addresses, spreadsheet cells, slide
shapes, or an engine that lives in another service. Rather than bend it, R21 adds a contract that
*composes* it:

* AGCore keeps owning workspace files, revisions, indexing and the workspace citation format.
* `dk/v1` is the lingua franca between AGCore and format engines (AGTools).
* Nothing in `r2-w/v1+a02` is renamed, removed or reinterpreted. The knowledge layer is additive
  and feature-flagged (`AGCORE_KNOWLEDGE_ENABLED`, `KNOWLEDGE_ENABLED`).

Verified by `tests/test_r21_contract_conformance.py` (golden field sets + cross-repo comparison)
and by the existing R2 workspace suite, which still passes unchanged.

---

## 2. Identity

| Concept | Rule |
| --- | --- |
| `DocumentRef.document_id` | `^[a-z0-9][a-z0-9_.:-]{2,95}$`. AGCore workspace: `wf_…`/`wc_…` file id. AGTools: `kd_…`. Session attachments: `att-…`. |
| `DocumentRef.namespace` | `workspace`, `session_attachment`, `agtools`, `artifact`, `external` — decides which provider serves the document. |
| `DocumentRef.revision` | Monotonic per document, starting at 1. A read/update names the revision it means. |
| `DocumentRef.content_hash` | `sha256:<64 hex>` of the exact bytes. Used for de-duplication and integrity. |
| `canonical_id` | `"{namespace}:{document_id}@{revision}"` — stable citation/cache key. |
| `document_key` | `"{namespace}:{document_id}"` — identity independent of revision. |

`DocumentRef.metadata` is free-form and intentionally **not** used to route. A consumer that
needs a path uses `metadata.path` for display only; access decisions come from the provider.

Content hash is the only cross-service de-duplication key. Two files with identical bytes but
different names are the same content and are de-duplicated in search results, while both remain
addressable as documents.

---

## 3. MIME and format family

`normalize_mime(declared, filename)` resolves a canonical MIME type:

1. a declared type that maps to a real family wins (`application/pdf; charset=binary` → `application/pdf`)
2. aliases are folded (`application/x-pdf` → `application/pdf`, `image/jpg` → `image/jpeg`)
3. a bare archive (`application/zip`) is **not** an Office format; the filename decides
4. otherwise `application/octet-stream`

`format_family()` maps MIME → `pdf | docx | xlsx | pptx | text | csv | image | unknown`.
An unknown family is a contract error (`unsupported_format`), never a silent text read.

---

## 4. Locators and citations

A locator is a typed coordinate, not a string:

| Format | Locator kinds | Coordinates |
| --- | --- | --- |
| PDF | `page`, `document`, `image` | 1-based `page`, optional `image` xref |
| DOCX | `paragraph`, `section`, `document` | 1-based body paragraph index, heading path in `section` |
| XLSX | `sheet`, `cell`, `range` | sheet name + A1 cell/range |
| PPTX | `slide`, `document` | 1-based `slide`, optional `shape` name |
| text/markdown | `paragraph`, `section`, `document` | paragraph index, heading path |

Validation is enforced at construction: a `cell` locator without `sheet` and `cell` is a
validation error, as is any typed locator with no coordinate at all.

A `Citation` pins: `document` (thus revision + content hash), `locator`, a `quote` (≤ 2 000
chars) and a deterministic `citation_id`:

```
citation_id = "cit_" + sha256(canonical_id | locator_json | quote[:512])[:16]
```

Consequences the tests rely on:

* the same passage cited twice yields the same id (so the UI can de-duplicate safely);
* a citation from an older revision keeps that revision and is reported as `superseded`, never
  silently re-pointed at the latest bytes.

---

## 5. Operations

### 5.1 `POST /knowledge/read`

`ReadRequest` carries the mode and the budget. Modes: `outline`, `full`, `pages`, `section`,
`sheet`, `slide`, `excerpt`. A mode that needs coordinates without them is a validation error.

Guarantees:

* `max_chars` (256 … 400 000) caps response text; when hit, `truncated=true` and `next_cursor`
  names where to continue (`page:7`, `sheet:Data:row:501`).
* every node carries its locator, so any excerpt can become a citation without re-reading.
* binary payloads are never embedded in JSON; images are described by `asset_id` metadata.

### 5.2 `POST /knowledge/search`

`SearchRequest`: `query`, `mode` (`keyword|vector|hybrid`), `top_k` (1 … 50), `filters`,
`document_ids`, `allow_degraded`, `rerank`.

Contract rules:

* **Degradation is explicit.** When embeddings are unavailable the answer says
  `degraded=true`, `mode_effective="keyword"`, `degradation_reason="embedding_unavailable"`.
  A degraded search never fails silently and never pretends to be hybrid.
* **De-duplication is counted.** `duplicates_removed` reports how many identical excerpts were
  dropped, so a caller can tell "one document matched" from "five copies matched".
* **Filters are server-side.** `mime_types`, `families`, `document_ids`, `names`,
  `path_prefix`, `updated_after`, `metadata` are applied before ranking.

### 5.3 `POST /knowledge/documents/from-blocks` (create)

`CreateRequest`: `name`, `mime_type`, ordered `blocks` (`heading|paragraph|bullets|table|image|
page_break`), optional `template_document_id`, `metadata`, `overwrite_document_id`.

Creation is deterministic: the same blocks produce a document whose reader returns the same
nodes. Template creation preserves the template's styles/layouts/masters (DOCX/PPTX) or
workbook properties (XLSX).

### 5.4 `POST /knowledge/documents/{id}/update`

`UpdateRequest`: `expected_revision`, `operations[]`, `dry_run`, `output_name`.

Operation vocabulary (all formats share one union; a format skips what it cannot do and says so):

`replace_text`, `insert_paragraph`, `delete_paragraph`, `set_cell`, `set_range`, `add_table`,
`add_chart`, `add_sheet`, `add_slide`, `delete_slide`, `move_slide`, `insert_image`,
`replace_image`, `set_metadata`.

Rules:

* `expected_revision` mismatch → `revision_conflict` (HTTP 409, `retryable=true`).
* `dry_run=true` reports `applied`/`skipped`/`diff_summary` and writes nothing.
* an unsupported operation appears in `skipped` with `"<op>:unsupported_for_<format>"`; it never
  aborts the other operations.
* editing an existing file touches only the named XML/parts; headers, footers, numbering,
  section properties, layouts, masters and theme survive (tested).

### 5.5 `POST /knowledge/documents/{id}/validate`

`ValidateResponse.checks` is a list of named integrity checks (`content_hash`, `size`,
`office_archive`, `parse_outline`, …). `ok=false` always comes with at least one
`KnowledgeErrorBody` of code `integrity_mismatch` or the specific failure.

### 5.6 Read caller-supplied bytes: `POST /knowledge/read-inline`

ACCore workspace bytes stay in AGCore's blob store. To read a PDF/DOCX/XLSX/PPTX workspace file
through the engine, AGCore sends `{name, mime_type, content_base64, request}` and gets a normal
`ReadResponse` whose document identity AGCore re-points at the workspace file (`_rebase`). No
second copy is stored and no AGTools document is created.

---

## 6. Capability manifest

`GET /knowledge/manifest` returns `CapabilityManifest`:

```json
{
  "contract_version": "dk/v1",
  "contracts_accepted": ["dk/v1"],
  "extensions": ["page-citation", "slide-citation", "sheet-cell-citation", "...", "provider-federation"],
  "service": "agtools",
  "build": "2.0.0",
  "formats": [{"mime_type": "...", "family": "pdf", "read": {...}, "write": {...}, "citation_kinds": ["page"], "available": true}],
  "search": {"modes": ["keyword"], "metadata_filters": [...], "incremental_index": true, "ready_reuse": true, "keyword_fallback": true},
  "vision": {"available": false, "engine": "none", "model": null, "capability_source": "none", "reason": "no_capability_file"},
  "artifacts": {"atomic_write": true, "revisions": true, "rollback": true, "optimistic_concurrency": true, "integrity_check": true},
  "limits": {"max_file_bytes": 104857600, "max_pages": 500, "max_chars_read": 400000, "max_search_top_k": 50},
  "error_codes": ["invalid_request", "..."]
}
```

Rules:

* a consumer must **read the manifest, not assume**: `write.create=false` for PDF is a fact, not
  a bug; `vision.model=null` means no model was bound (never a default model name).
* `available=false` on a format is accompanied by `unavailable_reason` (e.g.
  `openpyxl_not_installed`), so ops can see why without reading logs.
* AGCore's `/api/v1/knowledge/capabilities` returns the same fields plus `providers[]`
  (`workspace`, `session_attachment`, `agtools`) with each provider's own capability table and
  `degraded_reason` when it is currently unreachable.

---

## 7. Errors

One table, both services:

| code | HTTP | retryable | meaning |
| --- | --- | --- | --- |
| `invalid_request` | 400 | no | malformed/contradictory request |
| `contract_mismatch` | 400 | no | peer payload is not `dk/v1` |
| `permission_denied` | 403 | no | caller may not see this document |
| `document_not_found` | 404 | no | no such document in this namespace |
| `revision_not_found` | 404 | no | revision does not exist |
| `revision_conflict` | 409 | **yes** | another writer advanced the revision |
| `index_not_ready` | 409 | yes | index still building |
| `document_locked` | 423 | yes | write lock held |
| `size_limit_exceeded` | 413 | no | upload/file ceiling |
| `unsupported_format` | 415 | no | no engine for this MIME family |
| `write_not_supported` / `read_not_supported` | 405 | no | capability absent for this format |
| `encrypted_document` | 422 | no | password required; never brute-forced |
| `corrupt_content` | 422 | no | package/file damaged |
| `page_limit_exceeded` | 422 | no | page ceiling |
| `vision_unavailable` | 422 | no | no vision capability bound |
| `integrity_mismatch` | 422 | no | stored bytes ≠ recorded hash |
| `rate_limited` | 429 | yes | provider/engine throttle |
| `provider_unavailable` | 503 | yes | engine unreachable |
| `degraded_search` | 200 | — | informational; result is keyword-only |
| `internal_error` | 500 | maybe | unexpected |

Wire shape: `{"error": {"code", "message", "detail": {}, "retryable": bool}}`.
AGCore maps workspace failures onto this table (e.g. `content_conflict` →
`revision_conflict`), so a client only learns one vocabulary.

---

## 8. Versioning and backward compatibility

* `contract_version` is on every response; a consumer rejects an unknown major (`dk/v2`) with
  `contract_mismatch`.
* New optional fields are allowed within `dk/v1`; new *values* are appended to `extensions`.
  Removing or renaming a field requires `dk/v2`.
* The golden test (`tests/test_r21_contract_conformance.py`) pins every field set. A rename fails
  CI, which is the point.
* Legacy AGTools endpoints (`/api/v1/documents`, `/tools/docx-to-md`, FTS search) are unchanged
  and still used by the R2 durable-analysis flow.

---

## 9. Provider federation (AGCore extension `provider-federation`)

`FederatedSearchRequest` adds `workspace_id`, `conversation_id`, `sources`, `file_ids` to the
common search request. Merge rules (implemented in `app/knowledge/service.py`):

1. per provider, normalise scores by that provider's maximum;
2. fuse with `0.65 × normalised + 0.35 × 1/rank` (a light RRF, so a single strong provider does
   not drown the other);
3. de-duplicate by SHA-256 of the excerpt, counting drops in `duplicates_removed`;
4. sort, cut at `top_k`.

If a provider fails, its results are dropped and a warning `"<provider>:<error_code>"` is added;
the response is `degraded=true`. Search never fails because one engine is down — and it never
claims complete coverage when one is.

Context assembly (`KnowledgeContextRequest`) then applies the token budget
(64 … 131 072), neutralises document-controlled delimiters, counts prompt-injection signatures
and returns citations for exactly the blocks that fit.

---

## 10. Conformance evidence

| Check | Where |
| --- | --- |
| Golden field sets, error table, extension ids | `AGPilot0005/tests/test_r21_contract_conformance.py` |
| Cross-repo model comparison (same sandbox) | same file, `test_agtools_copy_matches_when_sources_are_present` |
| AGTools wire behaviour (read/search/create/update/validate) | `AGPilot0006/tests/test_knowledge_*.py` |
| Federated behaviour and degradation | `AGPilot0005/tests/test_r21_knowledge_federation.py` |
| Consumer typing / citation display rules | `AGPilot0011/hd2ai-admin/tests/knowledge-client.test.mjs` |
