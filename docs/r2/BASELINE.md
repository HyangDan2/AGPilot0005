# R2-W Baseline & Isolation Record (CP-W00 / WO-W01)

Recorded 2026-11-27 (KST) by the Workspace/RAG Engineer. Branch in every repository:
`feat/r2-workspace-rag`. No `main` merge, no deployment, no operating server/DB change.

## 1. Repository baselines

| Repository | Role | Baseline `origin/main` SHA | State at clone | Local branch |
| --- | --- | --- | --- | --- |
| `HyangDan2/AGPilot0005` | AGCore Workspace & RAG engine | `0d2047f09a67f4cfd44f8c5399523eab3eac2172` — `docs(release): finalize CHANGELOG for v1.2.0` | public, clean, `main` == v1.2.0 | `feat/r2-workspace-rag` |
| `HyangDan2/AGPilot0006` | AGTools Document Parser & Tool Provider | `6d0f57f36c516545d7c5a307f26172c1b958ab2b` | public, clean, parser v2.x line, has unmerged `codex/server-retention-20261124` branch (not touched) | `feat/r2-workspace-rag` |
| `HyangDan2/AGPilot0011` | HD2AI Workspace UI & API client | `b4913d0d39f8ca223f48bb7b5b7fbd99f6126ebb` — PR #3 `feat/wo-02a-r12-downstream-sync` | private **and accessible** (shared with Pickle), clean | `feat/r2-workspace-rag` |

`AGCore v1.2.0 (0d2047f)` confirmed as the release baseline; `app/main.py` still declares
`version="1.1.0"` in its FastAPI metadata while `CHANGELOG.md` marks v1.2.0 — noted, not changed.

No pre-existing uncommitted user changes were present in any clone (fresh clones into
`/workspace/repos/`), so nothing needed stashing or worktree protection.

## 2. Existing implementations surveyed

| Area | Location | Reuse decision |
| --- | --- | --- |
| Caller authentication / `ExecutionScope` | `app/core/auth.py` (`resolve_caller_scope`, `require_authenticated_caller`) | Reused as-is; workspace config derives tenant/owner/service only from it. Not modified. |
| Document adapter (AGTools client) | `app/adapters/document.py` | Read for patterns; the workspace ingestion adds a workspace-scoped client rather than changing it. |
| Tool registry / dispatcher / policy | `app/tools/registry.py`, `app/tools/dispatcher.py`, `app/tools/policy.py` | Untouched. Workspace tools are exposed via an independent adapter + manifest (CP-W04). |
| Context budget | `app/config.py::CONTEXT_BUDGET_TOKENS`, `app/runtime/context.py` | Pattern copied into the workspace context assembler; runtime context module untouched. |
| SQLite conventions | `app/tracing/store.py`, `SQLITE_BUSY_TIMEOUT_MS` | Same pragmas (WAL, busy timeout) in the new workspace store. |
| AI Gateway client | `app/gateway/client.py` | **No embedding endpoint exists** in the gateway client (chat only). The workspace embedding layer therefore implements independent providers and does not assume a gateway embedding API. |
| AGTools parser/chunking/search | `AGPilot0006` `app/api/v1/documents.py`, `app/parsers/chunking_engine.py`, `app/services/document_search.py`, `app/services/vector_index.py` | **Reused over HTTP** (upload → status → chunks → chunk detail). No PDF/DOCX/PPTX parser is duplicated. AGPilot0006 is currently **not modified** (feature branch exists but is identical to `main`). |

## 3. Conflict analysis (R1.3 runtime & Canvas)

Files the R1.3 runtime cutover or the Canvas engineer may touch, and how R2-W avoids them:

| Risk file | Owner | R2-W approach |
| --- | --- | --- |
| `app/main.py` (router table) | R1.3 runtime | Not modified. Integration is a single `include_router` line listed in the handover doc. |
| `app/api/agent.py`, `app/runtime/**`, `app/runtime/policy/**` | R1.3 runtime | Not modified; workspace preview app does not import them. |
| `app/core/auth.py`, `app/config.py` | R1.3 runtime | Not modified. Workspace settings live in `app/workspace/config.py` (env-prefixed `AGCORE_WORKSPACE_*`). |
| `app/tools/registry.py`, `app/tools/dispatcher.py` | AGCore tools | Not modified. Extension adapter is standalone. |
| Canvas: Artifact content/revision/patch/editor/UI/export | Canvas Engineer | Never implemented here. Only `ArtifactReference` (id + metadata) is stored. |
| `AGPilot0011/hd2ai-admin/src/App.tsx`, shared router, chat flow | Frontend / Canvas | Workspace UI is a separate panel/module plus its own client; existing chat path untouched (CP-W05). |

## 4. Independent dev run

```
# backend preview (no runtime, no gateway needed)
cd /workspace/repos/AGPilot0005
python -m uvicorn app.workspace.preview_app:app --host 127.0.0.1 --port 8210
# mock provider for Canvas:   GET  /api/v1/workspaces/_mock/workspaces

# tests
python -m pytest tests/test_r2_workspace_contract.py -q
```

Environment variables introduced by R2-W are prefixed `AGCORE_WORKSPACE_` and documented in
`docs/r2/HANDOVER.md` (CP-W06). They are read only by `app/workspace/config.py`, so an
unconfigured deployment behaves exactly as today.

## 5. Parallel Workers

| Worker | Branch/scope | Shared contract dependency |
| --- | --- | --- |
| Canvas Engineer | `feat/r2-canvas` | `docs/r2/WORKSPACE_CONTRACT.md`, `clients/workspace-contract.ts`, mock provider (`/api/v1/workspaces/_mock`) |
| Workspace Engineer (this) | `feat/r2-workspace-rag` | Contract owner |
