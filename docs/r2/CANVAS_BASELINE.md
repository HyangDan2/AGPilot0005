# AGCore R2 — Canvas Baseline & Isolation Record (CP-00 / WO-C01)

Recorded 2026-11-27 (KST) by the Canvas Engineer (피클 / 향단이).
Branch: `feat/r2-canvas`. No `main` merge, no deployment, no operating server or DB change.

---

## 1. Repository baselines

| Repository | Role | Baseline SHA | State | Local branch |
| --- | --- | --- | --- | --- |
| `HyangDan2/AGPilot0005` | AGCore backend — Canvas engine | `0d2047f09a67f4cfd44f8c5399523eab3eac2172` (`tag: v1.2.0`, `docs(release): finalize CHANGELOG for v1.2.0`) | public, clean, `main` == `v1.2.0` | `feat/r2-canvas` (branched from `0d2047f`) |
| `HyangDan2/AGPilot0011` | HD2AI frontend (Canvas UI) | `b4913d0d39f8ca223f48bb7b5b7fbd99f6126ebb` (`main`) | private; readable over git for this session, **not** writable by the GitHub App (404 on installation scope) | not checked out — out of scope for this task |

Baseline test result before any Canvas change (`python -m pytest tests/ -q`, Python 3.12,
`requirements.txt` fully installed): **174 passed, 1 warning, 17.0 s**.

Environment note: the workspace default virtualenv `/workspace/.venv-agp` lacks
`pytest-asyncio` and `langgraph`, so the pre-existing suite cannot be collected there
(26 collection errors, no code fault). Canvas work uses `/workspace/.venv-r2canvas`, a venv
built from `requirements.txt`; this matches the repository's documented run instructions.

---

## 2. Existing implementation survey (WO-C01)

| Area | Location | Finding | Reuse decision |
| --- | --- | --- | --- |
| Artifact feature | whole repository (`grep -ri artifact`) | **No pre-existing artifact/Canvas feature.** Nothing to migrate, nothing to collide with. | New `app/canvas/` package owns it. |
| Caller identity | `app/core/auth.py` (`resolve_caller_scope`, `require_authenticated_caller`, `ExecutionScope`) | Tenant/owner/service/role resolved from credential scopes or a verified trusted proxy; fails closed; `validate_scope_identifier` rejects `:` in tenant/owner ids. | Reused as-is. Canvas never accepts tenant/owner/service from a request body or tool argument. |
| Dynamic Tool Registry | `app/tools/registry.py` (`ToolRegistry`, `ToolRegistration`, `get_definitions`) | 8 read-only builtin tools; OpenAI/NIM-compatible `ToolDefinition` output; wire layer translates `.` → `_`. | Reused: Canvas registers 8 `canvas.*` tools with JSON Schema parameters and `is_read_only` flags. |
| Tool Policy / ACL | `app/tools/policy.py` (`BUILTIN_TOOLS`, profiles, `service_bindings`) | A tool is only offered to a service when its profile lists it; unregistered services fail closed. | Canvas tools are declared builtin but are **not** added to any existing profile, so R1.3 runtime behaviour is unchanged until an operator enables them. |
| Tool Dispatcher | `app/tools/dispatcher.py` | JSON-Schema validation, timeout, per-tool byte budget, sequential execution for side-effect tools. | Reused unchanged. Canvas tools are side-effect tools (`is_read_only=False`). |
| SSE event contract | `app/schemas/events.py` (`EventType`, `SSEEvent.to_sse`) | `run_id`, `event_id`, `timestamp`, `status` + flattened `data`. | Extended additively with the 7 `artifact.*` event types; no existing event renamed or removed. |
| SQLite conventions | `app/tracing/store.py`, `settings.SQLITE_BUSY_TIMEOUT_MS` | WAL, `synchronous=FULL`, explicit busy timeout, `threading.Lock`, per-event immediate commit. | Same pragmas in the Canvas metadata store; Canvas commits one revision per transaction. |
| Blob/atomic write convention | (none for content) | `app/tools/policy.py` uses temp file + `fsync` + `os.replace` for JSON. | Same temp → fsync → `os.replace` for content blobs. |
| Adapter pattern | `app/adapters/document.py`, `app/runtime/tasks/ports.py` | Protocol + Mock/Dev implementation + fail-closed production guard. | Same pattern for the Workspace gateway and the AI edit adapter. |
| Config convention | `app/config.py` (`Settings` on `os.environ`, `AGCORE_` prefix) | One settings singleton; per-service limits are plain attributes. | Canvas settings live in `app/canvas/config.py` with the `AGCORE_CANVAS_` prefix so this file stays untouched by R2 work. |
| Router convention | `app/api/*.py`, mounted in `app/main.py` under `/api/v1` | `APIRouter(prefix=..., dependencies=[Depends(require_authenticated_caller)])`; `HTTPException` for errors; SSE via `StreamingResponse`. | Same shape; one `include_router` line added to `app/main.py`. |

### 2.1 R1.3 runtime isolation

Nothing under `app/runtime/`, `app/runtime/policy/`, `app/api/agent.py`, `app/api/document_analysis.py`
or `app/gateway/` is modified. The only shared file touched is `app/main.py` (router include,
one import block) and `app/schemas/events.py` (additive `EventType` values), plus
`app/tools/registry.py` (additive `canvas.*` registration) and `app/tools/policy.py`
(additive `BUILTIN_TOOLS` names, no profile change). Those four are listed in the handover
document's merge-target table.

### 2.2 Workspace/RAG boundary

The Workspace/RAG engineer published a frozen contract on `feat/r2-workspace-rag`
(`docs/r2/WORKSPACE_CONTRACT.md`, `app/workspace/contracts.py`, `WORKSPACE_CONTRACT_VERSION = r2-w/v1`,
mock provider at `/api/v1/workspaces/_mock`). Canvas consumes that contract **logically**:

- Canvas stores only `workspace_id` in artifact metadata and never reads or writes workspace files.
- Workspace stores only an `ArtifactReference` (id + metadata); it never stores artifact content,
  revisions, patches or exports.
- Because that branch is not merged into Canvas' baseline, Canvas defines the interface it needs
  as a `WorkspaceGateway` protocol in `app/canvas/workspace.py` with the same operation names and
  payload fields, plus (a) an isolated `LocalDevWorkspaceProvider` for development and (b) a
  `RuntimeWorkspaceProvider` that binds to `app/workspace/contracts.py::WorkspaceProvider` by lazy
  import as soon as the branch is merged. No file under `app/workspace/` is created or modified here.
- The dev provider is refused in production (`AGCORE_ENV=production` → hard error) and is flagged
  in every response it produces (`provider_mode: "dev"`), so it can never be mistaken for the
  authorization path.

### 2.3 HD2AI UI design system (WO-C01)

`AGPilot0011` is not checked out in this task (GitHub App write/install scope returns 404 for that
private repository and the brief limits this task to the backend). The design system that Canvas
UI must follow is therefore **recorded as a blocker for CP-04**, with these facts established from
`AGPilot0005/AGENTS.md` (the repository's own branding rule) and from the Workspace engineer's
baseline note:

- Dark palette tokens: `--bg: #101110`, `--accent: #ff824f`, `--card-bg: #1b1d19`,
  `--olive-text: #b9c3a7`, assets/favicons from AGPilot0002.
- HD2AI keeps a single Master Build architecture; the Canvas panel is an addition next to the
  existing chat screen, not a redesign.
- `AGPilot0011` main = `b4913d0`; `hd2ai-shared/` holds cross-panel contracts (Workspace's
  `workspace-contract.ts` is mirrored there).
No frontend file is touched by this task.

---

## 3. Independent preview environment

- Backend preview: `python -m uvicorn app.main:app --host 127.0.0.1 --port 8200`
  (adds `/api/v1/canvas/*` next to the existing surface; no gateway or model is needed for
  Canvas dev APIs — the Canvas AI paths are explicit dev adapters, see CP-03 report).
- Canvas storage is a **separate development SQLite DB + blob root**, default
  `data/canvas/canvas.db` and `data/canvas/blobs/` inside the repository (`data/` already exists
  and is not the production `AGCore` store). `AGCORE_CANVAS_DB_PATH` /
  `AGCORE_CANVAS_DATA_DIR` override it.
- No production DB, no migration on an operating system, no `Server-HQ` configuration change.

---

## 4. Canvas API contract (CP-00 definition, implemented in CP-01…CP-05)

Base: `/api/v1/canvas`. All endpoints require a verified `ExecutionScope`
(`require_authenticated_caller`). Errors use the same structured envelope as the R2-W contract:

```json
{"detail": {"code": "revision_conflict", "message": "stale base revision", "retryable": false,
            "context": {"base_revision": 2, "current_revision": 3}}}
```

| Code | HTTP | Meaning |
| --- | --- | --- |
| `artifact_not_found` | 404 | unknown, deleted, or not visible to this scope |
| `revision_not_found` | 404 | revision outside the artifact's history |
| `revision_conflict` | 409 | `base_revision` ≠ current revision |
| `expected_text_mismatch` | 409 | `expected_original_text` ≠ current text at the range |
| `invalid_selection` | 422 | range outside content, inverted, or splitting a surrogate pair |
| `invalid_request` | 422 | schema/semantic validation failure |
| `unsupported_artifact_type` | 415 | type not in the registry |
| `content_too_large` | 413 | content above `CANVAS_MAX_CONTENT_BYTES` |
| `revision_limit_reached` | 409 | artifact reached `CANVAS_MAX_REVISIONS` |
| `proposal_not_found` | 404 | unknown proposal id |
| `proposal_stale` | 409 | proposal's base revision is no longer current |
| `workspace_unavailable` | 503 | workspace gateway not configured/healthy |
| `workspace_access_denied` | 403 | workspace access check returned `none` |
| `export_format_unsupported` | 400 | format not provided for this artifact type |
| `ai_adapter_unavailable` | 503 | no verified AI edit adapter (R1.3 not integrated) |
| `storage_failure` | 500 | persistence failed; nothing was committed |

Identity and ownership: `artifact_id = "art_" + 12 hex`, `proposal_id = "prp_" + 12 hex`.
Isolation is `tenant_id` + `owner_id` + `service_id`, all taken from the `ExecutionScope`.
A foreign or unknown artifact id returns `404 artifact_not_found` — never `403` — so artifact
existence is not disclosed.

Endpoint surface (final shape is frozen by `docs/r2/CANVAS_BACKEND_HANDOVER.md` §5 after CP-05):

```
GET    /canvas/capabilities
GET    /canvas/types
GET    /canvas/tools/manifest
POST   /canvas/artifacts                                   create
GET    /canvas/artifacts                                   list (conversation_id, workspace_id, type, cursor)
GET    /canvas/artifacts/{artifact_id}                     read metadata + current content
PATCH  /canvas/artifacts/{artifact_id}                     rename
DELETE /canvas/artifacts/{artifact_id}                     delete (soft by default, `purge=true` hard)
POST   /canvas/artifacts/{artifact_id}/patches             apply patch (atomic, 409 on conflict)
POST   /canvas/artifacts/{artifact_id}/patches/preview     diff without applying
GET    /canvas/artifacts/{artifact_id}/revisions           history (metadata only)
GET    /canvas/artifacts/{artifact_id}/revisions/{rev}     one revision's content
GET    /canvas/artifacts/{artifact_id}/diff                diff between revisions
POST   /canvas/artifacts/{artifact_id}/revisions/{rev}/restore   non-destructive restore
POST   /canvas/artifacts/{artifact_id}/links               workspace/conversation link
DELETE /canvas/artifacts/{artifact_id}/links/{kind}        unlink
GET    /canvas/artifacts/{artifact_id}/export              export current/selected revision
GET    /canvas/artifacts/{artifact_id}/preview             sandboxed HTML preview payload
POST   /canvas/artifacts/{artifact_id}/edit-proposals      AI partial edit proposal (dev adapter)
GET    /canvas/artifacts/{artifact_id}/edit-proposals      list proposals
GET    /canvas/edit-proposals/{proposal_id}
POST   /canvas/edit-proposals/{proposal_id}/apply
POST   /canvas/edit-proposals/{proposal_id}/reject
POST   /canvas/generations                                 explicit artifact generation (dev SSE contract)
```

Artifact metadata fields (snake_case, AGCore convention):

```
artifact_id, workspace_id, conversation_id, tenant_id, owner_id, service_id,
title, artifact_type, language, current_revision, content_hash,
created_at, updated_at, creation_source, deleted_at
```

`artifact_type` ∈ `markdown | plain_text | source_code | html | structured_json` (registry is
extensible: chart, table, spreadsheet, office document). `creation_source` ∈
`user | ai | import | tool`. Timestamps are ISO-8601 UTC (`Z`), matching R2-W.

### 4.1 Offset contract (Backend ↔ Frontend)

All ranges are `{"start": int, "end": int, "offset_unit": "utf16" | "codepoint"}` with
**`utf16` as the default**, because the browser textarea/`String.prototype.slice` counts UTF-16
code units. The backend converts to Python code-point indices and rejects an offset that splits a
surrogate pair (`invalid_selection`). Every response that echoes a range also returns
`content_metrics: {codepoints, utf16_units, utf8_bytes}` so the frontend can verify alignment.
Korean text is BMP, so `utf16` and `codepoint` agree; they differ only for astral characters.
`utf8_byte` offsets are not accepted.

---

## 5. Workspace integration contract (Canvas side)

Canvas ↔ Workspace (owner: Workspace/RAG Engineer) — Canvas needs exactly four operations and
never a fifth:

| Operation | Canvas use | Failure mapping |
| --- | --- | --- |
| `check_workspace_access(workspace_id, scope)` → `{level ∈ read/write/admin/none}` | before linking an artifact to a workspace, and before listing artifacts by workspace | `none` → `403 workspace_access_denied`; provider down → `503 workspace_unavailable` |
| `get_workspace(workspace_id, scope)` → identity | validate existence when linking | not found → `404 workspace_unavailable` (no id disclosure) |
| `register_artifact_reference(workspace_id, scope, {artifact_id, kind, metadata})` | after a successful link, so the workspace can list the artifact | failure keeps the local link but reports `workspace_sync: "pending"`; never silently reported as synced |
| `list_artifact_references(workspace_id, scope)` | reconciliation/diagnostics only | reported as unavailable, not fatal |

Canvas never calls `create_file`, `read_file`, `list_workspace_files`, search, embedding or index
endpoints — no workspace file system, indexing or RAG behaviour is duplicated. Canvas does not
write files into a workspace; "Export to workspace" is explicitly **out of scope** for R2
(handover item, requires the Workspace owner's approval of a file-write API use).

The dev provider (`LocalDevWorkspaceProvider`) keeps an in-memory workspace table seeded with
`ws_dev00000001` for development, returns `provider_mode: "dev"`, is refused when
`AGCORE_ENV=production`, and never claims to perform an authorization check.

---

## 6. Out of scope (recorded, not implemented)

- Canvas frontend (CP-04, WO-C09…C12) — `AGPilot0011` not accessible to this task.
- Real LLM artifact generation and AI edit proposals (requires R1.3 runtime integration; Canvas
  ships contracts + dev adapters and does **not** claim AI integration is verified).
- Office export engines (DOCX/PDF/XLSX/PPTX) — provider extension contract only.
- Export into a workspace file, multi-user real-time co-editing, Workspace file storage.
