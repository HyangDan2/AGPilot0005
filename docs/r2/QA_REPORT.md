# AGCore R2-W — Minimum Functional QA Report (CP-W06 / WO-W20) + Amendment 01

Date: 2026-11-27 KST · Branch `feat/r2-workspace-rag` · CP-W06 `1c9d4f1` (backend) / `0d623d1` (frontend); Amendment 01 `907482c` contract, `87d4141` engine (`feat/r2-workspace-rag`).
Scope: Lean Engineering Policy v1.1 — only the functions this work order changes were re-verified;
full regression was run once at the end of the backend work.

## 1. Automated tests

| Suite | Command | Result |
| --- | --- | --- |
| R2-W workspace (contract, storage, indexing, retrieval, API) | `python -m pytest tests/test_r2_workspace_*.py -q` | **52 passed** |
| Full AGCore suite (existing + new) | `python -m pytest -q` | **226 passed**, 1 warning |

Note: the sandbox initially lacked dev dependencies `langgraph`, `langchain-core` and `pytest-asyncio`
(pre-existing environment gap; the untouched R1.3 tests failed to even collect). After installing the
versions pinned in `requirements.txt` the whole suite passes — no existing test regressed.

Covered by suite (highlights):
`test_scope_isolation_tenant_owner_service`, `test_path_traversal_and_unsafe_names_rejected`,
`test_symlink_components_are_refused`, `test_file_size_and_workspace_quota`,
`test_agtools_ingestor_reuses_parser_chunks`, `test_stale_revision_job_cannot_overwrite_newer_index`,
`test_deleted_file_during_indexing_never_reappears`, `test_retry_then_failed_with_reason`,
`test_queue_cap_and_reconciliation`, `test_restart_recovery_requeues_indexing_job`,
`test_korean_keyword_search_matches_with_particles`, `test_vector_search_ranks_related_document_first`,
`test_hybrid_search_fuses_keyword_and_vector`, `test_hybrid_degrades_to_keyword_when_embedding_unavailable`,
`test_vector_index_is_separated_by_model_and_dimension`, `test_prompt_injection_is_data_not_instruction`,
`test_deleted_file_citation_reports_deleted`, `test_extension_tools_manifest_and_invocation`.

## 2. Preview backend end-to-end (real HTTP, local embedding)

Server: `uvicorn app.workspace.preview_app:app --port 8210`, local embedding (512d), chunk 128/192/24 for a small doc.

```
PASS upload                     file wf_… 2304 bytes text/plain
PASS index ready                READY, 5 chunks
retrieval_modes ['keyword','vector','hybrid']  embedder local/local-hash-v1/512 available
PASS hybrid search              mode hybrid, matched_by ['keyword','vector'], degraded false
PASS context ok                 used 191–800 tokens ≤ budget 800
PASS citations                  cit_… incl. citation_id in context_text
PASS citation resolve           available true, revision_status current
PASS citation validate          valid [cit_…], unknown [cit_bogus]
PASS keyword korean             "심볼릭 링크" matched
PASS traversal blocked          ../../escape.txt → 400 invalid_path
```

Restart check: with the same DB/storage, workspaces, files (`READY`), chunk count (5) and hybrid search
results were identical after a server restart (state recovery + FTS/vector persistence).

## 3. Real AGTools parser integration (DOCX + PPTX)

AGPilot0006 started locally (`uvicorn app.main:app --port 8006`, dev auth bypass, data dir in /tmp);
workspace preview pointed at it with `AGCORE_WORKSPACE_AGTOOLS_ENABLED=true`.

```
PASS upload report.docx         (36,893 bytes)
PASS upload slides.pptx         (29,318 bytes)
PASS both files indexed         ready 2 / failed 0, chunks_total 4
PASS parser chunk search        top report.docx section "검색 정책" matched_by ['keyword','vector']
PASS context from docx/pptx     status ok, used ≤ 600 budget, sources include pptx slide 1
PASS pptx slide found           slide 2, section "보안 정책"
```

`section` and slide `page_number` came from the parser's own chunk/source metadata — Workspace re-used
AGTools chunking (`chunking_version = "agtools"`) instead of re-splitting.

## 4. Workspace UI (headless Chromium, 1360×900 then 390×844)

Built `hd2ai-admin` against the preview backend (`VITE_WORKSPACE_API_BASE=…:8210/api/v1/workspaces`).

| Step | Result |
| --- | --- |
| Open sidebar → `🗂️ 워크스페이스` | panel opens over the existing chat UI |
| Create workspace (prompt) | workspace created, header shows `rev 1` |
| Upload `qa-doc.txt` via file input | progress bar → row appears, badge `READY` after indexing (≤ 45 s) |
| 검색 tab → "RRF 융합 인덱스 상태" | 1+ results with file, score, kw/vec chips |
| RAG 채팅 tab → question | assistant answer with `Mock 응답` badge, `근거 1건 · 191/2048 tokens`, citation chip |
| Click citation | preview modal shows original text + `revision 1 · current` + download |
| 인덱스 tab | READY/PENDING/… counts, embedder status, per-file 재색인/취소 |
| Mobile viewport 390×844 | panel switches to stacked layout and stays usable |

Console: only `GET /theme-toggle.js 404` — a pre-existing Server-HQ-served asset missing in the local
`vite preview`, unrelated to the workspace code. No React errors, no failed workspace requests.

Screenshots (copied for handover): `/workspace/downloads/r2-w-qa/01-home.png` … `09-mobile.png`.

## 5. Known gaps

- RAG answers are mock-labelled by default (no live gateway call in QA).
- Remote embedding provider tested with mocked HTTP only (no paid API used).
- AGTools PDF path uses the same protocol as DOCX/PPTX (mocked test) but was not parsed with a real PDF in QA.

---

# Amendment 01 — folder-based workspace QA

Date: 2026-11-27 KST · `907482c` (contract) · `87d4141` (engine) · AGPilot0011 `1935d53` (contract types) · branch `feat/r2-workspace-rag`
Scope: the ten Amendment 01 requirements, the existing R2-W regression, and the frontend lifecycle UI.

## A1.1 Automated tests

| Suite | Command | Result |
| --- | --- | --- |
| Amendment 01 contract | `python -m pytest tests/test_r2_workspace_am01_contract.py -q` | **21 passed** |
| Amendment 01 folder engine | `python -m pytest tests/test_r2_workspace_am01_folder.py -q` | **18 passed** |
| R2-W regression (v1 contract/storage/api/indexing/retrieval) | `python -m pytest tests/test_r2_workspace_*.py -q` | **78 passed** |
| Full AGCore suite | `python -m pytest -q` | **265 passed**, 1 warning (pre-existing R1.3 warning) |
| Frontend typecheck | `node node_modules/typescript/bin/tsc -p tsconfig.app.json` | **clean** |
| Frontend production build | `vite build --outDir dist-workspace-qa` | **built** (`index-*.js` 875 kB, `index-*.css` 72 kB) |

Requirement coverage (test names in `tests/test_r2_workspace_am01_folder.py`):

| Work order item | Test / evidence | Result |
| --- | --- | --- |
| 1 Folder root + provider | `test_open_folder_mounts_zero_copy_and_keeps_metadata_outside`, `test_new_workspace_creates_a_real_folder_under_the_allowed_root` | PASS |
| 2 Open / New / Close / Reopen | `test_close_keeps_metadata_and_reopen_catches_up`, `test_reopen_requires_the_root_to_still_exist`, `test_http_open_close_reopen_disconnect_and_write` | PASS |
| 3 Zero-copy | same open test: blob storage usage `0`, file read in place, root unchanged | PASS |
| 4 File watcher → incremental index | `test_watcher_applies_create_modify_move_delete_to_the_index`, `test_watcher_thread_picks_up_external_change`, `test_watcher_and_opening_scan_do_not_race` | PASS |
| 5 Canvas/Coding Agent write contract | `test_write_contract_is_identical_on_both_folder_providers`, `test_write_contract_path_gate_and_read_only` | PASS |
| 6 Disconnect vs. real deletion | `test_disconnect_never_deletes_the_real_folder`, `test_source_deletion_requires_operator_opt_in` | PASS |
| 7 Storage provider separation | `test_open_folder_rejects_outside_root_traversal_and_symlink`, `test_local_browser_workspace_never_uses_a_server_path` | PASS |
| 8 Browser constraints | same local_browser test + HTTP test (`client_write`, `server_side_path_access:false`) | PASS |
| 9 Allowed root confinement | outside root / `..` / relative path / symlink / missing root / `metadata_overlap` cases | PASS |
| 10 Metadata & index separation | open test asserts the root file list is unchanged and `index_storage.inside_workspace_root == false` | PASS |

## A1.2 UI data-layer end-to-end (real HTTP, live preview backend)

Backend: `uvicorn app.workspace.preview_app:app` with `AGCORE_WORKSPACE_ALLOWED_ROOTS=qa:<dir>:global`,
watcher interval 0.05 s (maximum race pressure), local embedding, AGTools off. Client: the built
`WorkspaceClient` the panel uses. Log: `/workspace/downloads/r2-w-am01-qa/client-e2e.log`.

```
PASS  allowed-roots lists the configured root
PASS  open folder mounts server_mounted_folder / zero-copy / lifecycle open
PASS  open folder is idempotent for the same owner
PASS  zero-copy files carry relative paths        (overview.md, policy/index.md, policy/parsing.md)
PASS  index storage lives outside the root
PASS  watcher reports running
PASS  write contract returns the workspace file / zero-copy / revision 1
PASS  sync reports the scanned tree
PASS  close keeps metadata; closed workspace refuses writes with workspace_closed
PASS  reopen restores the workspace
PASS  new workspace creates a real folder; same write call works on a managed folder
PASS  browser workspace has no server path access; writes are client directives
PASS  source deletion is refused by default (403 source_delete_not_permitted)
PASS  disconnect releases metadata only; released workspace is gone
# source files preserved after disconnect: every original file still on disk
SUMMARY 23/23
```

During this run a real defect was found and fixed: the watcher thread could scan a workspace while the
opening scan was still inserting rows, which raised `UNIQUE constraint failed: uq_file_name` on the
second open of the session. `FolderSyncService.workspace_lock` now serialises opening scan, watcher pass,
agent write and file delete per workspace (`test_watcher_and_opening_scan_do_not_race`, plus a defensive
`IntegrityError` re-read in `_insert_row`).

## A1.3 Browser (headless) verification — environment limit

This sandbox has **no browser that can start**: Playwright's Chromium and WebKit both fail on missing
system libraries (`libnspr4.so`, `libnss3.so`, `libatk-1.0.so.0`, `libgtk-4.so.1`, `libgstreamer-1.0.so.0`),
`apt`/root are unavailable, and the MCP browser refuses loopback URLs
(`private/loopback address 127.0.0.1 is not allowed`). No UI screenshots could therefore be produced in
this environment.

What was verified instead: frontend typecheck, production build, and the 23/23 data-layer E2E above,
which exercises exactly the calls each new UI control makes. The screenshot harness is committed as
`/workspace/qa/am01_qa.py` (Playwright: opens the panel, Open Folder → close → reopen → New Workspace →
disconnect-vs-delete → blocked source deletion → browser-folder dialog → disconnect) and writes
`01-workspace-panel-open.png` … `12-workspace-disconnected.png` into `/workspace/downloads/r2-w-am01-qa/`.
Run it in any environment that has browser dependencies installed:

```bash
python3 /workspace/qa/am01_qa.py   # needs playwright + chromium system libs
```

`/workspace/downloads/r2-w-am01-qa/` currently holds `client-e2e.log` (A1.2 evidence) and `QA-README.md`
(how to reproduce, including the screenshot run).

## A1.4 Known gaps (Amendment 01)

- Watcher is polling (default 2 s), not FS-event based; see HANDOVER §11b.
- `local_browser` handles are session-scoped in the browser; reconnect after reload by picking again.
- Folder rename/move propagates to descendants on the next watcher pass.
- No UI screenshot artifacts in this run (A1.3).
