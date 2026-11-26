# AGPilot0005 — Phase R1.1 Master QA & Benchmark Report

## 1. Master QA Matrix (`QA-R1-01` ~ `QA-R1-20`)

| QA ID | Verification Item | Severity | Status | Evidence |
|---|---|---|---|---|
| QA-R1-01 | ValidatedAction execution match | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_01_*` |
| QA-R1-02 | Tool budget bypass prevention | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_01_and_02_*` |
| QA-R1-03 | Trajectory owner isolation | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_03_and_04_*` |
| QA-R1-04 | Feedback referential integrity | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_03_and_04_*` |
| QA-R1-05 | Non-stream 1,000+ event handling | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_05_and_06_*` |
| QA-R1-06 | SSE reconnection & HTTP 410 expiration | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_05_and_06_*` |
| QA-R1-07 | Terminal state race & late result block | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_07_and_08_*` |
| QA-R1-08 | Hard deadline enforcement | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_07_and_08_*` |
| QA-R1-09 | Authentication & secret fail-closed | P0 | PASS | `tests/test_r1_1_stage_a.py::test_qa_r1_09_*` + secret scan (`0` hits) |
| QA-R1-10 | Policy parameter real execution | P1 | PASS | `tests/test_r1_1_stage_b.py::test_qa_r1_10_*` |
| QA-R1-11 | Safe fallback constraint validation | P1 | PASS | `tests/test_r1_1_stage_b.py::test_qa_r1_11_*` |
| QA-R1-12 | Telemetry failure isolation & flush | P1 | PASS | `tests/test_r1_1_stage_b.py::test_qa_r1_12_*` |
| QA-R1-13 | Actual token usage & outcome recording | P1 | PASS | `tests/test_r1_1_stage_b.py::test_qa_r1_13_*` |
| QA-R1-14 | Legacy vs LangGraph cross-runtime replay | P1 | PASS | `tests/test_r1_1_stage_cd.py::test_qa_r1_14_*` |
| QA-R1-15 | SSE & citation compatibility | P1 | PASS | `tests/test_runtime_and_api.py`, `tests/test_advanced_scenarios.py` |
| QA-R1-16 | Dynamic tool budget (`reg.parameters`, rem=0) | P1 | PASS | `tests/test_r1_1_stage_cd.py::test_qa_r1_16_and_17_*` |
| QA-R1-17 | Tool JSON Schema validation & truncation | P1 | PASS | `tests/test_r1_1_stage_cd.py::test_qa_r1_16_and_17_*` |
| QA-R1-18 | Policy version pinning & rollback | P2 | PASS | `tests/test_r1_1_stage_cd.py::test_qa_r1_18_*` |
| QA-R1-19 | Existing TaskOrchestrator regression | P1 | PASS | `tests/test_task_orchestrator.py` (included in 133/133) |
| QA-R1-20 | Full regression & REG-POL-11..20 | P0 | PASS | `pytest -q`: **133 passed, 0 failed** (`12.74s`) |

## 2. Performance & Reliability Benchmark (`scripts/benchmark_r1_1.py`)
- **Environment**: Python 3.13.2, macOS 15.7.9 arm64, `FastMockGateway` (in-process, excludes upstream LLM network latency).

| Scenario | Legacy P50 / P95 / P99 (ms) | Legacy Peak Mem | LangGraph R1 P50 / P95 / P99 (ms) | LangGraph R1 Peak Mem | Persisted / Dropped / Failed |
|---|---:|---:|---:|---:|---:|
| Single (`1`) | 6.15 / 6.15 / 6.15 | 31.5 KB | 99.48 / 99.48 / 99.48 | 297.0 KB | 12 / 0 / 0 |
| Small (`10`) | 17.52 / 17.89 / 17.89 | 119.1 KB | 341.02 / 518.96 / 518.96 | 1,150.3 KB | 120 / 0 / 0 |
| Medium (`50`) | 79.29 / 80.66 / 80.93 | 587.4 KB | 1,676.64 / 2,456.71 / 2,538.42 | 5,573.7 KB | 600 / 0 / 0 |
