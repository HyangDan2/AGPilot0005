# AGCore R2-W — Workspace & RAG Foundation Preview · Developer Handover

Status: **Preview complete (CP-W00 … CP-W06)** · 2026-11-27
Branch: `feat/r2-workspace-rag` in every repository · **no `main` merge, no deployment**

## 1. Repository / commit matrix

| Repository | Baseline `origin/main` | Branch head | Commits |
| --- | --- | --- | --- |
| `HyangDan2/AGPilot0005` (AGCore) | `0d2047f09a67f4cfd44f8c5399523eab3eac2172` = **v1.2.0** | `feat/r2-workspace-rag` | `90907ba` CP-W00 · `6b1562c` CP-W01 · `59f1573` CP-W02 · `dfa72e4` CP-W03 · `1c9d4f1` CP-W04 |
| `HyangDan2/AGPilot0006` (AGTools) | `6d0f57f36c516545d7c5a307f26172c1b958ab2b` | `feat/r2-workspace-rag` (created, **no commits — parser reused unchanged**) | – |
| `HyangDan2/AGPilot0011` (HD2AI) | `b4913d0d39f8ca223f48bb7b5b7fbd99f6126ebb` | `feat/r2-workspace-rag` | `79e718e` contract types · `0d623d1` CP-W05 UI |

All three branches are pushed. No operating server, database, or R1.3 runtime file was modified.

## 2. What was built

### 2.1 Backend (AGPilot0005, `app/workspace/`)

| Module | Responsibility |
| --- | --- |
| `contracts.py` | Contract r2-w/v1 types (identity, provider protocol, index/retrieval/context/citation schemas, error codes) |
| `api.py` | REST surface `/api/v1/workspaces` (contract §3) |
| `preview_app.py` | Independent preview ASGI app (no R1.3 runtime import) |
| `runtime.py` | Service container (store, storage, manager, indexer, retriever, assembler, tools) |
| `store.py` | SQLite metadata, revisions, events, index jobs, chunks, FTS5, vectors, citations |
| `storage.py` | id-derived blob paths, streaming atomic writes, O_NOFOLLOW reads, quota, purge, sweep |
| `manager.py` | Workspace/file/folder lifecycle, access policy, quotas, revision conflict, events, recovery |
| `ingest.py` | AGTools parser reuse over HTTP + local text extraction (no duplicated PDF/DOCX/PPTX parser) |
| `chunking.py` | Token-bounded, paragraph/section-aware chunking (only when the parser returns no chunks) |
| `indexer.py` | Index lifecycle, queue cap + reconciliation, revision guards, retry, cancel, worker thread |
| `embedding.py` | Provider interface: local signed-hash, remote OpenAI/Ollama, gateway (fail-closed) |
| `retrieval.py` | FTS5 keyword (Korean bigrams), cosine vector, RRF hybrid, filters, degradation reporting |
| `context.py` | Token-budgeted context package, de-duplication, prompt-injection hardening |
| `citations.py` | Persisted citation registry, revision status, validation |
| `tools.py` | AGCore extension adapter (manifest + 4 tools, ExecutionScope/Capability/ContextBudget) |
| `mock.py` | In-memory provider + `_mock` router for the Canvas engineer |
| `textutil.py` | Token estimation and Korean tokenisation shared by chunking/search |
| `config.py` | `AGCORE_WORKSPACE_*` settings (isolated from the R1.3 settings module) |

### 2.2 Frontend (AGPilot0011)

- `hd2ai-shared/workspaceClient.ts` — transport-only client for the whole contract (incl. XHR upload progress).
- `hd2ai-admin/src/workspace/` — `WorkspacePanel`, `FileExplorer`, `SearchPanel`, `IndexPanel`, `RagChat`, `workspaceHost`, `workspace.css`.
- `App.tsx` change: **one** full-width sidebar button (`🗂️ 워크스페이스`) and the panel mount. The existing chat, sessions, library modal and settings are untouched.

### 2.3 Shared contract

`docs/r2/WORKSPACE_CONTRACT.md` (+ `docs/r2/BASELINE.md`) in AGPilot0005, `clients/workspace-contract.ts` and the identical `AGPilot0011/hd2ai-shared/workspace-contract.ts`. Canvas can start immediately with the mock provider.

## 3. API schema summary

Full detail in `docs/r2/WORKSPACE_CONTRACT.md` §3. Highlights:

- `POST/GET/PATCH/DELETE /api/v1/workspaces…`, `/access`, `/usage`
- `GET/POST /files`, `/folders`, `PATCH/DELETE /files/{id}`, `GET /files/{id}/content` (`?preview=text`, `?revision=n`), `/events`
- `GET /index/status`, `POST /index/reindex`, `GET /index/jobs`, `POST /files/{id}/index/cancel`
- `GET /search`, `POST /context`, `GET /citations/{id}`, `POST /citations/validate`
- `POST/GET/DELETE /artifact-references`, `POST/GET /conversation-references`
- Errors: `{"detail": {"code", "message", "retryable", "context"}}` with the code table in §3.7.

## 4. Storage & index schema (SQLite)

Database: `AGCORE_WORKSPACE_DB_PATH` (default `/var/lib/hd2lab-agcore/workspace.db`).
Bodies: `AGCORE_WORKSPACE_STORAGE_ROOT/{tenant}/{workspace_id}/{file_id}/r{revision}.blob`.

| Table | Key columns |
| --- | --- |
| `workspace` | workspace_id, tenant_id, owner_id, service_id, name, description, status, revision, timestamps |
| `workspace_file` | file_id, workspace_id, parent_id, kind(file/folder), name, mime_type, size_bytes, content_hash, revision, index_status, index_error, deleted_at |
| `workspace_file_revision` | (file_id, revision), content_hash, size_bytes, blob_key, parser_version |
| `workspace_index_job` | job_id, file/revision, state, attempts, max_attempts, error_code, force, timestamps |
| `workspace_chunk` | chunk_id, citation_id, workspace_id, file_id, file_revision, chunk_index, content, content_hash, token_count, page_number, section, offsets, parser/chunking version |
| `workspace_chunk_fts` | FTS5 (unicode61) over bigram-expanded `body` |
| `workspace_chunk_vector` | (chunk_id, index_key=`model:dim`), dim, float32 blob |
| `workspace_citation` | citation_id, workspace_id, file/revision, chunk_id, file_name, page, section, excerpt |
| `workspace_event` | event_id, workspace_id, file_id, type, revision, at |
| `workspace_artifact_ref` / `workspace_conversation_ref` | Canvas artifact id + metadata / chat binding |

Schema version stored in `workspace_meta` (`SCHEMA_VERSION = 1`).

## 5. New environment variables

All backend variables are optional; defaults preserve today's behaviour (the module is not mounted in R1.3).

| Group | Variables |
| --- | --- |
| Core | `AGCORE_WORKSPACE_ENABLED`, `AGCORE_WORKSPACE_DB_PATH`, `AGCORE_WORKSPACE_STORAGE_ROOT`, `AGCORE_WORKSPACE_SQLITE_BUSY_TIMEOUT_MS` |
| Limits | `AGCORE_WORKSPACE_MAX_FILE_BYTES` (50 MiB), `…_MAX_WORKSPACE_BYTES` (512 MiB), `…_MAX_FILES_PER_WORKSPACE`, `…_MAX_WORKSPACES_PER_OWNER`, `…_MAX_FOLDERS_PER_WORKSPACE`, `…_UPLOAD_CHUNK_BYTES`, `…_BLOCKED_EXTENSIONS`, `…_REVISION_KEEP` (3) |
| Isolation | `AGCORE_WORKSPACE_SERVICE_ISOLATION` (true: same tenant+owner+service required) |
| Chunking | `AGCORE_WORKSPACE_CHUNK_TARGET_TOKENS` (768), `…_CHUNK_MAX_TOKENS` (1024), `…_CHUNK_OVERLAP_TOKENS` (96) |
| Index queue | `AGCORE_WORKSPACE_INDEX_MAX_PENDING` (200), `…_INDEX_MAX_CONCURRENCY` (2), `…_INDEX_MAX_ATTEMPTS` (3), `…_INDEX_MAX_CHUNKS_PER_FILE` (5000) |
| AGTools reuse | `AGCORE_WORKSPACE_AGTOOLS_ENABLED` (true), `AGCORE_WORKSPACE_AGTOOLS_URL` (default `AGCORE_AGTOOLS_URL`), `AGCORE_WORKSPACE_AGTOOLS_TOKEN`, `…_AGTOOLS_TIMEOUT_SECONDS`, `…_PARSER_POLL_INTERVAL_SECONDS`, `…_PARSER_MAX_WAIT_SECONDS` (240), `…_PARSER_CHUNK_FETCH_CONCURRENCY` (8) |
| Embedding | `AGCORE_WORKSPACE_EMBEDDING_PROVIDER` (`auto\|local\|remote\|gateway`), `…_EMBEDDING_MODEL`, `…_EMBEDDING_DIMENSION` (512), `…_EMBEDDING_BATCH_SIZE`, `…_EMBEDDING_MAX_INPUT_TOKENS`, `…_EMBEDDING_TIMEOUT_SECONDS`, `…_EMBEDDING_URL`, `…_EMBEDDING_API_KEY`, `…_EMBEDDING_REMOTE_MODEL`, `…_EMBEDDING_AUTO_INDEX` |
| Retrieval | `AGCORE_WORKSPACE_SEARCH_DEFAULT_TOP_K` (8), `…_SEARCH_MAX_TOP_K` (50), `…_SEARCH_RRF_K` (60), `…_SEARCH_MIN_SCORE`, `…_CONTEXT_BUDGET_TOKENS` (default `AGCORE_CONTEXT_BUDGET_TOKENS`), `…_CONTEXT_MAX_EXCERPT_TOKENS` |
| Preview app | `AGCORE_WORKSPACE_CORS_ORIGINS` |
| Frontend | `VITE_WORKSPACE_API_BASE` (e.g. `http://127.0.0.1:8210/api/v1/workspaces`), `VITE_WORKSPACE_RAG_MODE` (`mock` default / `gateway`) |

## 6. Embedding provider configuration

- `local` (default): offline signed feature hashing, 512 dims, no GPU/network/cost. Deterministic; best for preview and Raspberry Pi.
- `remote`: set `AGCORE_WORKSPACE_EMBEDDING_URL` (OpenAI-compatible `…/v1/embeddings` or Ollama `…/api/embed`) plus optional `…_EMBEDDING_API_KEY` / `…_EMBEDDING_REMOTE_MODEL`. Dimension is taken from the first response and the vector index key becomes `model:dim`.
- `gateway`: **not available** — the AGCore AI Gateway client exposes chat routes only (`GATEWAY_EMBEDDING_ENDPOINT_VERIFIED = False`); the adapter fails closed rather than pretending.
- Model/dimension changes never mix vectors: index keys are `model:dim`, and a missing key degrades hybrid search to keyword-only with an explicit `degraded` flag.

## 7. Independent preview

```bash
# backend (no R1.3 runtime, no gateway)
cd /workspace/repos/AGPilot0005
AGCORE_WORKSPACE_DB_PATH=/tmp/r2w/workspace.db \
AGCORE_WORKSPACE_STORAGE_ROOT=/tmp/r2w/storage \
AGCORE_WORKSPACE_EMBEDDING_PROVIDER=local \
python -m uvicorn app.workspace.preview_app:app --host 127.0.0.1 --port 8210
# or: scripts/run_workspace_preview.sh
```

```bash
# frontend
cd /workspace/repos/AGPilot0011/hd2ai-admin
npm ci
VITE_WORKSPACE_API_BASE=http://127.0.0.1:8210/api/v1/workspaces npm run dev
# open http://127.0.0.1:5173/ -> sidebar "🗂️ 워크스페이스"
```

```bash
# tests
cd /workspace/repos/AGPilot0005
python -m pytest tests/test_r2_workspace_*.py -q     # 52 passed
python -m pytest -q                                  # 226 passed (full AGCore suite)
```

Canvas mock provider (no storage needed): `GET/POST /api/v1/workspaces/_mock/...` from the preview app, or `MockWorkspaceProvider` / `createMockWorkspaceProvider()` in tests.

## 8. Verified functionality (CP-W06)

| WO-W20 item | Result | Evidence |
| --- | --- | --- |
| Workspace CRUD | PASS | `test_r2_workspace_storage.py`, `test_r2_workspace_api.py` |
| File CRUD (folder/rename/move/delete/revisions) | PASS | same + browser QA |
| Tenant/Owner/Service isolation | PASS | `test_scope_isolation_tenant_owner_service`, `test_http_isolation_and_error_contract` |
| Path traversal & symlink refusal | PASS | `test_path_traversal_and_unsafe_names_rejected`, `test_symlink_components_are_refused` |
| Storage quota / file size | PASS | `test_file_size_and_workspace_quota` |
| Document parsing & indexing (real AGTools) | PASS | DOCX+PPTX upload → 4 chunks READY, section titles/slide numbers from parser (`QA_REPORT.md` §3) |
| Keyword retrieval (Korean) | PASS | `test_korean_keyword_search_matches_with_particles`, preview smoke |
| Vector retrieval with a real (local) embedding | PASS | `test_vector_search_ranks_related_document_first` |
| Hybrid retrieval (RRF, degradation) | PASS | `test_hybrid_search_fuses_keyword_and_vector`, `test_hybrid_degrades_to_keyword_when_embedding_unavailable` |
| Index lifecycle on modify/delete | PASS | `test_stale_revision_job_cannot_overwrite_newer_index`, `test_deleted_file_during_indexing_never_reappears` |
| Revision change during indexing | PASS | same (atomic `replace_chunks_if_current`) |
| Context budget | PASS | `test_search_context_and_citation_flow` (used ≤ budget), tool adapter budget cap |
| Citation → original | PASS | citation resolve/validate + browser preview of `revision 1 · current` |
| Workspace UI & chat preview | PASS | headless Chromium flow (workspace → upload → READY → search → RAG chat → citation → index → mobile) |

Screenshots: `/workspace/downloads/r2-w-qa/` (`01-home` … `09-mobile`).

## 9. Not implemented / not verified

- **Real LLM answer text**: the RAG chat uses a clearly-labelled mock adapter by default. `VITE_WORKSPACE_RAG_MODE=gateway` attempts the existing chat API and falls back to mock on failure; not validated against a live gateway.
- **Remote embedding provider**: implemented and unit-tested with mocked HTTP only; no paid API was called (PO approval required).
- **AGTools PDF path**: protocol covered by a mocked HTTP test; the real-parser run used DOCX/PPTX (PDF page metadata relies on the same code path).
- **AGTools chunk list has no content** → one detail request per chunk (bounded concurrency 8, cap 5000). A future `include_content` param in AGTools would remove the N+1 (see Backlog).
- Page numbers for DOCX are `null` (the parser has no page concept) — the contract forbids inventing them.
- Out of scope by work order: multi-user sharing/co-editing, archive auto-extraction, executable execution, cross-encoder reranking, external vector DB, Coding Agent/Terminal, SigmaWorks statistics, AGPolicy RL.

## 10. Canvas connection interface

- Read `docs/r2/WORKSPACE_CONTRACT.md`; use `WorkspaceProvider` (`app/workspace/contracts.py`) or `WorkspaceProvider`/`createMockWorkspaceProvider()` (`clients/workspace-contract.ts`).
- Artifact references only: `POST /workspaces/{id}/artifact-references` with `{artifact_id, kind, file_id?, metadata?}`; Workspace never reads artifact content.
- Workspace does not touch `Artifact Content/Revision/Patch/Canvas Editor/Export`; no Canvas file was modified.

## 11. R1.3 runtime integration targets (do **not** apply before R1.3 closes)

1. `app/main.py`: `from app.workspace.api import router as workspace_router` + `app.include_router(workspace_router)`; start `WorkspaceRuntime` (with `start_worker=True`) in the lifespan and `shutdown()` on exit. Only these lines.
2. Auth: endpoints already call `resolve_caller_scope` / fail closed on unauthenticated. No change to `app/core/auth.py`.
3. Tools: register `WorkspaceToolAdapter` with the runtime's tool registry using `TOOL_MANIFEST`; pass `ExecutionScope`, capability profile and `context_budget`. `app/tools/dispatcher.py` needs no change.
4. Context: the runtime should call `ContextAssembler.build` (or the `workspace_get_context` tool) instead of pasting files; `AGCORE_CONTEXT_BUDGET_TOKENS` remains the ceiling.
5. Frontend: the panel is already mounted in `hd2ai-admin`; production base stays same-origin (`/api/v1/workspaces`), so no build change is required. Remove `VITE_WORKSPACE_API_BASE` for deployment.

## 12. Backlog

| ID | Item | Priority |
| --- | --- | --- |
| R2W-B1 | AGTools `GET /documents/{id}/chunks?include_content=true` to remove N+1 chunk detail fetches | P1 |
| R2W-B2 | Background re-embedding when the embedding model/dimension changes (index rebuild job) | P1 |
| R2W-B3 | PDF page mapping for split chunks inside AGTools (currently `page` per chunk when provided) | P2 |
| R2W-B4 | Workspace-level ACL/multi-user sharing (out of scope for R2-W) | P2 |
| R2W-B5 | Vector index scaling (brute force is fine to ~100k chunks; evaluate sqlite-vec/FAISS beyond) | P2 |
| R2W-B6 | Streaming context/citation SSE for long analyses | P3 |
| R2W-B7 | File move via drag & drop in the UI (API already supports `parent_id`) | P3 |
| R2W-B8 | Agent-facing answer verification loop (citation id check already exposed via `/citations/validate`) | P3 |
