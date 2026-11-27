# AGCore R2-W — Workspace Shared Contract

Status: **FROZEN PREVIEW CONTRACT (R2-W, v1)**
Owner: Workspace/RAG Engineer (`feat/r2-workspace-rag`)
Consumers: Canvas Engineer (`feat/r2-canvas`), HD2AI Frontend (`AGPilot0011`), Server-HQ Supervisor
Baseline: AGCore **v1.2.0 (`0d2047f`)**, AGTools **`6d0f57f`**, HD2AI **`b4913d0`**

This document is the contract between the Workspace/RAG engine (AGPilot0005), the Canvas
Artifact engine (parallel `feat/r2-canvas` branch) and the HD2AI frontend (AGPilot0011).
Canvas develops independently against the **Mock Provider** described in §7 without waiting
for the storage engine.

Canonical machine-readable types:

| Language | File | Purpose |
| --- | --- | --- |
| Python | `app/workspace/contracts.py` | Pydantic schemas + `WorkspaceProvider` protocol |
| TypeScript | `clients/workspace-contract.ts` | Frontend/Canvas types (copy: AGPilot0011 `hd2ai-shared/workspace-contract.ts`) |

Nothing in this contract changes R1.3 runtime, `AGPolicy`, `ActionValidator`, or the existing
authentication contract. Workspace endpoints are additive and are **not** mounted on the R1.3
runtime router; they are exposed by an independent preview entry (`app.workspace.preview_app`).

---

## 1. Workspace Identity

Tenant, owner and service are always derived by the server from the verified `ExecutionScope`
(`app/core/auth.py`). A client-supplied `owner_id` / `tenant_id` is **never** used as the basis
for authorization.

```json
{
  "workspace_id": "ws_7f3c1a9e2b04",
  "tenant_id": "default_tenant",
  "owner_id": "user_1042",
  "service_id": "hd2ai-admin",
  "name": "R2 설계 문서",
  "description": "Workspace RAG 검증용",
  "status": "active",
  "revision": 3,
  "created_at": "2026-11-27T01:10:12.482Z",
  "updated_at": "2026-11-27T02:44:05.019Z"
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `workspace_id` | string | `ws_` + 12 hex chars, server generated |
| `tenant_id` / `owner_id` / `service_id` | string | server decision, immutable |
| `name` | string(1..120) | trimmed, control characters rejected |
| `description` | string(0..2000) | optional |
| `status` | `active` \| `archived` \| `deleted` | `deleted` is terminal for preview |
| `revision` | integer | monotonically increasing, starts at 1 |
| `created_at` / `updated_at` | ISO-8601 UTC (`Z`) | server clock |

Workspace access level (returned by `GET /workspaces/{id}/access`):
`read` \| `write` \| `admin` \| `none`. In the preview all personal workspaces grant
`admin` to their owner and `none` to everyone else; multi-user sharing is out of scope.

### File identity

```json
{
  "file_id": "wf_5c81d0a7b3e9",
  "workspace_id": "ws_7f3c1a9e2b04",
  "parent_id": null,
  "kind": "file",
  "name": "spec.md",
  "path": "/spec.md",
  "mime_type": "text/markdown",
  "size_bytes": 8123,
  "content_hash": "sha256:6f1c…",
  "revision": 2,
  "index_status": "READY",
  "created_at": "2026-11-27T01:11:00.001Z",
  "updated_at": "2026-11-27T01:40:22.771Z"
}
```

`path` is a **display path** built from names, not an OS path. Storage never uses user-supplied
names for file system paths (§6). `kind` is `file` or `folder`; folders carry `size_bytes: 0`
and `index_status: null`.

---

## 2. Shared Provider Interface (logical)

The Canvas engine and the frontend only need this interface. Names are logical; the Python
signature is `app/workspace/contracts.py::WorkspaceProvider` and the TypeScript mirror is
`WorkspaceProvider` in the client contract file.

| Operation | Signature (logical) | Result |
| --- | --- | --- |
| `get_workspace` | `(workspace_id, scope)` | `WorkspaceIdentity` |
| `check_workspace_access` | `(workspace_id, scope)` | `AccessResult{level, reason}` |
| `list_workspace_files` | `(workspace_id, scope, parent_id?, recursive?)` | `FileIdentity[]` |
| `get_file_metadata` | `(workspace_id, file_id, scope)` | `FileIdentity` |
| `read_file` | `(workspace_id, file_id, scope, revision?)` | `FileContent{metadata, bytes, text?}` |
| `create_file` | `(workspace_id, scope, name, content, parent_id?, expected_revision?)` | `FileIdentity` |
| `register_artifact_reference` | `(workspace_id, scope, artifact_ref)` | `ArtifactReference` |
| `list_artifact_references` | `(workspace_id, scope, kind?)` | `ArtifactReference[]` |

Semantics:

- Every operation takes the verified scope; implementations must fail closed with
  `workspace_not_found` (not `403`) when the workspace belongs to another tenant/owner/service.
- `read_file` returns the **current** revision unless `revision` is given; an obsolete revision
  is returned with `revision_status: "superseded"`.
- `create_file` with an existing name in the same folder appends a new revision of that file
  when `overwrite=true`, otherwise `409 file_exists`.

---

## 3. Shared API Contract

Base: `/api/v1/workspaces` (preview entry only). Content type `application/json` except uploads
(`multipart/form-data`) and downloads (`application/octet-stream`).

### 3.1 Workspace CRUD

| Method | Path | Body | Success |
| --- | --- | --- | --- |
| `GET` | `/workspaces` | – | `200 {items:[WorkspaceIdentity], total}` |
| `POST` | `/workspaces` | `{name, description?}` | `201 WorkspaceIdentity` |
| `GET` | `/workspaces/{workspace_id}` | – | `200 WorkspaceIdentity` |
| `PATCH` | `/workspaces/{workspace_id}` | `{name?, description?, expected_revision?}` | `200 WorkspaceIdentity` |
| `DELETE` | `/workspaces/{workspace_id}` | – | `200 {workspace_id, status:"deleted", purged_files:int}` |
| `GET` | `/workspaces/{workspace_id}/access` | – | `200 {level, reason}` |

### 3.2 Workspace File CRUD

| Method | Path | Body | Success |
| --- | --- | --- | --- |
| `GET` | `/workspaces/{id}/files` | query `parent_id`, `recursive`, `include_deleted` | `200 {items:[FileIdentity], total}` |
| `POST` | `/workspaces/{id}/files` | multipart: `file`, `parent_id?`, `overwrite?`, `expected_revision?` | `201 FileIdentity` |
| `POST` | `/workspaces/{id}/folders` | `{name, parent_id?}` | `201 FileIdentity` |
| `GET` | `/workspaces/{id}/files/{file_id}` | – | `200 FileIdentity` |
| `GET` | `/workspaces/{id}/files/{file_id}/content` | query `preview` (`text` \| `raw`), `revision?` | `200` bytes / text |
| `PATCH` | `/workspaces/{id}/files/{file_id}` | `{name?, parent_id?, expected_revision?}` | `200 FileIdentity` |
| `DELETE` | `/workspaces/{id}/files/{file_id}` | – | `200 {file_id, deleted:true}` |
| `GET` | `/workspaces/{id}/events` | query `since`, `limit` | `200 {items:[FileEvent], next_since}` |

`FileEvent`: `{event_id, type, workspace_id, file_id, revision, at}` with
`type` ∈ `file.created` \| `file.updated` \| `file.renamed` \| `file.moved` \| `file.deleted` \|
`index.status_changed`.

### 3.3 Index lifecycle

| Method | Path | Body | Success |
| --- | --- | --- | --- |
| `GET` | `/workspaces/{id}/index/status` | – | `200 IndexSummary` |
| `POST` | `/workspaces/{id}/index/reindex` | `{file_id?}` | `202 {jobs:[IndexJob]}` |
| `GET` | `/workspaces/{id}/index/jobs` | query `state`, `file_id` | `200 {items:[IndexJob]}` |
| `POST` | `/workspaces/{id}/files/{file_id}/index/cancel` | – | `200 IndexJob` |

`IndexJob`: `{job_id, workspace_id, file_id, file_revision, state, attempts, max_attempts,
error_code, error_detail, created_at, started_at, finished_at}`.

`state` ∈ `PENDING` \| `INDEXING` \| `READY` \| `FAILED` \| `STALE` \| `CANCELLED`.

`IndexSummary`: `{workspace_id, files_total, ready, indexing, pending, failed, stale, cancelled,
chunks_total, embedder:{provider, model_id, dimension, available}, retrieval_modes:[...]}`.

### 3.4 Retrieval

`GET /workspaces/{id}/search?q=…&mode=hybrid|keyword|vector&top_k=8&file_id=…&mime_type=…&min_score=…`

```json
{
  "workspace_id": "ws_…",
  "query": "문서 파싱 정책",
  "mode_requested": "hybrid",
  "mode_effective": "hybrid",
  "degraded": false,
  "degradation_reason": null,
  "provider_status": {"keyword": "ok", "embedding": "ok"},
  "total_matches": 3,
  "results": [{
    "chunk_id": "wc_…",
    "file_id": "wf_…",
    "file_revision": 2,
    "file_name": "spec.md",
    "page_number": 4,
    "section": "3. 파싱",
    "excerpt": "…",
    "score": 0.031,
    "keyword_score": 0.82,
    "vector_score": 0.71,
    "matched_by": ["keyword", "vector"]
  }]
}
```

`mode_effective` may differ from `mode_requested` only by **degradation**: when the embedding
provider is unavailable and `mode=hybrid|vector`, the response is keyword-only with
`degraded: true`, `mode_effective: "keyword"`, `degradation_reason: "embedding_unavailable"`.
A degraded response is **never** reported as a successful hybrid search.

### 3.5 Context assembly & citations

`POST /workspaces/{id}/context`

```json
{"query": "…", "top_k": 8, "token_budget": 4096, "file_ids": ["wf_…"], "search_mode": "hybrid"}
```

Response:

```json
{
  "workspace_id": "ws_…",
  "query": "…",
  "status": "ok",
  "context_text": "[WS-DOC-1] …",
  "used_tokens": 3120,
  "budget_tokens": 4096,
  "truncated": false,
  "sources": [{
    "citation_id": "cit_2b7c…",
    "workspace_id": "ws_…",
    "file_id": "wf_…",
    "file_revision": 2,
    "file_name": "spec.md",
    "chunk_id": "wc_…",
    "page_number": 4,
    "section": "3. 파싱",
    "source_excerpt": "…",
    "revision_status": "current"
  }],
  "retrieved": [/* search results, including ones not used */],
  "degraded": false
}
```

`status` ∈ `ok` \| `insufficient_documents` \| `empty_workspace`.

Citation rules: `page_number` and `section` are `null` when the parser did not provide them —
never invented. `revision_status` ∈ `current` \| `superseded` \| `deleted`.
`GET /workspaces/{id}/citations/{citation_id}` re-resolves a source with an access check and
returns `available: false` + `revision_status` when the file was deleted or superseded.

`POST /workspaces/{id}/citations/validate` `{citation_ids:[…]}` →
`{valid:[…], unknown:[…]}` — used to reject LLM-invented citation ids.

### 3.6 Artifact references (Canvas boundary)

| Method | Path | Body | Success |
| --- | --- | --- | --- |
| `POST` | `/workspaces/{id}/artifact-references` | `{artifact_id, kind, file_id?, metadata?}` | `201 ArtifactReference` |
| `GET` | `/workspaces/{id}/artifact-references` | query `kind` | `200 {items:[…]}` |
| `DELETE` | `/workspaces/{id}/artifact-references/{artifact_ref_id}` | – | `200 {deleted:true}` |

`ArtifactReference`: `{artifact_ref_id, workspace_id, artifact_id, kind, file_id, metadata,
created_at}`. The Workspace engine stores **only the reference**. Artifact content, revisions,
patches and export stay in the Canvas engine's ownership; Workspace never reads or writes them.

Conversation references: `POST/GET /workspaces/{id}/conversation-references`
(`{conversation_id}`), used to bind a chat to one workspace.

### 3.7 Common error response

All workspace endpoints return structured errors:

```json
{"detail": {"code": "revision_conflict", "message": "stale revision", "retryable": false,
            "context": {"expected_revision": 2, "current_revision": 3}}}
```

| code | HTTP | Meaning |
| --- | --- | --- |
| `workspace_not_found` | 404 | unknown/deleted workspace, or not visible to this scope |
| `file_not_found` | 404 | unknown/deleted file |
| `file_exists` | 409 | name collision without `overwrite` |
| `revision_conflict` | 409 | `expected_revision` does not match current |
| `quota_exceeded` | 413 | workspace byte quota / file count limit |
| `file_too_large` | 413 | single file limit |
| `unsupported_media_type` | 415 | upload type not allowed |
| `invalid_path` | 400 | traversal, absolute path, control chars, symlink component |
| `invalid_request` | 400 | schema validation failure |
| `index_not_ready` | 409 | search/context before any chunk is indexed |
| `embedding_unavailable` | 503 | vector/hybrid requested, provider down (only when `allow_degraded=false`) |
| `forbidden` | 403 | authenticated but scope is not the owner |

Empty search is `200` with `total_matches: 0`.

---

## 4. Revision conflict handling

- `expected_revision` is optional. When present and `!= current revision`, respond `409
  revision_conflict` and change nothing.
- File content changes always create a new `revision`; the previous revision stays readable by
  `GET …/content?revision=n` until the file is deleted.
- A workspace rename also bumps the workspace `revision` (used by UI to avoid lost updates).
- Upload with `overwrite=true` on an existing name and a stale `expected_revision` is rejected
  before any byte is written.

## 5. Index lifecycle contract

- Upload → job `PENDING` → `INDEXING` → `READY` / `FAILED`.
- File content change → new revision; the old job is `CANCELLED` and a new `PENDING` job is
  created. A late completion of an older revision can never overwrite the newest index.
- Delete → chunks, vectors and keyword index entries are removed; in-flight jobs are cancelled
  and their late results discarded.
- `STALE` marks a revision whose indexed content no longer matches the current revision (e.g.
  interrupted reindex after restart).
- Reindex is idempotent: unchanged `content_hash` + same parser/chunking version → no duplicate
  work.

## 6. Storage security invariants

- All file bodies live under a configurable **Storage Root**; the OS path is derived only from
  server-generated ids (`{tenant}/{workspace_id}/{file_id}/r{revision}.blob`). User names never
  touch the file system.
- Every path is resolved and confirmed to stay inside the root; `..`, absolute paths, NUL,
  control characters, and any symlink component are rejected (`invalid_path`).
- Limits: per-file size, per-workspace bytes, per-workspace file count, upload body size.
- Uploads are streamed to a temp file, fsynced, then atomically renamed; failures remove the
  temp file. Startup performs a minimal orphan sweep (temp files, blobs without metadata).
- After workspace deletion, file content, chunks, vectors and search results are unreachable
  (`workspace_not_found` / `file_not_found`), even if blobs still exist on disk.
- No archive auto-extraction and no executable execution in this scope.

## 7. Mock Provider (Canvas independence)

`app/workspace/mock.py::MockWorkspaceProvider` implements the §2 interface in memory and
`app/workspace/mock.py::build_mock_router()` exposes the §3 subset needed by Canvas
(workspace get/access/files/read/create/artifact references). It needs no database, no storage
root and no auth service; it is enabled only for the preview app
(`/api/v1/workspaces/_mock/*`) and is never mounted in production.

TypeScript peers use `createMockWorkspaceProvider()` from the client contract file with the
same shapes, so Canvas UI can be developed and tested offline.

## 8. Preview runtime

```
python -m uvicorn app.workspace.preview_app:app --host 127.0.0.1 --port 8210
```

The preview app mounts only `/api/v1/workspaces` (+ `_mock`) and does not import the R1.3
runtime, `AGPolicy` or `ActionValidator`. Integration into the main app later requires one
router inclusion in `app/main.py` (listed in the handover document), nothing else.

## 9. Change policy

Breaking changes to this contract require a note to Server-HQ Supervisor and both engineers.
Additive fields are allowed without a version bump. The contract revision is tracked in
`WORKSPACE_CONTRACT_VERSION` (`app/workspace/contracts.py`).
