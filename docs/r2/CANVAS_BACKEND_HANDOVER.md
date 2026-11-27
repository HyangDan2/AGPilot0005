# AGCore R2 — Canvas Backend Handover (CP-06 / WO-C15 · WO-C16 · WO-C17 · R2-W Amendment 01)

Recorded 2026-11-27 (KST) by the Canvas Engineer (피클 / 향단이), branch `feat/r2-canvas`.
Status: **backend complete and locally verified, not merged, not deployed, no production change.**
WO-C17 (Canvas Backend Correction, `C-FIX-01` … `C-FIX-07`) is applied on top: the feature is now
off by default, workspace write/read authority is separated and re-checked on every path, proposal
apply is one transaction, the revision cap prunes instead of blocking, and applying a proposal
requires a verified user approval (§16).
**R2-W Amendment 01 (`r2-w/v1+a01`, folder-based workspace)** is consumed on top of that (§17):
workspace lifecycle (`open`/`closed`/`deleted`), read-only roots, the three storage providers
including `local_browser`, and "export to workspace" through the shared
`PUT /workspaces/{id}/file` write contract. v1 behaviour is unchanged and the frozen v1 surface
keeps its meaning.
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
| `c615874` | WO-C17 / C-FIX-05 | Feature isolation behind `AGCORE_CANVAS_ENABLED` (default **false**), separate `AGCORE_CANVAS_DEV_PREVIEW`, `/capabilities` isolation/retention/approval report, `app/canvas/approval.py` |
| `136ac0f` | WO-C17 / C-FIX-01·02 | Workspace write-vs-read authority, live access re-check on every artifact path, list filtering for revoked artifacts |
| `db551a3` | WO-C17 / C-FIX-03·04·06 | Atomic proposal apply (one `BEGIN IMMEDIATE`), revision retention by pruning, user-approval gate for apply |
| `f383aff` | WO-C17 tests | `tests/test_r2_canvas_correction.py` (18) plus the corrected existing Canvas expectations |
| `769d6e8` | R2-W Amendment 01 | `r2-w/v1+a01` consumer: lifecycle/read-only/storage-provider, export-to-workspace, contract error bridge (§17) |
| `7315d73` | R2-W Amendment 01 tests | `tests/test_r2_canvas_workspace_a01.py` (27) |

Push status: `feat/r2-canvas` **is pushed to `origin`** (branch tip at handover: `git ls-remote origin feat/r2-canvas`). The first push attempt returned HTTP 403 from the GitHub App write path and the immediate retry succeeded; if the connection stops accepting writes, the operator must transfer the branch — never force-push or merge `main`. No pull request was opened and `main` remains at `0d2047f`.

---

## 2. Module inventory

### 2.1 New Canvas package (`app/canvas/`)

| Module | Responsibility | ≈LOC |
| --- | --- | --- |
| `__init__.py` | Public exports of the package | 21 |
| `config.py` | `AGCORE_CANVAS_*` settings resolved at call time; feature flag + separate dev-preview gate; production downgrade of dev adapters; retention window | 178 |
| `approval.py` | User-approval contract for `apply` (`UserApproval`, role rules, `require_user_approval`, policy report) | 129 |
| `errors.py` | `CanvasError` + `CanvasErrorCode` → HTTP status map; R2-W-compatible envelope; Amendment 01 / contract error codes (§17.4) | 121 |
| `types.py` | Extensible artifact type registry (5 enabled, 4 reserved), language → extension map | 227 |
| `models.py` | Pydantic domain models: `Artifact`, `Revision`, `PatchRequest`, `PatchOutcome`, `EditProposal`, pages (page reports workspace exclusions) | 148 |
| `offsets.py` | UTF-16 ↔ code-point offset contract, surrogate-split rejection, content metrics | 137 |
| `storage.py` | `CanvasStorage` protocol + SQLite implementation, blob store, atomic `apply_proposal`, retention pruning + prune audit | 1156 |
| `manager.py` | Artifact CRUD, list/get, rename, soft delete/purge, workspace & conversation links, export/preview entry points, workspace level enforcement + list filtering | 575 |
| `revisions.py` | Revision history, revision diff, non-destructive restore, `PatchEngine` (workspace-aware) | 222 |
| `patching.py` | Pure patch computation for six operations (conflict / expected-text checks) | 201 |
| `diffing.py` | Line-based diff with a bounded line budget | 211 |
| `editing.py` | AI edit adapter port, dev adapter, edit proposal lifecycle (approval-gated apply), bounded context envelope | 576 |
| `events.py` | SSE event contract, streaming generation session (dev-preview gated), idempotent replay | 417 |
| `export.py` | Export providers (md/txt/html/json/source), safe filename + RFC 5987 disposition, office contract | 243 |
| `security.py` | Preview sanitizer & CSP policy, title/identifier hygiene, scope-override guard, token estimate | 293 |
| `tools.py` | Eight-tool manifest, handlers, AI-exposed subset (withholds `apply_edit`), `install_canvas_tools()` | 506 |
| `service.py` | `CanvasService` singleton wiring storage/types/workspace/exporter/adapter/managers; feature gate; isolation report | 155 |
| `schemas.py` | Re-exported event-type constants | 16 |
| `workspace.py` | `WorkspaceGateway` protocol, providers, `WorkspaceAccess` with read/write levels, dev `set_level`; Amendment 01: storage provider / lifecycle / read-only, path gate, write contract, contract error bridge, export (§17) | 1232 |

### 2.2 API and tests

| File | Content |
| --- | --- |
| `app/api/canvas.py` | `/api/v1/canvas` router (25 endpoints, incl. `export-to-workspace`), feature-flag gate, approval body on apply, request models with `extra="forbid"`, structured errors (589 LOC) |
| `tests/conftest.py` | `canvas` / `configured` fixtures (throw-away data dir + service reset) — new file, 36 LOC |
| `tests/r2_canvas_support.py` | Scope/header/client helpers, `configure()` (feature flag + dev preview on) (77 LOC) |
| `tests/test_r2_canvas_foundation.py` | CP-01: types, manager, persistence, restart recovery, limits (11 tests) |
| `tests/test_r2_canvas_revision_patch.py` | CP-02: revisions, restore, diff, patch conflicts, offsets (7 tests) |
| `tests/test_r2_canvas_ai.py` | CP-03: generation stream, edit proposals, tools, opt-in registration (17 tests) |
| `tests/test_r2_canvas_export_security.py` | CP-05: export formats, filename/header hardening, sanitizer, isolation, limits (10 tests) |
| `tests/test_r2_canvas_correction.py` | WO-C17: C-FIX-01…06 verification (18 tests) |
| `tests/test_r2_canvas_workspace_a01.py` | R2-W Amendment 01: lifecycle, read-only, export-to-workspace, error bridge (27 tests) |

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
| POST | `/artifacts/{artifact_id}/export-to-workspace` | write the artifact (or `revision`) into a workspace root through the Amendment 01 write contract; `local_browser` answers `client_write` (§17.3) |
| POST | `/artifacts/{artifact_id}/edit-proposals` | AI edit proposal (dev adapter) (201) |
| GET | `/artifacts/{artifact_id}/edit-proposals` | list proposals (`status`, paging) |
| GET | `/edit-proposals/{proposal_id}` | one proposal (+ `is_stale`) |
| POST | `/edit-proposals/{proposal_id}/apply` | apply a proposal — requires `user_approved: true` and an interactive user scope |
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
| `forbidden` | 403 | scope override fields |
| `proposal_already_applied` | 409 | a second apply of an applied proposal (WO-C17) |
| `proposal_rejected` | 409 | apply of a rejected proposal (WO-C17) |
| `user_approval_required` | 403 | apply without a verified user approval (WO-C17 / C-FIX-06) |
| `canvas_disabled` | 503 | `AGCORE_CANVAS_ENABLED` is off, so the whole router is gated (WO-C17 / C-FIX-05) |
| `workspace_closed` | 409 | workspace lifecycle is `closed`; reopen it first (Amendment 01) |
| `read_only_workspace` | 403 | the workspace root was opened read-only, so no write (Amendment 01) |
| `path_outside_allowed_root` | 403 | path is not inside a declared allowed root (Amendment 01, passed through) |
| `symlink_escape` | 400 | a symlink component would escape the root (Amendment 01, passed through) |
| `root_unavailable` | 409 | the root folder is missing or unreadable (Amendment 01, passed through) |
| `provider_mismatch` | 400 | the provider does not implement the operation (e.g. a v1-only provider asked to write) |
| `content_conflict` | 409 | `expected_content_hash` no longer matches the workspace file |
| `source_delete_not_permitted` | 403 | disconnecting may not delete real files (Amendment 01, passed through) |
| `metadata_overlap` / `watch_unavailable` / `index_not_ready` | 400 / 409 / 409 | further contract codes Canvas carries unchanged when a provider answers them |
| `workspace_not_found` | 404 | workspace is detached (`lifecycle_state: deleted`) |
| `file_exists` / `file_not_found` | 409 / 404 | workspace file names/ids reported through Canvas |
| `invalid_path` | 400 | the shared relative-path gate rejected the path |

### 5.2 Request rules

* `extra="forbid"` on every request model: `tenant_id`, `owner_id`, `service_id`, `role` and any
  other unknown key are rejected with 422 before the handler runs.
* `patch` requires `base_revision` and, for region operations, `selection` /
  `expected_original_text`; the write is compare-and-commit inside one transaction.
* `artifact create` accepts `creation_source` (`user|ai|import|tool`) and an optional
  `idempotency_key`; a duplicate key returns the original artifact (`duplicate: true`).
* `POST /edit-proposals/{id}/apply` requires `user_approved: true` **and** an interactive principal
  (`user`/`owner`/`admin`/`admiral`). Without it the answer is `403 user_approval_required`; a
  second apply is `409 proposal_already_applied`, a rejected one `409 proposal_rejected`.
* Every route is behind `AGCORE_CANVAS_ENABLED`; with the flag off the answer is
  `503 canvas_disabled` (authentication is still evaluated first).

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
  constructed in production, seeded with `ws_dev00000001` (Amendment 01 setup: `set_level`,
  `set_lifecycle_state`, `set_read_only`, `set_storage_provider`, `seed_file`).
* `UnavailableWorkspaceProvider` — fail-closed default (`workspace_unavailable`).

### Amendment 01 (`r2-w/v1+a01`) on top of the above

Canvas consumes only the **additive** members of the revision, so a v1 provider keeps working:

| Amendment 01 item | Canvas use |
| --- | --- |
| `WorkspaceIdentity.lifecycle_state` (`open` / `closed` / `deleted`) | every workspace-linked read/write/export: `closed` → `409 workspace_closed`, `deleted` → `404 workspace_not_found`; detach (`DELETE /links/workspace`) stays owner-allowed |
| `WorkspaceIdentity.root.read_only` | `403 read_only_workspace` on every write requirement |
| `WorkspaceIdentity.root.storage_provider` | `server_managed` / `server_mounted_folder` / `local_browser`; reported in `/capabilities` and on the export response |
| `WorkspaceIdentity.root.server_side_path_access` | `false` for `local_browser`: no server path is echoed and no write is attempted server-side |
| `write_file` (`PUT /workspaces/{id}/file`) | the only Amendment 01 operation Canvas calls — export to workspace (§17.3) |
| §A1.10 error codes | carried through Canvas' own envelope with the contract's status (§17.4) |

Still out of scope for R2 Canvas: workspace file listing, search/embedding/index, folder open/new/
close/reopen/disconnect/watch (those are Workspace endpoints), and any workspace file storage —
Canvas only *sends* bytes through the contract and never touches a real path.

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
| `canvas.apply_edit` | no | `proposal_id` | **withheld from the AI-exposed set**; a tool call is refused with `403 user_approval_required` (a revision is only written from the user-facing apply endpoint) |
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
   Without it the registry contains exactly the eight R1.3 builtin tools. Registration adds the
   **seven AI-exposed tools**; `canvas.apply_edit` is withheld until R1.3 defines its approval
   channel (`install_canvas_tools(registry, ai_exposed_only=False)` is the admin-only variant).
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
| Schema version | `2` in `canvas_meta`; created idempotently with `CREATE TABLE IF NOT EXISTS` (v2 adds `canvas_revision_prunes`) |
| Soft delete | `deleted_at` set; hidden from reads/lists; data retained |
| Purge | deletes artifact/revision/proposal/event/prune rows; content-addressed blobs stay (shared, no GC in R2) |
| Limits | content ≤ 512 KiB, revisions ≤ 100 (pruned at the cap), list ≤ 100/page, diff ≤ 2000 lines, context ≤ 3000 tokens (all `AGCORE_CANVAS_*` overridable) |
| Restart | nothing cached in memory; a new process on the same DB + blob root returns identical data (covered by a test) |

Environment variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `AGCORE_ENV` | `development` | `production` disables dev adapters and forces the runtime/unavailable providers |
| `AGCORE_CANVAS_ENABLED` | **`false`** | engine on/off; off ⇒ `503 canvas_disabled` on every route and the tool layer refuses |
| `AGCORE_CANVAS_DEV_PREVIEW` | `false` | separate switch for the dev-only surface (dev workspace provider, dev edit adapter, simulated generation stream); forced off in production |
| `AGCORE_CANVAS_DATA_DIR` | `<repo>/data/canvas` | DB + blob root |
| `AGCORE_CANVAS_DB_PATH` | `<data_dir>/canvas.db` | explicit DB file |
| `AGCORE_CANVAS_TOOLS_ENABLED` | unset (off) | register the Canvas tools in the tool registry (seven AI-exposed; `canvas.apply_edit` withheld) |
| `AGCORE_CANVAS_WORKSPACE_PROVIDER` | `dev` | `dev` \| `workspace` \| `unavailable` |
| `AGCORE_CANVAS_AI_ADAPTER` | `dev` | `dev` \| `runtime` |
| `AGCORE_CANVAS_DEV_ADAPTERS` | `true` | second gate on the dev adapters; can only shrink the surface, forced off in production |
| `AGCORE_CANVAS_MAX_CONTENT_BYTES` | `524288` | per-artifact content limit |
| `AGCORE_CANVAS_MAX_REVISIONS` | `100` | revision cap per artifact (enforced by pruning, never by blocking edits) |
| `AGCORE_CANVAS_RETENTION_MIN_REVISIONS` | `20` | newest revisions always kept (clamped to `max_revisions - 1`) |
| `AGCORE_CANVAS_MAX_LIST_SCAN` | `1000` | rows scanned when list must filter revoked workspace artifacts |
| `AGCORE_CANVAS_MAX_TITLE_CHARS` | `200` | title limit |
| `AGCORE_CANVAS_MAX_LIST_LIMIT` | `100` | page size ceiling |
| `AGCORE_CANVAS_DEFAULT_PAGE_LIMIT` | `25` | default page size |
| `AGCORE_CANVAS_MAX_DIFF_LINES` | `2000` | diff line budget |
| `AGCORE_CANVAS_CONTEXT_TOKENS` | `3000` | AI edit context budget |

WO-C17 push: `c615874` … `c25a9a5` were pushed to `origin/feat/r2-canvas` on 2026-11-27
(`git ls-remote origin feat/r2-canvas` → `c25a9a5`, `main` still `0d2047f`). No force push, no
merge, no pull request.

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
export AGCORE_CANVAS_ENABLED=true            # WO-C17: the feature is off by default
export AGCORE_CANVAS_DEV_PREVIEW=true        # dev provider / dev edit adapter / fake stream
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
`environment=development`, five enabled types and eight manifest tools, artifact creation returned
`art_…` with `current_revision: 1`. With `AGCORE_CANVAS_TOOLS_ENABLED=1`, `/api/v1/agent/agents`
listed all eight `canvas.*` tools; with the flag unset it listed none. (WO-C17 changes the exposed
count to seven — `canvas.apply_edit` is withheld — and requires `AGCORE_CANVAS_ENABLED=true`
before any Canvas route answers; see §11.1 for the WO-C17 smoke run.) Stop the server with
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

### 11.1 WO-C17 verification run

```bash
cd /workspace/repos/AGPilot0005
/workspace/.venv-r2canvas/bin/pytest \
  tests/test_r2_canvas_foundation.py tests/test_r2_canvas_revision_patch.py \
  tests/test_r2_canvas_ai.py tests/test_r2_canvas_export_security.py \
  tests/test_r2_canvas_correction.py -q
```

Result: **63 passed** (45 pre-existing Canvas tests + 18 new WO-C17 tests), Python 3.12.16,
2026-11-27. The 219-test baseline was not re-run (its PASS evidence is accepted per WO-C17); the
R1.3 modules that share the tool registry (`tests/test_tools.py`, `tests/test_beta_baseline.py`,
`tests/test_runtime_and_api.py`, 23 tests) were re-run because `install_canvas_tools()` changed and
all pass.

Smoke run over HTTP (2026-11-27, `uvicorn` on 127.0.0.1):

| Check | Result |
| --- | --- |
| flag off (default): `GET /canvas/capabilities` | `503 canvas_disabled`, `/api/v1/health` still `200` |
| flag + dev preview on: `GET /canvas/capabilities` | `enabled: true`, `dev_preview: true`, retention and approval policy reported |
| create → propose → apply without `user_approved` | `403 user_approval_required` |
| apply with `user_approved: true` | `200`, revision 2, `approval.source = "user_action"` |
| second apply | `409 proposal_already_applied` |
| `/agent/agents` with `AGCORE_CANVAS_TOOLS_ENABLED=1` | 7 `canvas.*` tools, `canvas.apply_edit` absent |

### 11.2 R2-W Amendment 01 verification run

```bash
cd /workspace/repos/AGPilot0005
/workspace/.venv-r2canvas/bin/pytest tests/test_r2_canvas_*.py -q
```

Result: **90 passed, 0 failed** (Python 3.12.16, 2026-11-27) — the 63 pre-existing Canvas tests
(unchanged, v1 regression 0) plus the 27 new Amendment 01 tests. The 219-test baseline and the R1.3
86-test run were not repeated (their PASS evidence is accepted, per the task).

| Area | Assertion |
| --- | --- |
| `closed` lifecycle | `GET artifact` / `export` / `preview` / `revisions` / `diff` → `409 workspace_closed` (`retryable: true`, `context.recovery` names reopen); patch / rename → 409; export-to-workspace → 409; rows kept; detach `DELETE /links/workspace` → 200; reopen restores reads |
| `deleted` lifecycle | `404 workspace_not_found`, detach still allowed |
| `read_only` root | create-into-workspace / patch / export-to-workspace → `403 read_only_workspace`; reads → 200; nothing written |
| export-to-workspace (mounted folder) | 200 with `storage_provider: server_mounted_folder`, `content_origin: external`, `content_hash` = sha256 of the exported bytes, `workspace_file.relative_path`, `workspace_file_revision` 1→2, default path from the sanitized title |
| export-to-workspace (`local_browser`) | 200 with `write_mode: client_write`, `server_side_path_access: false`, `workspace_root_path: null`, `client_write.client_handle_id`, `written_bytes: 0`, no file stored server-side |
| path gate | 7 rejected forms (absolute, `..`, `.`, `//`, drive letter, backslash, NUL) → `400 invalid_path`, artifact untouched |
| write conflicts | `expected_content_hash` mismatch → `409 content_conflict` (file unchanged); `overwrite:false` on an existing path → `409 file_exists` |
| v1-only provider | access/link/reference unchanged (200, reference registered); export → `400 provider_mismatch` |
| runtime identity | `WorkspaceIdentity.lifecycle_state` / `root` are consumed: closed → 409, `read_only` → 403, mounted root path reported, `write_file` receives `path` / `content` / `create_parents` / `overwrite` |
| error bridge | all 22 contract codes map to their own Canvas code and contract status (13 of them end-to-end through the endpoint, all 22 at unit level); an unknown engine failure → `503 workspace_unavailable` (not a client error) |
| `/capabilities` | `workspace.contract.revision == "r2-w/v1+a01"`, amendment `A01`, `a01_operations: [write_file]`, lifecycle enforcement report |

Smoke run over HTTP (`uvicorn` on 127.0.0.1, dev workspace provider, 2026-11-27):

| Check | Result |
| --- | --- |
| `GET /api/v1/health` with Canvas on | `200` |
| `GET /canvas/capabilities` | `revision: r2-w/v1+a01`, `a01_operations: ["write_file"]` |
| export to a mounted-folder workspace (`smoke/report.md`) | `200`, `server_mounted_folder`, `server_write`, `sha256:48f1e5…`, file present in the root |
| export with `../escape.md` | `400 invalid_path` |
| export to a `local_browser` root | `200`, `client_write`, `client_handle_id: h-1`, `workspace_root_path: null` |
| list with `?workspace_id` after close | `409 workspace_closed` |
| export after close | `409 workspace_closed` |

## 12. Not implemented / not verified

| Item | State |
| --- | --- |
| **Real LLM artifact generation** | **Not verified.** `POST /canvas/generations` is a dev contract that chunks caller-supplied content (`simulate: complete \| abort`); no model is called and `ai_integration_verified` is `false`. |
| **Real LLM AI edit proposals** | **Not verified.** The dev adapter needs `scripted_replacement` or a `dev_rule`; otherwise 503 `ai_adapter_unavailable`. No R1.3 runtime adapter exists yet (`AGCORE_CANVAS_AI_ADAPTER=runtime` → unavailable). |
| Canvas frontend (CP-04) | Out of this task. `AGPilot0011` was not accessible to the GitHub App (404 on installation scope); no UI file was touched. |
| Office export (DOCX/PDF/XLSX/PPTX) | Contract declared in `OFFICE_EXPORT_CONTRACT`, refused with `export_format_unsupported`. No conversion engine. |
| Export into a workspace file | **Implemented against the contract** (§17): `POST /artifacts/{id}/export-to-workspace` via `write_file`. Verified against the dev provider (server-managed, mounted-folder and `local_browser` + `client_write`). Not yet exercised against the live engine, because `feat/r2-workspace-rag` is not merged into this branch. |
| Live Workspace provider | `RuntimeWorkspaceProvider` is implemented but never exercised against a merged `feat/r2-workspace-rag`; only the dev provider is tested. The WO-C17 revocation tests use the dev provider's `set_level()`, so the *live* revocation path (real provider answering `none`) is still unverified. |
| Generation session expiry / storage quota | P2 backlog — see §16.8 (C-FIX-07). |
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

3. **Tool exposure to the model** — after registration, add the seven AI-exposed names to the
   service's tool profile (`app/tools/policy.py` `DEFAULT_POLICY["profiles"][…]["tools"]` or the runtime
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

---

## 16. WO-C17 — Canvas Backend Correction

Scope of this work order: `C-FIX-01` … `C-FIX-06` implemented and verified, `C-FIX-07` registered as
backlog. The 219-test evidence from CP-06 is taken as-is; no architecture was redesigned, no R1.3
runtime file, no Workspace/RAG-owned file, no `main` merge and no deployment is part of it.

### 16.1 What changed

| Item | Defect | Correction | Evidence |
| --- | --- | --- | --- |
| **C-FIX-01** workspace write permission | A `read` grant was enough to create an artifact *into* a workspace and to link one | `WorkspaceAccess` splits `read` (`read\|write\|admin`) from `write` (`write\|admin`); create-into-workspace, link, rename, delete, patch, restore and proposal writes require `write` | `test_cfix01_*` (403 with `required_level: "write"`, no artifact / no link written) |
| **C-FIX-02** access revocation | A workspace grant checked once at link time was never re-checked; revoked users kept reading/editing | `assert_workspace_level()` is called on every path that touches a workspace-linked artifact, with the *current* level; `list` drops revoked artifacts and reports the exclusions | `test_cfix02_*` (14 paths → 403, list exclusion, 503 when the provider is down, detach still allowed) |
| **C-FIX-03** proposal transaction | Revision insert and proposal status were two transactions, so a duplicate/concurrent apply could double-write or the proposal could stay `proposed` after a revision landed | `SqliteCanvasStorage.apply_proposal()` does status check → patch → status flip in one `BEGIN IMMEDIATE`; 409 for already-applied / rejected; stale committed then raised | `test_cfix03_*` (concurrent apply → one revision, rollback on storage failure, stale status persisted) |
| **C-FIX-04** revision retention | At `max_revisions` every edit failed with `409 revision_limit_reached` — an autosave history bricked the artifact | At the cap, the oldest unprotected revision is pruned inside the same transaction and recorded in `canvas_revision_prunes`; current + newest window + proposal-referenced revisions are kept, baseline dropped last | `test_cfix04_*` (12 autosaves on a cap of 4, proposal-referenced row survives) |
| **C-FIX-05** feature isolation | Canvas merged into an operating service with the API live and dev adapters on by default | `AGCORE_CANVAS_ENABLED` defaults **false** (503 `canvas_disabled`), `AGCORE_CANVAS_DEV_PREVIEW` is a separate off-by-default switch, production forces dev adapters off | `test_cfix05_*`, smoke run |
| **C-FIX-06** user approval boundary | `canvas.apply_edit` (a model tool call) applied a proposal directly | Apply needs a `UserApproval` built from an interactive scope **and** an explicit `user_approved: true`; `canvas.apply_edit` is withheld from the AI-exposed set and refuses with `403 user_approval_required` | `test_cfix06_*` |
| **C-FIX-07** generation session expiry / storage quota | Not implemented (see 16.8) | Registered as P2 backlog | this section |

### 16.2 Workspace permission contract (Canvas side)

| Operation | Required workspace level |
| --- | --- |
| `list`, `get`, content/revision read, `diff`, `preview`, `export`, proposal get/list | `read` (read · write · admin) |
| create with `workspace_id`, link to a workspace, rename, delete, patch, patch preview, restore, proposal create/apply/reject | `write` (write · admin) |
| `DELETE /links/workspace` (detach) | none — owner scope only, the documented way out of a revoked workspace |
| Access provider error | `503 workspace_unavailable` (never a stale allow) |

A revoked level makes the artifact invisible and immutable immediately; it is **not** deleted, and
the owner can detach it to regain local access. Artifact ids from another scope still answer `404`,
so the 403 only ever describes an artifact the caller already owns.

### 16.3 Proposal apply — state and concurrency

```
proposed --apply(user approval)--> applied   (one transaction: revision + status)
proposed --rejected-------------> rejected  (409 on any later apply)
proposed --base revision moved--> stale     (committed; 409 proposal_stale)
applied  --apply----------------> 409 proposal_already_applied
```

The apply request carries `user_approved: true`; the response echoes the recorded approval
(`approval_id`, `approved_by`, `source: "user_action"`) and the `artifact.edit.applied` event data
carries the same object, so the audit trail shows *who* approved, not just that a revision appeared.

### 16.4 Revision retention

* Policy: `prune_oldest_unprotected`, evaluated inside the write transaction.
* Always kept: the current revision, the newest `min_retained_revisions` (default 20, clamped to
  `max_revisions - 1`), revisions referenced by a proposal (`applied_revision`, or `base_revision`
  while the proposal is `proposed`/`stale`).
* Dropped first: oldest rows outside that window — the baseline (revision 1) is only dropped when it
  is the last candidate.
* Each removal is recorded in `canvas_revision_prunes` (revision, content hash, timestamp, reason
  `retention_cap` / `retention_cap_forced`) and surfaced as
  `limits.revision_retention.pruned_total` with `editing_blocked_at_limit: false`.
* `409 revision_limit_reached` now exists only for the pathological case where every stored revision
  is protected; normal editing and autosave never hit it.
* Blob garbage collection is still out of scope — pruned blob files remain (16.8).

### 16.5 Feature isolation and Dev Preview settings

| Flag | Default | Effect |
| --- | --- | --- |
| `AGCORE_CANVAS_ENABLED` | `false` | Whole router + service + tools answer `503 canvas_disabled` / refuse. Merging the branch activates nothing. |
| `AGCORE_CANVAS_DEV_PREVIEW` | `false` | Dev workspace provider, deterministic dev edit adapter, simulated `POST /generations`. Forced `false` under `AGCORE_ENV=production`. |
| `AGCORE_CANVAS_DEV_ADAPTERS` | (implied true) | Second gate on the dev adapters; can only shrink the surface. |
| `AGCORE_CANVAS_TOOLS_ENABLED` | unset (off) | Tool registration, independent of the API flag. |
| `AGCORE_CANVAS_WORKSPACE_PROVIDER` | `dev` | `dev` \| `workspace` \| `unavailable`; production rewrites `dev` → `workspace`. |
| `AGCORE_CANVAS_AI_ADAPTER` | `dev` | `dev` \| `runtime`; production rewrites `dev` → `runtime`. |

Activation sequence for a preview: `AGCORE_CANVAS_ENABLED=true` **and**
`AGCORE_CANVAS_DEV_PREVIEW=true` (plus `AGCORE_CANVAS_TOOLS_ENABLED=1` only if the model should see
the tools). `/capabilities.feature_isolation` reports all of this, including
`auto_activation: false`, so a deployment can be audited without guessing.

### 16.6 User approval boundary

* Approvable roles: `user`, `owner`, `admin`, `admiral` (a human principal) **with**
  `user_approved: true` in the request body.
* Refused: `service`, `tool`, `ai`, `assistant`, `automation`, `system`, `guest` — even when they
  hold the owner scope, and even with the flag set.
* `canvas.apply_edit` never receives an approval object: the tool dispatcher has no approval channel,
  so the call returns `403 user_approval_required` and no revision is written. The tool is also left
  out of `AI_EXPOSED_TOOL_NAMES`, so with `AGCORE_CANVAS_TOOLS_ENABLED=1` the model sees seven tools,
  not eight. `manifest().approval_boundary` states this for the frontend and the tool admin UI.
* Until R1.3 defines its own approval channel, the model can propose (`canvas.propose_edit`) but the
  user applies from the UI — which is the intended product behaviour, now enforced by the backend.

### 16.7 New / changed error codes

| Code | HTTP | Raised when |
| --- | --- | --- |
| `canvas_disabled` | 503 | `AGCORE_CANVAS_ENABLED` is off |
| `user_approval_required` | 403 | apply without a verified user approval (or a machine role) |
| `proposal_already_applied` | 409 | second apply of an applied proposal |
| `proposal_rejected` | 409 | apply of a rejected proposal |
| `workspace_access_denied` | 403 | current level is below the required level (`context.required_level`, `context.level`) |
| `workspace_unavailable` | 503 | the workspace provider is down or unconfigured |

### 16.8 C-FIX-07 — Backlog (P2, deliberately not implemented)

| Item | Priority | Detail | Why deferred |
| --- | --- | --- | --- |
| Generation session expiry | P2 | `GenerationService._sessions` keeps every completed/aborted session in process memory for idempotent replay; there is no TTL, so a long-lived process accumulates session frames (each holds the accumulated chunks). Need: idle TTL + max sessions + eviction that keeps the idempotency key result readable from storage instead of memory. | Requires a decision on replay semantics after eviction (replay from `canvas_events` vs 409 `session_expired`), which belongs to the R1.3 generation integration. |
| Generation storage quota | P2 | No per-scope/per-artifact generation quota or total-bytes accounting; only `max_content_bytes` per artifact and `max_revisions` per artifact exist. Need: scope-level byte/piece quota, a rejection code, and an operator-visible usage report. | Quota limits are a product/ops decision (who is limited, what happens at the cap) and would otherwise bake a guess into the store. |
| Blob garbage collection | P2 | Pruned revisions and purged artifacts leave content-addressed blobs on disk (`data/canvas/blobs`) because blobs are shared. Need: reference scan + GC job. | Safe GC needs a full-store pass and a scheduling owner; deleting blobs from a request path would risk another artifact's content. |
| List scan ceiling | P3 | A scope with many workspace-linked artifacts and revoked access scans up to `AGCORE_CANVAS_MAX_LIST_SCAN` rows per page and reports `total_exact: false` beyond that. | Acceptable at current scale; a proper fix is a per-scope workspace visibility index owned by the Workspace side. |

### 16.9 Changed files (WO-C17)

| File | Change |
| --- | --- |
| `app/canvas/approval.py` | new — approval contract + policy |
| `app/canvas/config.py` | feature flag default off, dev preview, retention window, activation report |
| `app/canvas/workspace.py` | read/write levels, `set_level` for the dev provider, write-gated reference registration |
| `app/canvas/manager.py` | `owned_artifact`, workspace-aware `visible_artifact`, write-gated create/link/rename/delete/patch, list filtering, retention report |
| `app/canvas/revisions.py` | workspace gateway in revision/patch managers, read/write requirements |
| `app/canvas/editing.py` | workspace-aware proposal lifecycle, approval-gated atomic apply |
| `app/canvas/storage.py` | schema v2 (`canvas_revision_prunes`), `_apply_patch_conn`, `apply_proposal`, retention pruning, `count_artifacts` |
| `app/canvas/events.py` | dev-preview gate for generation, fail-fast workspace write check at stream start |
| `app/canvas/service.py` | feature gate, wiring, capabilities (`feature_isolation`, `revision_retention`, `approval`) |
| `app/canvas/tools.py` | approval gate on `canvas.apply_edit`, AI-exposed subset, manifest boundary |
| `app/canvas/errors.py`, `models.py` | new codes, page exclusion fields |
| `app/api/canvas.py` | `require_canvas_enabled` dependency, approval body on apply |
| `tests/r2_canvas_support.py`, `tests/test_r2_canvas_{ai,foundation}.py`, `tests/test_r2_canvas_correction.py` | flag-aware fixtures, corrected expectations, 18 new tests |

No file under `app/runtime/`, `app/runtime/policy/`, `app/gateway/`, `app/adapters/`,
`app/workspace/` or `app/tools/{registry,policy}.py` is modified by WO-C17.

---

## 17. R2-W Amendment 01 — Folder-Based Workspace (`r2-w/v1+a01`)

Reference (read-only, Workspace/RAG-owned): `docs/r2/WORKSPACE_CONTRACT.md` §A1.1–§A1.11,
`app/workspace/contracts.py`, `app/workspace/mock.py` on `origin/feat/r2-workspace-rag` (`907482c`).
No file under `app/workspace/**` was created or modified — Canvas keeps a mirror of the few types it
must understand (`WorkspaceWriteRequest`, `ClientWriteDirective`, `WorkspaceWriteResult`,
`validate_relative_path`) because that package is not importable from `feat/r2-canvas`.

### 17.1 What Canvas now understands

| Contract item | Canvas consumption |
| --- | --- |
| `WORKSPACE_CONTRACT_VERSION = "r2-w/v1"` | unchanged; every v1 field/operation keeps its meaning |
| `WORKSPACE_CONTRACT_AMENDMENT = "A01"`, `WORKSPACE_CONTRACT_REVISION = "r2-w/v1+a01"` | exported as constants, reported by `/capabilities` (`workspace.contract`) and on every export response |
| `WorkspaceStorageProvider` (`server_managed` / `server_mounted_folder` / `local_browser`) | carried on `WorkspaceAccess.storage_provider`, chosen by the target workspace, reported in the export result |
| `WorkspaceLifecycleState` (`open` / `closed` / `deleted`) | enforced on every workspace-linked artifact path (§17.2) |
| `WorkspaceRootInfo.read_only` | `403 read_only_workspace` for a write requirement |
| `WorkspaceRootInfo.server_side_path_access` | `false` for `local_browser`: Canvas never builds, resolves or echoes a server path; the export response carries `workspace_root_path: null` |
| `WorkspaceWriteRequest` / `WorkspaceWriteResult` / `ClientWriteDirective` | request is built by `build_write_request()` (contract model when importable, identical mirror otherwise) and the result is normalised so every contract field is always present |
| `validate_relative_path()` | mirrored byte-for-byte in `app/canvas/workspace.py`, raised as `400 invalid_path` |
| §A1.10 error codes | mapped to Canvas codes with the contract status (§17.4) |

The v1 provider operations are untouched: `check_workspace_access`, `register_artifact_reference`
(and `get_workspace`, used only to *enrich* the access answer with lifecycle/root). A provider that
does not implement `get_workspace` (a v1-only provider) is treated as an open, server-managed
workspace — exactly the v1 behaviour.

### 17.2 Lifecycle and read-only enforcement

`assert_workspace_level()` — the single choke point every workspace-linked Canvas path goes through
— now runs `enforce_workspace_state()` after the permission level is satisfied. Order matters: a
caller without access gets `403 workspace_access_denied` and learns nothing about the lifecycle.

| Workspace state | Reads (`read`) | Writes (`write`) | Export to workspace |
| --- | --- | --- | --- |
| `open`, writable | allowed | allowed | allowed |
| `open`, `read_only: true` | allowed | `403 read_only_workspace` | `403 read_only_workspace` |
| `closed` | `409 workspace_closed` | `409 workspace_closed` | `409 workspace_closed` |
| `deleted` (detached) | `404 workspace_not_found` | `404 workspace_not_found` | `404 workspace_not_found` |

Paths covered by the read/write table: get, list (explicit `workspace_id` filter), export, preview,
revision history/get, diff, patches (apply/preview), restore, rename, delete, link, proposal
create/apply/reject, generation start, and export-to-workspace.

Deliberate exception (C-FIX-02, unchanged): `DELETE /api/v1/canvas/artifacts/{id}/links/workspace`
uses owner scope only — detaching is the documented way out of a closed or detached workspace, it
neither reads nor writes workspace content, and it is the only way a user can recover their
artifact after the workspace went away.

### 17.3 Export to Workspace

`POST /api/v1/canvas/artifacts/{artifact_id}/export-to-workspace`

```json
{
  "workspace_id": "ws_7f3c1a9e2b04",
  "path": "reports/summary.md",
  "revision": 2,
  "format": "md",
  "expected_content_hash": null,
  "create_parents": true,
  "overwrite": true,
  "mime_type": "text/markdown"
}
```

The bytes come from the **same export provider the download endpoint uses**, so the written file is
exactly what the user would have downloaded (`format` defaults to the type's default format; the
`artifact_revision` written is echoed). The target `path` is validated with the shared path gate;
when omitted, the export filename derived from the sanitized title is used.

Service path: `ArtifactManager.export_to_workspace()` → `read_content(required="read")` →
`assert_workspace_level(required="write")` (closed/read-only refused here, before any byte is
produced) → `exporter.export()` → `write_workspace_file()` → provider `write_file()`.

Response `200`:

```json
{
  "artifact_id": "art_8ce43de6a789",
  "artifact_revision": 1,
  "artifact_type": "markdown",
  "format": "md",
  "extension": ".md",
  "media_type": "text/markdown; charset=utf-8",
  "workspace_id": "ws_7f3c1a9e2b04",
  "path": "reports/summary.md",
  "storage_provider": "server_mounted_folder",
  "content_origin": "external",
  "client_write": null,
  "write_mode": "server_write",
  "server_side_path_access": true,
  "workspace_root_path": "/srv/hd2lab/workspaces/lab-docs",
  "workspace_file": { "file_id": "wf_…", "relative_path": "reports/summary.md", "revision": 1, "content_hash": "sha256:…", "content_origin": "external" },
  "workspace_file_revision": 1,
  "content_hash": "sha256:…",
  "written_bytes": 8123,
  "export_warnings": [],
  "content_metrics": { "codepoints": 8120, "utf16_units": 8123, "utf8_bytes": 8123 },
  "workspace_access": { "level": "write", "lifecycle_state": "open", "read_only": false, "storage_provider": "server_mounted_folder", "server_side_path_access": true, "…": "…" },
  "contract_revision": "r2-w/v1+a01"
}
```

Rules that hold on every provider:

* Canvas writes **nothing** itself: the workspace engine owns the file, the atomic replace, the
  revision row and the index job. Canvas only builds `{path, content | content_base64, mime_type,
  expected_content_hash, create_parents, overwrite}`.
* Text content travels as `content`; bytes that are not valid UTF-8 (or contain NUL) travel as
  `content_base64`.
* `expected_content_hash` mismatch → `409 content_conflict` and the file is unchanged, so two agents
  writing the same path cannot silently overwrite each other.
* `overwrite: false` on an existing path → `409 file_exists`.
* A provider that does not implement `write_file` (v1-only) → `400 provider_mismatch`.

### 17.4 Contract error → Canvas error bridge

`translate_workspace_error()` reads `detail.code` / `exc.code` from whatever the provider raises
(`HTTPException` with the contract envelope, the contract's own error type, or a plain exception) and
keeps the meaning. The map covers the whole contract table:

| Contract code | Canvas code | HTTP |
| --- | --- | --- |
| `workspace_not_found` | `workspace_not_found` | 404 |
| `workspace_closed` | `workspace_closed` | 409 |
| `read_only_workspace` | `read_only_workspace` | 403 |
| `path_outside_allowed_root` | `path_outside_allowed_root` | 403 |
| `symlink_escape` | `symlink_escape` | 400 |
| `root_unavailable` | `root_unavailable` | 409 |
| `provider_mismatch` | `provider_mismatch` | 400 |
| `content_conflict` | `content_conflict` | 409 |
| `source_delete_not_permitted` | `source_delete_not_permitted` | 403 |
| `metadata_overlap` | `metadata_overlap` | 400 |
| `watch_unavailable` | `watch_unavailable` | 409 |
| `file_not_found` | `file_not_found` | 404 |
| `file_exists` | `file_exists` | 409 |
| `revision_conflict` | `revision_conflict` | 409 |
| `quota_exceeded`, `file_too_large` | `content_too_large` | 413 |
| `unsupported_media_type` | `unsupported_artifact_type` | 415 |
| `invalid_path` | `invalid_path` | 400 |
| `invalid_request` | `invalid_request` | 422 (Canvas schema semantics) |
| `index_not_ready` | `index_not_ready` | 409 |
| `embedding_unavailable` | `workspace_unavailable` | 503 |
| `forbidden` | `forbidden` | 403 |

An **unknown** provider failure stays `503 workspace_unavailable` (`retryable: true`) with the
provider exception class in `context.reason` — an outage is never reported as a client error.
Every mapped error keeps the contract's `message`, `retryable` and `context`, plus
`context.workspace_id`, `context.operation` and `context.provider_code`.

### 17.5 `local_browser` (client write)

For a browser-owned root the server has no path access. Canvas reflects that everywhere:

* `WorkspaceAccess.server_side_path_access = false`, `root_display_path = ""` — even if a provider
  sends a display path, Canvas drops it instead of echoing a client machine path;
* the export answers `200` with `write_mode: "client_write"`, `written_bytes: 0`, no
  `workspace_file`, and
  `client_write: {required: true, client_handle_id, relative_path, reason:
  "local_browser_no_server_path_access"}`;
* the browser then performs the write through its file handle (frontend responsibility) — Canvas
  never retries server-side.

### 17.6 v1 backward compatibility

| v1 behaviour | Evidence |
| --- | --- |
| 63 pre-existing Canvas tests | unchanged and still passing |
| `read`/`write` separation, revocation re-check, detach escape | `test_r2_canvas_correction.py` unchanged |
| A provider without `get_workspace` / `write_file` | access, create-into-workspace, link and reference registration work exactly as before (`test_a01_v1_only_provider_keeps_working_but_cannot_write`); only the new export answers `provider_mismatch` |
| `WORKSPACE_CONTRACT_VERSION = "r2-w/v1"` | still the consumed version; the amendment is tracked separately (`+a01`) |

### 17.7 Changed files (Amendment 01)

| File | Change |
| --- | --- |
| `app/canvas/workspace.py` | contract revision constants, `WorkspaceStorageProvider` / `WorkspaceLifecycleState` / `ContentOrigin`, `WorkspaceRootInfo`, write-contract mirrors, `validate_relative_path`, `enforce_workspace_state`, `apply_workspace_identity`, `WORKSPACE_ERROR_CODES` + `translate_workspace_error`, `write_workspace_file`, dev-provider Amendment 01 state and `write_file`, runtime `write_file` + identity enrichment, `workspace_contract_report()` |
| `app/canvas/manager.py` | `assert_workspace_level` enforces lifecycle/read-only; `export_to_workspace()` |
| `app/canvas/errors.py` | 15 new codes (8 Amendment 01 + 7 pass-through v1/file codes) with the contract status |
| `app/canvas/service.py` | `/capabilities` reports the contract, the Amendment 01 operations and the lifecycle enforcement |
| `app/api/canvas.py` | `POST /artifacts/{artifact_id}/export-to-workspace` + `ExportToWorkspaceBody` (`extra="forbid"`) |
| `tests/test_r2_canvas_workspace_a01.py` | 27 tests (§11.2) |

No file under `app/workspace/`, `app/runtime/`, `app/gateway/`, `app/adapters/`,
`app/tools/{registry,policy}.py`, `app/schemas/events.py` or `app/main.py` is modified by this
amendment.

### 17.8 Not verified

| Item | State |
| --- | --- |
| Export to a **live** workspace engine | Not verified: `feat/r2-workspace-rag` is not merged into this branch, so `write_file`, `get_workspace` and the real error envelopes were exercised only through the dev provider and a contract-shaped fake provider. |
| Real-file write / atomic replace / index enqueue | Owned by the Workspace engine; Canvas cannot observe it. The dev provider stores bytes in memory only. |
| Browser handle write (`local_browser`) | The directive is returned and covered by tests; performing it is frontend work (`AGPilot0011` CP-04 / the frontend Amendment 01 task). |
| Reopen, watch, sync, folder open/new, disconnect | Workspace endpoints; Canvas calls none of them. `source_delete_not_permitted` is only carried through the error bridge. |
| Office formats into a workspace | Still `export_format_unsupported` (no conversion engine). |

### 17.9 Integration order

1. Merge `feat/r2-workspace-rag` (`907482c`) first; then merge `feat/r2-canvas`.
2. Set `AGCORE_CANVAS_WORKSPACE_PROVIDER=workspace` and `register_runtime_provider_factory(lambda:
   WorkspaceService(...))`; no Canvas code change is needed — `RuntimeWorkspaceProvider` binds
   `write_file` / `get_workspace` by attribute.
3. Re-run §11.2 against the live engine and confirm: closed → 409, read-only → 403, mounted-folder
   export writes a real file, `local_browser` returns `client_write`, `expected_content_hash`
   conflict → 409.
4. Frontend: consume `write_mode` / `client_write` / the Amendment 01 error codes (sibling task on
   `AGPilot0011`).
