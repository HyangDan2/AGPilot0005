# AGCore R2-W — Minimum Functional QA Report (CP-W06 / WO-W20)

Date: 2026-11-27 KST · Branch `feat/r2-workspace-rag` · Commit `1c9d4f1` (backend) / `0d623d1` (frontend)
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
