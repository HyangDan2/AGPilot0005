# AGCore R2-W — Workspace Shared Contract

Status: **FROZEN PREVIEW CONTRACT (R2-W, v1) + Amendment 01 (folder-based workspace) + Amendment 02 (agent tools & chat scope)**
Contract revision: `r2-w/v1+a02` (`WORKSPACE_CONTRACT_REVISION`)
Owner: Workspace/RAG Engineer (`feat/r2-workspace-rag`)
Consumers: Canvas Engineer (`feat/r2-canvas`), HD2AI Frontend (`AGPilot0011`), Server-HQ Supervisor
Baseline: AGCore **v1.2.0 (`0d2047f`)**, AGTools **`6d0f57f`**, HD2AI **`b4913d0`**

Amendment 01 is **additive**. Every v1 type, operation, error code and field keeps its meaning;
new members are optional for a v1 consumer. Sections §1–§9 are the frozen v1 contract, §A1 is
Amendment 01 (folder-based workspace). The v1 operation name list (`provider_operations()`) stays
frozen; Amendment 01 operations are listed by `provider_operations_a01()`. Amendment 02 (§A2) adds
**server-side agent tools** and a **chat workspace scope**: it does not add a provider operation and
the `WorkspaceProvider` interface is unchanged.

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
`WORKSPACE_CONTRACT_VERSION` (`app/workspace/contracts.py`); Amendment 01 is tracked separately in
`WORKSPACE_CONTRACT_AMENDMENT` / `WORKSPACE_CONTRACT_REVISION`.

---

# Amendment 01 — Folder-Based Workspace

A workspace is no longer only a logical upload space. It can be rooted in a **real folder**: an
existing server folder mounted zero-copy, a folder the server creates and manages, or a folder the
user picks in the browser. The work order's ten requirements map to §A1.1–§A1.10.

## A1.1 Root and Storage Provider (§1, §7 of the work order)

`WorkspaceIdentity.root` describes where the root lives:

```json
"root": {
  "storage_provider": "server_mounted_folder",
  "root_id": "lab-docs",
  "display_path": "/srv/hd2lab/workspaces/lab-docs",
  "allowed_root_id": "lab",
  "read_only": false,
  "zero_copy": true,
  "server_side_path_access": true,
  "client_handle_id": null,
  "opened_at": "2026-11-27T02:41:09.004Z",
  "status": "available"
}
```

| `storage_provider` | Root | Content origin | Server path access |
| --- | --- | --- | --- |
| `server_managed` (no root folder) | v1 upload workspace | `blob` (content-addressed copy) | n/a |
| `server_managed` (folder root) | folder the server created under an allowed root | `external` (zero-copy) | yes |
| `server_mounted_folder` | existing server folder, mounted in place | `external` (zero-copy) | yes |
| `local_browser` | folder picked in the browser (File System Access API) | `external` (browser-owned) | **no** |

`FileIdentity` gains `storage_provider`, `content_origin` (`blob` \| `external`), `relative_path`
(the real path inside the root, POSIX, no leading slash) and `synced_at`. `path` stays the display
path of §1. For `content_origin: external` the bytes are read from the root in place; nothing is
copied into the storage root.

## A1.2 Lifecycle: Open / New / Close / Reopen

`WorkspaceIdentity.lifecycle_state` ∈ `open` \| `closed` \| `deleted` (default `open`).

| Operation | Effect |
| --- | --- |
| `Open Folder` | `POST /workspaces/open-folder` — mount an existing folder; scans it into the index |
| `New Workspace` | `POST /workspaces/new` — create a real folder under an allowed root, then open it |
| `Close` | `POST /workspaces/{id}/close` — watcher stops; reads, writes and indexing return `409 workspace_closed`; **all metadata, revisions and chunks are kept** |
| `Reopen` | `POST /workspaces/{id}/reopen` — re-validates the root, rescans (catch-up) and returns to `open` |
| `Disconnect` | `POST /workspaces/{id}/disconnect` (or `DELETE /workspaces/{id}`) — drops the binding and every metadata row; the real folder is untouched |

`GET /workspaces/{id}/watch` returns `WorkspaceWatchStatus` (`watching`, `last_scan_at`,
`last_change_at`, `changes_total`, `interval_seconds`). `POST /workspaces/{id}/sync` forces one
watcher pass (`{full?:boolean}` → `WorkspaceSyncResult`).

## A1.3 Zero-copy (§3)

Opening an existing folder never copies file bodies. The engine stores only metadata, revisions
(content hash + size per revision) and the RAG index. Quota limits for `external` content are file
count / scanned byte limits, not storage bytes. Revisions of an `external` file keep their hash and
size; older bodies are not retained because the file is the single source of truth.

## A1.4 File watcher → incremental index (§4)

The engine watches each open folder-backed workspace with a lightweight polling scanner
(configurable interval, `AGCORE_WORKSPACE_WATCH_INTERVAL_SECONDS`, default 2s; no external watcher
daemon is required). One pass detects create / modify / move / delete of files and folders and
applies them through the existing revision and index lifecycle:

* create → new `file.created`, `PENDING` index job
* modify (content hash changed) → new `revision`, in-flight job `CANCELLED`, chunks dropped, `file.updated`, `PENDING`
* move/rename (same hash, new path) → `file.moved`, index kept (no reindex)
* delete → chunks/vectors removed, jobs cancelled, `file.deleted`
* a folder move is expressed as the moves of its descendants

Every applied change also appears in `GET /workspaces/{id}/events` as `filesystem.changed` with a
payload `{change, path, file_id, revision}`. A scan that finds the revision's hash no longer matches
the file cancels that index job (`stale_revision`) instead of indexing stale bytes.

## A1.5 Canvas / Coding Agent common write contract (§5)

`PUT /workspaces/{id}/file` with `WorkspaceWriteRequest` writes an output into the same workspace
root, for every provider:

```json
{"path": "reports/summary.md", "content": "# …", "mime_type": "text/markdown",
 "expected_content_hash": null, "create_parents": true, "overwrite": true}
```

Response `WorkspaceWriteResult`: `{workspace_id, path, file, revision, content_hash, written_bytes,
storage_provider, content_origin, client_write}`.

* Paths pass `validate_relative_path()` (absolute paths, `..`, `.`, drive letters, backslashes,
  control characters and over-long segments rejected with `invalid_path`), then every component is
  confined to the root (`path_outside_allowed_root`, `symlink_escape`).
* Folder-backed providers write the real file (atomic temp + `os.replace`, `create_parents`),
  record a new revision and enqueue indexing — the same call the Coding Agent uses.
* `local_browser`: the server has no path access, so it returns `client_write` (a
  `ClientWriteDirective` with `client_handle_id` and `relative_path`) and the browser performs the
  write through its handle, then reports the result. Writes against a `read_only` root return
  `403 read_only_workspace`.
* `expected_content_hash` mismatch returns `409 content_conflict` and changes nothing, so two
  agents writing the same file cannot silently overwrite each other.

The shared helper also exists client-side: `validateRelativePath()` in the TS contract and
`validate_relative_path()` in `contracts.py`.

## A1.6 Disconnect vs. delete (§6)

Disconnecting releases the workspace and keeps the user's files. Deleting real files is never part
of disconnect and is **refused by default**: `POST /workspaces/{id}/disconnect` with
`{delete_source_files: true}` returns `403 source_delete_not_permitted` unless the server operator
enables `AGCORE_WORKSPACE_ALLOW_SOURCE_DELETE=true`, and even then the request must carry
`confirm: true`. The UI shows disconnect and source deletion as two separate actions, and the
default (and only enabled) action is disconnect.

## A1.7 Allowed roots (§9)

Server folder paths (`open_folder`, `new` with a `parent_path`) are only accepted inside a
configured **allowed root** for that user scope:

```
AGCORE_WORKSPACE_ALLOWED_ROOTS="lab:/srv/hd2lab/workspaces:owner,shared:/srv/hd2lab/shared:tenant"
```

Format: `root_id:absolute_path:scope` (`owner` \| `tenant` \| `global`); a fourth `:ro` suffix makes
the root read-only. An owner-scoped base adds `/{tenant}/{owner}`, a tenant-scoped base adds
`/{tenant}`, and a global root uses its configured path. The configuration may name these
scope segments explicitly. IDs consisting of lowercase ASCII letters, digits, hyphens or
underscores up to 64 characters use their original values. Other IDs use `~` followed by the
full lowercase SHA-256 hash of their UTF-8 bytes.

`GET /workspaces/allowed-roots` lists the expanded paths for the caller. Each downstream
deployment configures its own folder bases, Workspace database and blob storage. Validate
existing bindings against the expanded paths before enabling the updated runtime. Copy or
move only files with verified ownership, keeping the existing Workspace and file IDs.

Validation applies to opening, reopening, active operations, and worker startup:

1. the path is absolute and normalizes inside a declared root (`path_outside_allowed_root`)
2. no `..` / control characters, and no symlink component inside the root (`symlink_escape`)
3. the resolved root is a directory (`root_unavailable`)
4. the metadata store and the storage root are **not** inside the workspace root (`metadata_overlap`)

## A1.8 Browser constraints (§8)

`local_browser` workspaces are created by `POST /workspaces/local-browser`
(`LocalBrowserWorkspaceRequest`: `client_handle_id`, `display_path`, `entries[]` with per-file
`path`, `size_bytes`, optional `content_hash` and the `text` the browser captured for indexing).
The server stores index metadata only for those entries, never a server-side path, and always
answers `server_side_path_access: false`. Reads of a browser workspace are served from the captured
text/index; writes are returned as `client_write` directives. No endpoint accepts a client-local
absolute path as a server path.

## A1.9 Metadata and index separation (§10)

`GET /workspaces/{id}/root` also returns `IndexStorageInfo`: metadata, chunk index and vectors live
in the state directory SQLite database (`state://workspace.db`), never inside the workspace root.
Directory scans skip the engine's own bookkeeping and ignore these default excluded names when
indexing: `.git`, `node_modules`, `__pycache__`, `.venv`, `venv`, `dist`, `build` (configurable via
`AGCORE_WORKSPACE_SCAN_EXCLUDES`). Opening a root that contains the database file is refused
(`metadata_overlap`), which is the structural guarantee that the user's folder is never polluted.

## A1.10 Errors added by Amendment 01

| code | HTTP | Meaning |
| --- | --- | --- |
| `workspace_closed` | 409 | workspace is closed; reopen it first |
| `read_only_workspace` | 403 | the root was opened read-only |
| `path_outside_allowed_root` | 403 | path is not inside a declared allowed root |
| `symlink_escape` | 400 | a symlink component would escape the root |
| `root_unavailable` | 409 | the root folder is missing or unreadable (open/reopen/sync) |
| `provider_mismatch` | 400 | operation not valid for this storage provider |
| `content_conflict` | 409 | `expected_content_hash` no longer matches |
| `source_delete_not_permitted` | 403 | disconnecting may not delete real files |
| `metadata_overlap` | 400 | workspace root would contain the metadata store |
| `watch_unavailable` | 409 | watcher cannot run for this provider/lifecycle |

## A1.11 Mock provider (Canvas independence, unchanged guarantee)

`MockWorkspaceProvider` (Python) and `createMockWorkspaceProvider()` (TS) implement the Amendment 01
lifecycle in memory: `openFolder`, `newWorkspace`, `closeWorkspace`, `reopenWorkspace`,
`disconnectWorkspace` (refuses source deletion), `writeFile` (returns `client_write` for
`local_browser`) and `syncWorkspace`. The preview router exposes the same paths under
`/api/v1/workspaces/_mock/*`. A v1 consumer that only calls §2 operations keeps working unchanged.

---

# Amendment 02 — Agent Tools & Chat Workspace Scope

`r2-w/v1+a02`. Two additions, both server-side; no `WorkspaceProvider` operation changes.

## A2.1 Agent tools (11)

`app/workspace/tools.py` → `TOOL_MANIFEST` (`version: r2-w/v1+a02`). These are AGCore **tools**, not
client provider operations:

| # | Tool | side_effects | Required approval |
| --- | --- | --- | --- |
| 1 | `workspace_list_files` | none | – |
| 2 | `workspace_find_files` | none | – |
| 3 | `workspace_get_file_info` | none | – |
| 4 | `workspace_read_file` | none | – |
| 5 | `workspace_search_text` | none | – |
| 6 | `workspace_retrieve` | none | – |
| 7 | `workspace_create_file` | workspace_write | overwrite: `overwrite` + `confirm_overwrite` |
| 8 | `workspace_patch_file` | workspace_write | – (revisioned edit with `expected_revision`) |
| 9 | `workspace_move_file` | workspace_write | target exists: `confirm_overwrite` |
| 10 | `workspace_delete_file` | irreversible_workspace_delete | `confirm=true` (user) |
| 11 | `workspace_create_directory` | workspace_write | – |

`workspace_search` and `workspace_get_context` remain callable for v1 callers but are no longer in
the manifest (agents use `workspace_retrieve`). No terminal/OS command tool exists.

A refusal returns status `approval_required` with `context = {action, path, file_id, required[]}` and
changes nothing; the caller repeats the call with the approval flags.

## A2.2 Chat workspace scope

A chat request may carry `workspace: {workspace_id, enabled?, context_budget_tokens?,
granted_capabilities?}`. The server attaches `workspace_context` with exactly:
`workspace_id`, `workspace_root`, `storage_provider`, `execution_scope`, `granted_capabilities`,
`file_metadata`, `retrieval_availability` (+ `context_policy.file_contents_included = false`).
File bodies are never part of this block; content is fetched by tool calls. Without a workspace
selection the request is passed through unchanged.

`POST /api/v1/workspaces/{workspace_id}/chat/context` returns that block;
`POST /api/v1/workspaces/{workspace_id}/agent/turn` runs one agent turn
(`message`, `conversation_id?`, `approvals?`, `capability?`, `context_budget_tokens?`) and returns
`intent`, `plan`, `tool_calls[]`, `citations[]`, `files[]`, `verification`, `pending_approval`,
`retrieval_availability` and a labelled `llm` block (`mock: true` while no LLM is connected).

## A2.3 Workspace → Canvas handoff (WO-C18)

| Workspace value | Canvas endpoint / field |
| --- | --- |
| `workspace_id`, `file_id`, `revision`, `content_hash` | `POST /artifacts/from-workspace` |
| `content_hash` | `last_saved_content_hash` → `409` when the canvas copy is newer |
| `PUT /workspaces/{id}/file` + `expected_content_hash` | `POST /artifacts/{id}/save-to-workspace` |
| artifact reference (`kind=canvas`) | `GET /artifacts/{id}/workspace-link` |

Open contract gaps (backlog R2W-B9/B10):
1. **`client_read`** — no request/response contract for the server to ask the browser for an
   existing file body in a `local_browser` workspace.
2. **`client_write` acknowledgement** — no contract for the browser to report the result
   (`content_hash`, `revision`, failure) of a `ClientWriteDirective`.

## A2.4 Errors added by Amendment 02

| code | HTTP | Meaning |
| --- | --- | --- |
| `approval_required` | 409 | write/delete needs the user's approval (`confirm*` flags) |
| `patch_conflict` | 409 | `edits[].find` missing or ambiguous |
| `section_not_found` | 404 | markdown section to replace does not exist |
| `capability_denied` | 403 | tool not granted by the capability profile / read-only |
