# AGCore R2 — Canvas Backend Handover (CP-06 / WO-C15 · WO-C16)

Recorded 2026-11-27 (KST) by the Canvas Engineer (피클 / 향단이), branch `feat/r2-canvas`.
Status: **backend complete and locally verified, not merged, not deployed, no production change.**
`main` was not touched, no pull request was opened, and no operating AGCore store, `Server-HQ`
configuration or gateway was modified.

Companion documents:

| Document | Content |
| --- | --- |
| `docs/r2/CANVAS_BASELINE.md` | CP-00 baseline, repository survey, existing-code reuse decisions, Workspace/RAG boundary |
| `docs/r2/CANVAS_BACKEND_HANDOVER.md` (this file) | CP-06: final architecture, contracts, verification results, merge/next-step instructions |

---

## 1. Repository baseline and commits

| Item | Value |
| --- | --- |
| Repository | `HyangDan2/AGPilot0005` (private, GitHub App connection `@HyangDan2`) |
| Baseline | `0d2047f09a67f4cfd44f8c5399523eab3eac2172` — tag `v1.2.0`, "docs(release): finalize CHANGELOG for v1.2.0" |
| Working branch | `feat/r2-canvas`, branched from the baseline (not from `main` head of any other branch) |
| `main` state | unchanged at `0d2047f` (`main == origin/main == v1.2.0`) |
| Commits | see below |

| Commit | Phase | Content |
| --- | --- | --- |
| `fc29cd5` | CP-00 / WO-C01 | Baseline record, existing-code survey, Canvas/Workspace contracts |
| `b0cf968` | CP-01 · CP-02 | Artifact engine core: types, models, storage, manager, revisions, patch engine, diffing, API router. This commit also carries the first version of `events.py`, `editing.py`, `export.py`, `security.py`, `tools.py`, `workspace.py` and the shared-file hooks (see §14) |
| `af4d4fd` | CP-03 / WO-C06·C07·C08 | Generation stream contract, partial editing proposals, eight Canvas tools, opt-in tool registration (`AGCORE_CANVAS_TOOLS_ENABLED`) |
| `0b95c33` | CP-05 / WO-C13·C14 | Export provider closure, HTML/preview sanitizer hardening, security & resource-control test suite |
| CP-06 | WO-C15·C16 | This handover document (and the tool-manifest wording that matches the opt-in flag) — `git log -1 -- docs/r2/CANVAS_BACKEND_HANDOVER.md` for the exact SHA |

Push status: `feat/r2-canvas` **is pushed to `origin`** (branch tip at handover: `git ls-remote origin feat/r2-canvas`). The first push attempt returned HTTP 403 from the GitHub App write path and the immediate retry succeeded; if the connection stops accepting writes, the operator must transfer the branch — never force-push or merge `main`. No pull request was opened and `main` remains at `0d2047f`.

---

## 2. Module inventory

### 2.1 New Canvas package (`app/canvas/`)

| Module | Responsibility | ≈LOC |
| --- | --- | --- |
| `__init__.py` | Public exports of the package | 21 |
| `config.py` | `AGCORE_CANVAS_*` settings resolved at call time; production downgrade of dev adapters | 132 |
| `errors.py` | `CanvasError` + `CanvasErrorCode` → HTTP status map; R2-W-compatible envelope | 78 |
| `types.py` | Extensible artifact type registry (5 enabled, 4 reserved), language → extension map | 227 |
| `models.py` | Pydantic domain models: `Artifact`, `Revision`, `PatchRequest`, `PatchOutcome`, `EditProposal`, pages | 143 |
| `offsets.py` | UTF-16 ↔ code-point offset contract, surrogate-split rejection, content metrics | 137 |
| `storage.py` | `CanvasStorage` protocol + SQLite implementation with content-addressed blob store | 874 |
| `manager.py` | Artifact CRUD, list/get, rename, soft delete/purge, workspace & conversation links, export/preview entry points | 359 |
| `revisions.py` | Revision history, revision diff, non-destructive restore, `PatchEngine` | 202 |
| `patching.py` | Pure patch computation for six operations (conflict / expected-text checks) | 201 |
| `diffing.py` | Line-based diff with a bounded line budget | 211 |
| `editing.py` | AI edit adapter port, dev adapter, edit proposal lifecycle, bounded context envelope | 552 |
| `events.py` | SSE event contract, streaming generation session, idempotent replay | 397 |
| `export.py` | Export providers (md/txt/html/json/source), safe filename + RFC 5987 disposition, office contract | 243 |
| `security.py` | Preview sanitizer & CSP policy, title/identifier hygiene, scope-override guard, token estimate | 293 |
| `tools.py` | Eight-tool manifest, handlers, `install_canvas_tools()` (idempotent) | 467 |
| `service.py` | `CanvasService` singleton wiring storage/types/workspace/exporter/adapter/managers | 125 |
| `schemas.py` | Re-exported event-type constants | 16 |
| `workspace.py` | `WorkspaceGateway` protocol, runtime/dev/unavailable providers, `WorkspaceAccess` | 259 |

### 2.2 API and tests

| File | Content |
| --- | --- |
| `app/api/canvas.py` | `/api/v1/canvas` router (24 endpoints), request models with `extra="forbid"`, structured errors (496 LOC) |
| `tests/conftest.py` | `canvas` / `configured` fixtures (throw-away data dir + service reset) — new file, 36 LOC |
| `tests/r2_canvas_support.py` | Scope/header/client helpers, `configure()` (54 LOC) |
| `tests/test_r2_canvas_foundation.py` | CP-01: types, manager, persistence, restart recovery, limits (11 tests) |
| `tests/test_r2_canvas_revision_patch.py` | CP-02: revisions, restore, diff, patch conflicts, offsets (7 tests) |
| `tests/test_r2_canvas_ai.py` | CP-03: generation stream, edit proposals, tools, opt-in registration (17 tests) |
| `tests/test_r2_canvas_export_security.py` | CP-05: export formats, filename/header hardening, sanitizer, isolation, limits (10 tests) |

### 2.3 Shared files touched (merge targets, see §14)

| File | Change |
| --- | --- |
| `app/main.py` | +1 import, +1 `include_router(canvas_router, prefix="/api/v1")` |
| `app/schemas/events.py` | +7 literal values in `EventType` (`artifact.*`); nothing renamed/removed |
| `app/tools/registry.py` | opt-in registration block + `CANVAS_TOOLS_ENABLED` / `canvas_tools_enabled()` |
| `app/tools/policy.py` | +8 names in `BUILTIN_TOOLS`; no profile and no `service_bindings` change |
| `.gitignore` | +`data/canvas/` (development store) |

No file under `app/runtime/`, `app/runtime/policy/`, `app/api/agent.py`,
`app/api/document_analysis.py`, `app/gateway/`, `app/adapters/` or `app/workspace/` is modified.

---

## 3. Canvas architecture

```
HD2AI frontend (AGPilot0011, CP-04)
        │  REST + SSE (browser)
        ▼
app/api/canvas.py  ── require_authenticated_caller ──▶ ExecutionScope
        │                                              (tenant · owner · service · role)
        ▼
CanvasService (process singleton)
  ├─ ArtifactManager      (CRUD, links, export/preview entry)
  ├─ RevisionManager      (history, diff, restore)
  ├─ PatchEngine          (atomic patch + conflict control)
  ├─ ProposalService      (AI edit propose/apply/reject) ── EditAdapterPort
  └─ GenerationService    (SSE generation sessions)
        │
        ├─ SqliteCanvasStorage  → canvas.db (metadata) + blobs/ (content-addressed)
        ├─ WorkspaceGateway     → dev | workspace (lazy bind) | unavailable
        └─ CompositeExportProvider → text/markdown/html/json/source

Dynamic Tool Registry ──install_canvas_tools()──▶ same CanvasService singleton
(opt-in: AGCORE_CANVAS_TOOLS_ENABLED)                (tool call and HTTP call see the same data)
```

Design rules encoded in the code:

1. **Explicit creation only.** An artifact exists because a user request, an explicit tool call or
   an explicit generation event created it. Nothing parses model output for code fences.
2. **Identity comes from the verified scope.** `tenant_id`/`owner_id`/`service_id` are never read
   from a body, query, tool argument or artifact content. Request models use `extra="forbid"`, and
   `security.reject_scope_override()` refuses scope keys in tool/proposal payloads with 403.
3. **Isolation by scope tuple.** All reads and writes are keyed by `(tenant, owner, service)`;
   a foreign artifact id returns `404 artifact_not_found`, never 403, so existence is not leaked.
4. **Lifecycle independent of chat.** `conversation_id` / `workspace_id` are optional links;
   deleting a conversation never deletes an artifact (and vice versa).
5. **No generated-code execution, no outbound network.** Canvas never runs artifact content and
   never fetches a URL from content; previews are sanitized HTML rendered in a sandboxed iframe.
6. **Fail closed.** Production refuses dev adapters (`AGCORE_ENV=production` → dev workspace/AI
   adapters are replaced by the runtime/unavailable providers); persistence failures raise
   `storage_failure` instead of returning a success payload.

---

## 4. Artifact schema

### 4.1 `Artifact` (metadata)

| Field | Type | Notes |
| --- | --- | --- |
| `artifact_id` | string | `art_` + 12 hex |
| `workspace_id` | string \| null | optional link; access checked through the Workspace gateway |
| `conversation_id` | string \| null | optional link; never a lifecycle dependency |
| `tenant_id` / `owner_id` / `service_id` | string | from `ExecutionScope` only |
| `title` | string | sanitized (control chars/path separators/compact whitespace), ≤ 200 chars |
| `artifact_type` | string | one of the registry keys (`markdown`, `plain_text`, `source_code`, `html`, `structured_json`) |
| `language` | string \| null | normalized; required for `source_code` |
| `current_revision` | int | head pointer; starts at 1 |
| `content_hash` | string | `sha256:<hex>` of the current content |
| `created_at` / `updated_at` | string | ISO-8601 UTC with `Z` |
| `creation_source` | enum | `user \| ai \| import \| tool` |
| `deleted_at` | string \| null | soft-delete marker |

```json
{
  "artifact_id": "art_8ce43de6a789",
  "workspace_id": null,
  "conversation_id": null,
  "tenant_id": "default_tenant",
  "owner_id": "u1",
  "service_id": "hd2ai-admin",
  "title": "미리보기 문서",
  "artifact_type": "markdown",
  "language": null,
  "current_revision": 1,
  "content_hash": "sha256:…",
  "created_at": "2026-11-27T02:15:03.412Z",
  "updated_at": "2026-11-27T02:15:03.412Z",
  "creation_source": "user",
  "deleted_at": null
}
```

### 4.2 `Revision` (immutable)

`artifact_id`, `revision` (1…N), `content_hash`, `size_bytes`, `actor` (`user|ai|import|system`),
`change_reason`, `operation` (`create|replace_content|replace_selection|insert_before|insert_after|
append_content|delete_selection|restore`), `base_revision`, `created_at`, `created_by`,
`proposal_id`.

### 4.3 Artifact types (registry)

| Key | Enabled | Default ext. | Export formats | Preview family |
| --- | --- | --- | --- | --- |
| `markdown` | ✅ | `.md` | md, txt, html | text |
| `plain_text` | ✅ | `.txt` | txt | text |
| `source_code` | ✅ (language required) | language map (`python`→`.py`, …) | source, txt | code |
| `html` | ✅ | `.html` | html, txt | html (sanitized) |
| `structured_json` | ✅ (JSON validated) | `.json` | json, txt | json |
| `chart`, `table`, `spreadsheet` | ❌ reserved | `.json` | json | json |
| `office_document` | ❌ reserved | `.docx` | docx | text |

Adding a type is one `ArtifactTypeRegistry.register()` call plus an export entry if needed; there is
no schema migration because `artifact_type` is a textual key validated against the registry.

### 4.4 `EditProposal`

`proposal_id` (`prp_`+12 hex), `artifact_id`, `base_revision`, `operation`, `selection`,
`expected_original_text`, `replacement_text`, `change_reason`, `status`
(`proposed|applied|rejected|stale`), `actor`, `adapter`, `model_label`,
`ai_integration_verified` (always `false` in R2), `created_at`, `updated_at`, `applied_revision`,
`request`, `context_budget`, `preview`.

### 4.5 Storage tables (SQLite, schema version 1)

`canvas_meta` (schema_version), `canvas_artifacts`, `canvas_revisions` (PK
`artifact_id, revision`, FK cascade), `canvas_events` (PK `scope_key, idempotency_key`),
`canvas_edit_proposals`. Indexes cover owner/scope ordering, conversation and workspace lookups,
and proposal listing.

---

## 5. Canvas API contract

Base: `/api/v1/canvas`. Every endpoint requires a verified `ExecutionScope`
(`Depends(require_authenticated_caller)`). The final endpoint shape is the table below; it is the
frozen R2 contract referenced by `CANVAS_BASELINE.md` §4.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/capabilities` | engine/storage/limits/workspace/AI-edit/export/event contract |
| GET | `/types` | artifact type catalogue incl. reserved entries, offset units |
| GET | `/tools/manifest` | eight-tool manifest for the registry/frontend |
| POST | `/artifacts` | create (201); optional `idempotency_key`, links |
| GET | `/artifacts` | list by `conversation_id`, `workspace_id`, `artifact_type`, `limit` (≤100), `offset` |
| GET | `/artifacts/{artifact_id}` | metadata + current (or `?revision=`) content |
| PATCH | `/artifacts/{artifact_id}` | rename |
| DELETE | `/artifacts/{artifact_id}` | soft delete; `?purge=true` hard delete (rows) |
| POST | `/artifacts/{artifact_id}/patches` | atomic patch (409 on revision/text conflict) |
| POST | `/artifacts/{artifact_id}/patches/preview` | diff of a patch, writes nothing |
| GET | `/artifacts/{artifact_id}/diff` | diff between `from_revision`/`to_revision` |
| GET | `/artifacts/{artifact_id}/revisions` | revision history (metadata, newest first) |
| GET | `/artifacts/{artifact_id}/revisions/{rev}` | one revision's content |
| POST | `/artifacts/{artifact_id}/revisions/{rev}/restore` | non-destructive restore (new revision) |
| POST | `/artifacts/{artifact_id}/links` | link workspace/conversation (workspace access checked) |
| DELETE | `/artifacts/{artifact_id}/links/{kind}` | unlink `workspace` or `conversation` |
| GET | `/artifacts/{artifact_id}/export` | export current/selected revision (`format`, `revision`) |
| GET | `/artifacts/{artifact_id}/preview` | sanitized preview payload + policy |
| POST | `/artifacts/{artifact_id}/edit-proposals` | AI edit proposal (dev adapter) (201) |
| GET | `/artifacts/{artifact_id}/edit-proposals` | list proposals (`status`, paging) |
| GET | `/edit-proposals/{proposal_id}` | one proposal (+ `is_stale`) |
| POST | `/edit-proposals/{proposal_id}/apply` | apply an approved proposal |
| POST | `/edit-proposals/{proposal_id}/reject` | reject a proposal |
| POST | `/generations` | explicit artifact generation (dev SSE contract; `?stream=true`) |

### 5.1 Error envelope and codes

```json
{"detail": {"code": "revision_conflict", "message": "stale base revision", "retryable": false,
            "context": {"base_revision": 2, "current_revision": 3}}}
```

| Code | HTTP | Meaning |
| --- | --- | --- |
| `artifact_not_found` | 404 | unknown, deleted or not visible to this scope |
| `revision_not_found` | 404 | revision outside the artifact's history |
| `revision_conflict` | 409 | `base_revision` ≠ current revision |
| `expected_text_mismatch` | 409 | `expected_original_text` ≠ text at the range |
| `invalid_selection` | 422 | out of range, inverted, or splits a surrogate pair |
| `invalid_request` | 422 | schema/semantic validation failure |
| `unsupported_artifact_type` | 415 | type not in the registry (or reserved) |
| `content_too_large` | 413 | above `CANVAS_MAX_CONTENT_BYTES` |
| `revision_limit_reached` | 409 | artifact reached `CANVAS_MAX_REVISIONS` |
| `proposal_not_found` | 404 | unknown proposal id |
| `proposal_stale` | 409 | proposal base revision no longer current |
| `workspace_unavailable` | 503 | workspace gateway not configured/healthy |
| `workspace_access_denied` | 403 | access check returned `none` |
| `export_format_unsupported` | 400 | format not available for this type (incl. office) |
| `ai_adapter_unavailable` | 503 | no verified AI edit adapter |
| `storage_failure` | 500 | persistence failed; nothing committed |
| `unauthenticated` | 401 | no verified scope |
| `forbidden` | 403 | scope override fields, disabled engine |

### 5.2 Request rules

* `extra="forbid"` on every request model: `tenant_id`, `owner_id`, `service_id`, `role` and any
  other unknown key are rejected with 422 before the handler runs.
* `patch` requires `base_revision` and, for region operations, `selection` /
  `expected_original_text`; the write is compare-and-commit inside one transaction.
* `artifact create` accepts `creation_source` (`user|ai|import|tool`) and an optional
  `idempotency_key`; a duplicate key returns the original artifact (`duplicate: true`).

### 5.3 Offset contract (backend ↔ frontend)

`{"start": int, "end": int, "offset_unit": "utf16" | "codepoint"}`; **`utf16` is the default**
(browser `String.prototype.slice` counts UTF-16 code units). The backend converts to code points
and rejects an offset that splits a surrogate pair (`invalid_selection`). Every response that
echoes a range also returns `content_metrics: {codepoints, utf16_units, utf8_bytes}`.
`utf8_byte` offsets are not accepted.

### 5.4 Export response headers

`Content-Type` from the format, `Content-Disposition` (RFC 5987 `filename*` + ASCII fallback, no
CR/LF), `X-Content-Type-Options: nosniff`, `Cache-Control: no-store`,
`X-Canvas-Revision`, `X-Canvas-Format`, and `X-Canvas-Export-Warnings` (comma-separated warning
codes such as `html_sanitized`, `html_export_escaped`).

---

## 6. Workspace integration contract

Canvas consumes the frozen R2-W contract (`WORKSPACE_CONTRACT_VERSION = r2-w/v1`) and implements
**no** workspace storage, indexing, authorization or file I/O.

| Operation | Canvas use | Failure mapping |
| --- | --- | --- |
| `check_workspace_access(workspace_id, scope)` → level `read/write/admin/none` | before create-with-workspace and before linking | `none` → `403 workspace_access_denied`; provider down → `503 workspace_unavailable` |
| `get_workspace(workspace_id, scope)` | existence/identity check when linking | not found → `404 workspace_unavailable` (no id disclosure) |
| `register_artifact_reference(workspace_id, scope, {artifact_id, kind, metadata})` | after a successful link so the workspace can list the artifact | failure keeps the local link and reports `workspace_sync: {"status": "pending", …}` — never reported as synced |
| `list_artifact_references(workspace_id, scope)` | diagnostics/reconciliation only | reported as unavailable, not fatal |

### Integration order (Workspace → Canvas)

1. **Workspace first.** Merge `feat/r2-workspace-rag` (its `app/workspace/contracts.py`,
   `docs/r2/WORKSPACE_CONTRACT.md`, `WORKSPACE_CONTRACT_VERSION = r2-w/v1`) to the integration
   branch before Canvas. Canvas has no code dependency on that branch today (it builds and tests
   without it), so this order is about the runtime provider binding, not compilation.
2. **Then Canvas.** Merge `feat/r2-canvas`. The router include in `app/main.py` is additive, and
   the tool registration stays off until `AGCORE_CANVAS_TOOLS_ENABLED` is set.
3. **Bind the provider.** `RuntimeWorkspaceProvider` imports `app/workspace/contracts.py::WorkspaceProvider`
   lazily; once step 1 is present, set `AGCORE_CANVAS_WORKSPACE_PROVIDER=workspace` (production
   forces it even if `dev` is configured).
4. **Verify the mapping.** Run the link flow against the real provider: access `none` → 403,
   unknown workspace → 404, provider error → 503, `register_artifact_reference` failure → local
   link kept with `workspace_sync.status = "pending"`.
5. **Workspace side** stores only `{artifact_id, kind, metadata}` references — never artifact
   content, revisions, patches or exports.

Providers:

* `RuntimeWorkspaceProvider` — lazy bind, `mode = "workspace"`, `enforced = true`.
* `LocalDevWorkspaceProvider` — in-memory, `mode = "dev"`, `enforced = false`, refuses to be
  constructed in production, seeded with `ws_dev00000001`.
* `UnavailableWorkspaceProvider` — fail-closed default (`workspace_unavailable`).

Out of scope for R2: writing an artifact into a workspace file ("Export to workspace"), workspace
file listing, search/embedding/index, and any workspace file storage.

---

## 7. SSE event contract

Seven additive event types in `app/schemas/events.py::EventType` (nothing renamed or removed),
using the existing `SSEEvent` envelope (`run_id`, `event_id`, `timestamp`, `status`, flattened
`data`):

| Event | When | Key data |
| --- | --- | --- |
| `artifact.created` | stream start (provisional) | `artifact_id` (provisional), `title`, `artifact_type`, `language`, `provisional: true`, `persisted: false` |
| `artifact.delta` | each chunk | `seq` (1..N, monotonic), `delta`, `accumulated_utf16_units`, `provisional: true` |
| `artifact.updated` | commit | `artifact_id` (real), `revision`, `persisted: true`, `content_hash` |
| `artifact.edit.proposed` | AI proposal created | `proposal_id`, `operation`, `selection`, `status: "proposed"` |
| `artifact.edit.applied` | proposal applied | `proposal_id`, `revision` |
| `artifact.edit.rejected` | proposal rejected | `proposal_id`, `reason` |
| `artifact.error` | abort/failure | `code` (e.g. `stream_aborted`), `persisted: false` |

Rules (enforced by tests):

* Deltas are provisional; nothing is written until the stream completes. An aborted or failed
  stream persists no artifact and no revision (`persisted: false`).
* Completion performs exactly **one** `ArtifactManager.create` with the accumulated content, so a
  stream produces exactly one artifact and one revision.
* `idempotency_key` replay returns the same artifact (`duplicate: true`), not a second revision.
* A duplicate delta `seq` returns the original frame; a `seq` gap is rejected (`invalid_request`).
* Type/language validation happens **before** the first frame: `415 unsupported_artifact_type` or
  `422 invalid_request`, with nothing persisted.

Wire form (`?stream=true`, `text/event-stream`, `Cache-Control: no-store`,
`X-Accel-Buffering: no`):

```
event: artifact.delta
data: {"run_id":"gen_…","event_id":"gen_…-3","timestamp":1764…,"status":"streaming",
       "artifact_id":"art_…","session_id":"gen_…","seq":3,"delta":"…",
       "accumulated_utf16_units":42,"provisional":true}
```

The non-streaming form returns the same frames as a JSON list (`events`) plus
`session_id`, `artifact_id`, `committed`, `duplicate`, `revision`, `content_hash`,
`first_event_id`, `final_event`.

---

## 8. Tool manifest

Eight tools, registered in the existing Dynamic Tool Registry (`app/tools/registry.py`).

| Tool | Read-only | Required arguments | Side effect |
| --- | --- | --- | --- |
| `canvas.create_artifact` | no | `title`, `artifact_type`, `content` | creates an artifact + first revision |
| `canvas.read_artifact` | yes | `artifact_id` (optional `revision`) | none |
| `canvas.list_artifacts` | yes | — (filters, paging) | none |
| `canvas.propose_edit` | no | `artifact_id`, `instruction` | stores a proposal; revision unchanged |
| `canvas.apply_edit` | no | `proposal_id` | creates a revision from the proposal |
| `canvas.list_revisions` | yes | `artifact_id` | none |
| `canvas.restore_revision` | no | `artifact_id`, `revision` | appends a restore revision |
| `canvas.export_artifact` | yes | `artifact_id` (optional `revision`, `format`) | none |

Invariants:

* Every parameter schema is closed (`additionalProperties: false`) and contains **no**
  `tenant_id`, `owner_id`, `service_id`, `role` or workspace access field.
* Identity is taken only from `current_execution_scope`; a call without a verified scope returns
  `unauthenticated`.
* Failures return `{"error": true, "code": …, "message": …, "context": …}` so the dispatcher
  records a failed trace instead of a fake success.
* Handlers never execute generated code and never open a terminal.

Enablement (two independent steps):

1. **Registration** — `AGCORE_CANVAS_TOOLS_ENABLED=1` (also `true`/`yes`/`on`) at process start.
   Without it the registry contains exactly the eight R1.3 builtin tools.
2. **Exposure** — add the tool names to the operator's tool profile (`app/tools/policy.py`
   profiles / `tool_policy.json`). No shipped profile lists a `canvas.*` tool.

The wire layer translates `canvas.create_artifact` to the provider-safe
`canvas_create_artifact` form, so no gateway change is required.

---

## 9. Storage and migration policy

| Item | Value |
| --- | --- |
| Metadata | SQLite at `AGCORE_CANVAS_DB_PATH` or `<data_dir>/canvas.db` |
| Content | content-addressed blobs at `<data_dir>/blobs/<hh>/<hh>/<sha256>.txt` |
| Default data dir | `<repo>/data/canvas` (development); `/var/lib/hd2lab-agcore/canvas` only when that root is writable |
| Pragmas | `journal_mode=WAL`, `synchronous=FULL`, `foreign_keys=ON`, `busy_timeout=5000ms` |
| Transactions | `BEGIN IMMEDIATE`; metadata + revision + head pointer commit together |
| Blob write | temp file → `fsync` → `os.replace` (atomic), immutable once named by its hash |
| Schema version | `1` in `canvas_meta`; created idempotently with `CREATE TABLE IF NOT EXISTS` |
| Soft delete | `deleted_at` set; hidden from reads/lists; data retained |
| Purge | deletes artifact/revision/proposal/event rows; content-addressed blobs stay (shared, no GC in R2) |
| Limits | content ≤ 512 KiB, revisions ≤ 100, list ≤ 100/page, diff ≤ 2000 lines, context ≤ 3000 tokens (all `AGCORE_CANVAS_*` overridable) |
| Restart | nothing cached in memory; a new process on the same DB + blob root returns identical data (covered by a test) |

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGCORE_ENV` | `development` | `production` disables dev adapters and forces the runtime/unavailable providers |
| `AGCORE_CANVAS_ENABLED` | `true` | engine on/off (off → 403 `forbidden`) |
| `AGCORE_CANVAS_DATA_DIR` | `<repo>/data/canvas` | DB + blob root |
| `AGCORE_CANVAS_DB_PATH` | `<data_dir>/canvas.db` | explicit DB file |
| `AGCORE_CANVAS_TOOLS_ENABLED` | unset (off) | register the eight Canvas tools in the tool registry |
| `AGCORE_CANVAS_WORKSPACE_PROVIDER` | `dev` | `dev` \| `workspace` \| `unavailable` |
| `AGCORE_CANVAS_AI_ADAPTER` | `dev` | `dev` \| `runtime` |
| `AGCORE_CANVAS_DEV_ADAPTERS` | `true` | ignored/forced off in production |
| `AGCORE_CANVAS_MAX_CONTENT_BYTES` | `524288` | per-artifact content limit |
| `AGCORE_CANVAS_MAX_REVISIONS` | `100` | revision retention per artifact |
| `AGCORE_CANVAS_MAX_TITLE_CHARS` | `200` | title limit |
| `AGCORE_CANVAS_MAX_LIST_LIMIT` | `100` | page size ceiling |
| `AGCORE_CANVAS_DEFAULT_PAGE_LIMIT` | `25` | default page size |
| `AGCORE_CANVAS_MAX_DIFF_LINES` | `2000` | diff line budget |
| `AGCORE_CANVAS_CONTEXT_TOKENS` | `3000` | AI edit context budget |

Migration policy:

* **No production migration in R2.** R1.3 has no artifact data, so there is nothing to migrate
  from; Canvas opens its own store and never touches `trajectory.db`, the analysis directory or
  the R1.3 runtime store.
* Schema changes are additive and gated by `schema_version` in `canvas_meta`; a future change must
  ship an explicit upgrade step (there is no automatic destructive migration).
* To go live later: allocate a dedicated volume, set `AGCORE_CANVAS_DB_PATH` /
  `AGCORE_CANVAS_DATA_DIR`, set `AGCORE_ENV=production`, confirm `workspace` provider binding,
  keep `AGCORE_CANVAS_TOOLS_ENABLED` off until the tool profile is approved, and back up DB +
  blobs together (they are only consistent as a pair).

---

## 10. Preview run

Environment used for verification: `/workspace/.venv-r2canvas` (Python 3.12.16, built from
`requirements.txt`). The workspace default venv `/workspace/.venv-agp` lacks `pytest-asyncio` and
`langgraph`, so use the R2 venv for both tests and preview.

```bash
cd /workspace/repos/AGPilot0005

export AGCORE_CANVAS_DATA_DIR=/tmp/canvas-preview
export AGCORE_TOOL_POLICY_FILE=/tmp/canvas-preview/tool_policy.json
export AGCORE_RUNTIME_CONFIG_PATH=/tmp/canvas-preview/runtime_config.json
export AGCORE_TRAJECTORY_DB_PATH=/tmp/canvas-preview/trajectory.db
export AGCORE_AGTOOLS_ENABLED=false          # no AGTools token in this environment
# export AGCORE_CANVAS_TOOLS_ENABLED=1       # only when testing tool registration

/workspace/.venv-r2canvas/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8213
```

Why those overrides: as a non-root user in this container `/var/lib/hd2lab-agcore` is not
writable, so `app/tools/policy.py` cannot create its default policy file at startup
(`PermissionError: '/var'`), and `tool_policy.sync_all` raises `agtools_token_missing_or_invalid`
when AGTools is enabled without a token. Both are pre-existing R1.3 environment requirements, not
Canvas defects.

Smoke requests (development auth headers are accepted because `127.0.0.1` is a trusted proxy in
development):

```bash
H=(-H "X-HD2Lab-User: preview" -H "X-HD2Lab-Service: hd2ai-admin" -H "X-HD2Lab-Tenant: default_tenant")

curl -s "${H[@]}" http://127.0.0.1:8213/api/v1/health
curl -s "${H[@]}" http://127.0.0.1:8213/api/v1/canvas/capabilities
curl -s "${H[@]}" -H 'Content-Type: application/json' -X POST \
  -d '{"title":"preview doc","artifact_type":"markdown","content":"# T\n\nbody"}' \
  http://127.0.0.1:8213/api/v1/canvas/artifacts
```

Verified on 2026-11-27: `/api/v1/health` → 200, `/canvas/capabilities` returned
`environment=development`, five enabled types and eight tools, artifact creation returned
`art_…` with `current_revision: 1`. With `AGCORE_CANVAS_TOOLS_ENABLED=1`, `/api/v1/agent/agents`
listed all eight `canvas.*` tools; with the flag unset it listed none. Stop the server with
`Ctrl+C`; delete `/tmp/canvas-preview*` afterwards.

Interactive docs: `/docs` (the custom Swagger UI) lists all `/api/v1/canvas` endpoints.

---

## 11. Test results (required verification)

Run command (single run on the CP-06 tree; not repeated):

```bash
cd /workspace/repos/AGPilot0005
/workspace/.venv-r2canvas/bin/pytest tests/ -q
```

Result: **219 passed, 1 warning, 13.65 s** (Python 3.12.16, 2026-11-27).

| Suite | Tests |
| --- | --- |
| R1.3 baseline (`0d2047f`) | 174 |
| `test_r2_canvas_foundation.py` (CP-01) | 11 |
| `test_r2_canvas_revision_patch.py` (CP-02) | 7 |
| `test_r2_canvas_ai.py` (CP-03) | 17 |
| `test_r2_canvas_export_security.py` (CP-05) | 10 |
| **Total** | **219** |

Regression: 0. The only warning is pre-existing
(`test_r1_2_final_cp2::test_ng_02_real_ai_gateway_client_http_fixture_usage` —
"coroutine 'connect_tcp.<locals>.try_connect' was never awaited"), unchanged by Canvas work.

Test-isolation notes:

* Every Canvas test uses a throw-away `AGCORE_CANVAS_DATA_DIR` (tmp_path) and resets the service
  singleton, so no repository data file is written.
* `data/runtime_config.json` was found modified after a test run (the R1.3 config API writes its
  default `compaction_policy` into the local fallback). It was **not** part of Canvas work and was
  reverted (`git checkout -- data/runtime_config.json`); the working tree contains no such change.

---

## 12. Not implemented / not verified

| Item | State |
| --- | --- |
| **Real LLM artifact generation** | **Not verified.** `POST /canvas/generations` is a dev contract that chunks caller-supplied content (`simulate: complete \| abort`); no model is called and `ai_integration_verified` is `false`. |
| **Real LLM AI edit proposals** | **Not verified.** The dev adapter needs `scripted_replacement` or a `dev_rule`; otherwise 503 `ai_adapter_unavailable`. No R1.3 runtime adapter exists yet (`AGCORE_CANVAS_AI_ADAPTER=runtime` → unavailable). |
| Canvas frontend (CP-04) | Out of this task. `AGPilot0011` was not accessible to the GitHub App (404 on installation scope); no UI file was touched. |
| Office export (DOCX/PDF/XLSX/PPTX) | Contract declared in `OFFICE_EXPORT_CONTRACT`, refused with `export_format_unsupported`. No conversion engine. |
| Export into a workspace file | Out of scope; needs the Workspace owner's approval of a file-write API. |
| Live Workspace provider | `RuntimeWorkspaceProvider` is implemented but never exercised against a merged `feat/r2-workspace-rag`; only the dev provider is tested. |
| Tool exposure to a live model | Tools dispatch through `ToolDispatcher` in tests, but no real LLM tool-calling run was made; no shipped profile lists `canvas.*`. |
| Production deployment / migration | Not attempted. No server, systemd unit, reverse-proxy rule or production DB change. |
| Load/performance and multi-process concurrency | Only SQLite `BEGIN IMMEDIATE` + busy timeout is exercised; no load test, no multi-worker run. |
| Blob garbage collection | Purge removes rows; unreferenced content-addressed blobs remain. A GC job is not implemented. |
| Browser-side preview rendering | Backend returns sanitized HTML + CSP/sandbox policy; actual iframe rendering was not verified in a browser (frontend CP-04). |
| Canvas tool profile/binding UI | Not added to the tool admin UI; profile opt-in is a manual file edit. |

---

## 13. R1.3 runtime connection points

1. **Tool registration gate — `app/tools/registry.py`**

   ```python
   CANVAS_TOOLS_ENV = "AGCORE_CANVAS_TOOLS_ENABLED"

   def canvas_tools_enabled() -> bool:
       return (os.environ.get(CANVAS_TOOLS_ENV) or "").strip().lower() in ("1", "true", "yes", "on")

   # inside ToolRegistry._register_default_tools(), after the 8 builtin tools:
   if canvas_tools_enabled():
       from app.canvas.tools import install_canvas_tools
       install_canvas_tools(self)
   ```

   Default (flag absent) → the registry is exactly the R1.3 eight tools; `get_definitions()` and
   every existing route are byte-for-byte unchanged. Read once at process start — restart after
   changing the flag.

2. **Router integration patch — `app/main.py`** (already applied in `b0cf968`; this is the whole
   patch for a cherry-pick)

   ```diff
   +from app.api.canvas import router as canvas_router
   ...
    app.include_router(document_analysis_router, prefix="/api/v1")
   +app.include_router(canvas_router, prefix="/api/v1")
    app.include_router(demo_router, prefix="/api/v1")
   ```

   The Canvas router is not gated by the tool flag: the HTTP API can be present while the model
   cannot see the tools. To hide the API too, condition this include on the same flag.

3. **Tool exposure to the model** — after registration, add the eight names to the service's tool
   profile (`app/tools/policy.py` `DEFAULT_POLICY["profiles"][…]["tools"]` or the runtime
   `tool_policy.json`). Only then do `get_definitions(service)` / the agent runner offer them.
   No `app/runtime/` file needs a change: the runner already asks the registry for definitions and
   the dispatcher resolves `canvas.*` handlers from the same registry.

4. **Runtime dispatch path** — `ToolDispatcher.execute_tool_call` → registry handler →
   `CanvasService` singleton. Nothing in `app/runtime/`, `app/gateway/` or `app/api/agent.py` was
   modified, so R1.3 runs exactly as before whether the flag is on or off.

5. **Events** — the seven `artifact.*` values are additive literals in
   `app/schemas/events.py::EventType`; existing R1.3 event handling and `SSEEvent.to_sse()`
   are untouched. A future R1.3 integration should emit generation frames through
   `ArtifactStreamSession` rather than inventing new names.

6. **Environment prerequisites observed during preview** — `AGCORE_TOOL_POLICY_FILE` must point
   to a writable path when running as non-root, and `AGCORE_AGTOOLS_ENABLED=false` (or a valid
   AGTools token) is required for startup in this container. These are R1.3 constraints, not Canvas
   settings.

7. **Rollback** — unset `AGCORE_CANVAS_TOOLS_ENABLED` (tools disappear), and/or remove the single
   `include_router` line (API disappears). No data migration is needed; the Canvas DB is separate.

---

## 14. Shared-file merge targets and collision risk

| File | Canvas change | Collision risk | How to resolve |
| --- | --- | --- | --- |
| `app/main.py` | +1 import, +1 `include_router` line | Low — every feature branch adds router lines in the same block | Keep both; order of `include_router` calls is irrelevant |
| `app/schemas/events.py` | +7 `EventType` literals at the end of the Literal | Low/medium — other R2 branches may also append event names | Append-only union; never rename existing values |
| `app/tools/registry.py` | `import os`, flag helper, opt-in block at the end of `_register_default_tools()` | Medium — registry/policy work (ADR-0017) may restructure registration | Re-apply the opt-in block after the `_register_default_tools()` builtins; keep `install_canvas_tools(self)` inside the `if` |
| `app/tools/policy.py` | +8 `BUILTIN_TOOLS` names | Medium — tool-profile work may edit `BUILTIN_TOOLS`/`DEFAULT_POLICY` | Append the 8 names; profiles must stay untouched |
| `tests/conftest.py` | new file (Canvas fixtures) | Medium — the baseline had no `conftest.py`, so any other branch adding one conflicts | Merge fixtures; Canvas fixtures only depend on `tmp_path`/`monkeypatch` and do not override global behaviour |
| `.gitignore` | +`data/canvas/` | Very low | Keep both lines |
| `docs/r2/` | `CANVAS_BASELINE.md` (CP-00) + this file | Low | Both belong to Canvas |
| `data/runtime_config.json` | none (test side effect reverted) | — | Do not commit it with Canvas work |

Recommended merge order at integration time: **Workspace/RAG → Canvas** (see §6), then tool
profile changes, then frontend (CP-04). `main` merge is explicitly out of scope for this task and
was not performed.

---

## 15. Next actions

1. The branch is pushed; open a PR against `main` for review — **no merge without the owner's
   explicit approval** (this task forbade a main merge).
2. Merge `feat/r2-workspace-rag` first, then bind `AGCORE_CANVAS_WORKSPACE_PROVIDER=workspace`
   and re-run the link/access tests against the real provider.
3. Connect the R1.3 runtime AI adapter (`AGCORE_CANVAS_AI_ADAPTER=runtime`) and replace the dev
   generation contract with real streaming generation; then flip `ai_integration_verified` only
   with evidence.
4. CP-04 frontend against this frozen API (offset unit `utf16`, SSE frames from §7, preview policy
   from `/preview`).
5. Approve a tool profile entry before enabling `AGCORE_CANVAS_TOOLS_ENABLED` in any shared
   environment.
6. Plan production storage (dedicated DB + blob volume, backup pair, blob GC) before the first
   real deployment.
