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
