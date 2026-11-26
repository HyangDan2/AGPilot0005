# AGPilot0005 — Runtime Hardening R1.2 QA & Benchmark Report

## 1. FIX-201 ~ FIX-212 Closure Matrix

| Fix ID | Title | Severity | Status | Evidence |
|---|---|---|---|---|
| FIX-201 | Complete Authentication Integration | P0 | PASS | `tests/test_r1_2_stage_a.py::test_fix_201_*` |
| FIX-202 | Verified ConstraintSpec | P0 | PASS | `tests/test_r1_2_stage_a.py::test_fix_202_*` |
| FIX-203 | AGTools Credential & Boot Fail-Fast | P0 | PASS | `tests/test_r1_2_stage_a.py::test_fix_203_*` |
| FIX-204 | Trajectory Ownership Migration & Isolation | P0 | PASS | `tests/test_r1_2_stage_a.py::test_fix_204_*` |
| FIX-205 | Tool Outcome Status Correction (`completed`) | P1 | PASS | `tests/test_r1_2_stage_b.py::test_fix_205_*` |
| FIX-206 | Strict Dynamic Tool Budget & `additionalProperties` | P1 | PASS | `tests/test_r1_2_stage_b.py::test_fix_206_*` |
| FIX-207 | Production Gateway Per-Invocation Usage Contract | P1 | PASS | `tests/test_r1_2_stage_b.py::test_fix_207_*` |
| FIX-208 | Feedback Direct Atomic Commit & SQLite Integrity | P1 | PASS | `tests/test_r1_2_stage_b.py::test_fix_208_*` |
| FIX-209 | Compiled LangGraph Reuse & 50-Run Isolation | P1 | PASS | `tests/test_r1_2_stage_c.py::test_fix_209_*` |
| FIX-210 | Full `AgentRunner` Cross-Runtime Replay | P1 | PASS | `tests/test_r1_2_stage_c.py::test_fix_210_*` |
| FIX-211 | SSE Replay, HTTP 410 & Disconnect Isolation | P1 | PASS | `tests/test_r1_2_stage_c.py::test_fix_211_*` |
| FIX-212 | Legacy Terminal State Fence Protection | P1 | PASS | `tests/test_r1_2_stage_c.py::test_fix_212_*` |

## 2. Performance Before vs After FIX-209 (`scripts/benchmark_r1_1.py`)
- **Environment**: Python 3.13.2, macOS 15.7.9 arm64, `FastMockGateway` (in-process).
- **Raspberry Pi Benchmark**: `NOT RUN` (awaiting deployment approval).

| Scenario | R1.1 Before (P50 / P99, Peak Mem) | R1.2 After FIX-209 (P50 / P99, Peak Mem) | Improvement | Persisted / Dropped / Failed |
|---|---|---|---|---|
| Concurrent `1` | 99.48ms / 99.48ms (297.0 KB) | **61.91ms / 61.91ms (270.2 KB)** | **-38% latency** | 12 / 0 / 0 |
| Concurrent `10` | 341.02ms / 518.96ms (1,150.3 KB) | **176.57ms / 184.90ms (849.1 KB)** | **-64% P99, -26% mem** | 120 / 0 / 0 |
| Concurrent `50` | 1,676.64ms / 2,538.42ms (5,573.7 KB) | **895.00ms / 930.04ms (4,057.3 KB)** | **-63% P99, -27% mem** | 600 / 0 / 0 |

## 3. Final Release Gate Closure Matrix (`NG-01` ~ `NG-08`)

| ID | Verification | Status | Evidence |
|---|---|---|---|
| NG-01 | AGTools auth contract & Disabled path (0 outbound calls) | **PASS** | `tests/test_r1_2_final_cp1.py::test_ng_01_*` + `scripts/ops_precheck_r12.sh` |
| NG-02 | Real `AIGatewayClient` HTTP Fixture (`_structured=True`, stream & non-stream) | **PASS** | `tests/test_r1_2_final_cp2.py::test_ng_02_*` |
| NG-03 | Credential Scope binding, BFF delegation, unregistered service 403, run-scoped HTTP tool | **PASS** | `tests/test_r1_2_final_cp1.py::test_ng_03_*` |
| NG-04 | Pre-handler zero-budget gate (`0` handler calls) & Policy budget propagation | **PASS** | `tests/test_r1_2_final_cp2.py::test_ng_04_*` |
| NG-05 | SQLite Dedup integrity verification & HTTP 503 fail-closed + Preflight SQL | **PASS** | `tests/test_r1_2_final_cp2.py::test_ng_05_*` + `scripts/check_sqlite_dedup.sql` |
| NG-06 | SSE terminal-cursor reconnect no-dup & mid-replay drain ordering | **PASS** | `tests/test_r1_2_final_cp2.py::test_ng_06_*` |
| NG-07 | Bounded session TTL & metadata-only Tombstone (410 owner / 404 other) | **PASS** | `tests/test_r1_2_final_cp2.py::test_ng_07_*` |
| NG-08 | Dirty Set persistence (no new worker/table) & training export filtering | **PASS** | `tests/test_r1_2_final_cp2.py::test_ng_08_*` |

## 4. R1.2 Release Candidate Hotfix Closure Matrix (`CR-01` ~ `CR-09`)

| ID | Verification | Status | Evidence |
|---|---|---|---|
| CR-01 | HTTP provider registration (`AGTOOLS_ENABLED=True`/`False`) & non-AGTools provider isolation | **PASS** | `tests/test_r1_2_rc_hotfix_cp1.py::test_cr_01_*` |
| CR-02 | Real FastAPI → Auth → Immutable `ExecutionScope` → `AgentRunner` (Legacy + LangGraph) → `Dispatcher` → HTTP Tool + Tenant/Owner/Service isolation | **PASS** | `tests/test_r1_2_rc_hotfix_cp1.py::test_cr_02_*` |
| CR-03 | Credential secret collision (`duplicate_credential_secret`) boot fail-fast & BFF delegation guard | **PASS** | `tests/test_r1_2_rc_hotfix_cp1.py::test_cr_03_*` |
| CR-04 | `LangGraphR1Runtime` Policy-approved `context_budget_tokens` propagation into `tool_node` | **PASS** | `tests/test_r1_2_rc_hotfix_cp2.py::test_cr_04_*` |
| CR-05 | SQLite `PRAGMA index_list` + `PRAGMA index_info` real corrupted/wrong-column dedup index detection (no manual flag override) | **PASS** | `tests/test_r1_2_rc_hotfix_cp2.py::test_cr_05_*` |
| CR-06 | Slow-subscriber overflow gap honesty (never silently skipping to normal `run.completed`) | **PASS** | `tests/test_r1_2_rc_hotfix_cp2.py::test_cr_06_*` |
| CR-07 | Auto-eviction on `create_run` & finish + Active Run Admission Control (`429`) + Status/Cancel/SSE contract parity | **PASS** | `tests/test_r1_2_rc_hotfix_cp2.py::test_cr_07_*` |
| CR-08 | `mark_incomplete()` exception propagation preserving Dirty Markers on DB failure until actual commit + bounded `incomplete_trajectories` | **PASS** | `tests/test_r1_2_rc_hotfix_cp2.py::test_cr_08_*` |
| CR-09 | Trajectory completeness proof (`expected_event_count`, orphan/duplicate checks) & fail-closed `export_training_jsonl()` | **PASS** | `tests/test_r1_2_rc_hotfix_cp2.py::test_cr_09_*` |
