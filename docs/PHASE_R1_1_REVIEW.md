# AGPilot0005 — Phase R1.1 Review & Hardening Summary

- **Baseline Commit**: `c2f7de4b44edeaf6ad40bc662a4d69bbec5cab03`
- **Working Branch**: `feature/runtime-r1-corrective`
- **Deployment Status**: `AGCORE_RUNTIME_ENGINE=legacy` preserved (Release Hold pending approval)

## 1. Pre-flight Repository & Working Tree Audit
- `HEAD` and `origin/main` matched Baseline Commit `c2f7de4b44edeaf6ad40bc662a4d69bbec5cab03`.
- 7 pre-existing uncommitted local files (`app/runtime/document_analysis.py`, `app/runtime/tasks/{domain,orchestrator,progress,services}.py`, `data/runtime_config.json`, `tests/test_task_orchestrator.py`) were identified as out-of-scope TaskOrchestrator work and preserved untouched throughout CP1–RC.

## 2. Production Secret Rotation Audit (`R1-FIX-05 B.7`)
- Read-only inspection of 17 production systemd/env files on Raspberry Pi confirmed `AGCORE_AGTOOLS_TOKEN` was **not** explicitly set, meaning production previously relied on the source default token.
- The hardcoded default token has been removed from `app/config.py` (`AGTOOLS_TOKEN=""` fail-closed).
- **Action Required Before Production Cutover**: Provision a newly rotated `AGCORE_AGTOOLS_TOKEN` in Pi `/etc/hd2lab-agcore*` and matching `AGTOOLS_SERVICE_TOKEN` in AGTools before deploying R1.1.

## 3. Root-Cause Fixes Delivered
- **CP1 (`011001c`)**: `ValidatedAction` mint-token contract (`R1-FIX-01`), Caller `ExecutionScope` & Trajectory/Feedback owner+referential ACL (`R1-FIX-02`), Non-blocking `RunEventBus` with `Last-Event-ID` & HTTP 410 Gone (`R1-FIX-03`), Monotonic `HardDeadline` & atomic `TerminalStateFence` (`R1-FIX-04`), Fail-closed Auth & Secret removal + actual-body size middleware (`R1-FIX-05`).
- **CP2 (`312d670`)**: Typed `DecisionType` & real `reasoning_effort`/`context_budget_tokens` wiring (`R1-FIX-06`), `SAFE_RESPOND` non-mutating tool disable & context-budget fallback guard (`R1-FIX-07`), `AsyncTrajectoryWriter` bounded queue + failure isolation + Lifespan shutdown (`R1-FIX-08`), actual provider `token_usage` capture (`R1-FIX-09`).
- **CP3 (`793020f`)**: Cross-runtime Legacy vs LangGraph Replay Harness & fixture exhaustion fail-fast (`R1-FIX-10`), pseudo-tool syntax streaming guard & citation propagation (`R1-FIX-11`), `reg.parameters` dynamic tool budget + `remaining=0` guard + JSON Schema validation + valid JSON truncation + read-only/side-effect batch separation (`R1-FIX-12`), strict explicit allowlist privacy sanitizer (`R1-FIX-13`), `PolicyRegistry` with version pinning & rollback (`R1-FIX-14`).

## 4. Known Bottleneck & Backlog
- **Benchmark Bottleneck**: `LangGraphR1Runtime.execute()` currently compiles `StateGraph` per run (~99 ms P50 vs 6 ms legacy under in-process MockGateway). Pre-compiling a reusable graph template can reduce per-run overhead in a follow-up optimization.
- **Unfinished R1 Backlog (Excluded per Section 8)**: Workspace RAG, Vector DB / Embedding Provider integration, Desktop Agent executable, TaskOrchestrator checkpointer migration, RL / Contextual Bandit training engine.
