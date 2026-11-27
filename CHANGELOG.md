# Changelog

## [1.3.0] - 2026-11-27 (main)
### Phase B — Runtime Parity, LangGraph Single Runtime & Legacy Retirement (`WO-05` B1~B3, `AM-05`, `AM-06`)
- Added 14-scenario Runtime Parity suite (`tests/test_r1_3_runtime_parity.py`) and closed LangGraph parity defects: non-delta streaming `answer.delta` & `legacy_citations` emission, `COMPACT_CONTEXT` fallback `rejected_signatures` loop fix, `GatewayStreamError` normalization, cancelled early-return error guard, pseudo-tool markup stream block, and `SAFE_RESPOND` fallback reason preservation.
- Retired Legacy runtime execution loop (`app/runtime/runner.py`) and unused legacy graph builders (`app/runtime/graph.py`), establishing `LangGraphR1Runtime` as the single execution entrypoint while preserving `TaskOrchestrator`, `DocumentAnalysisManager`, `AGPolicy`, `ActionValidator`, `ExecutionScope`, Context Budget, SSE EventBus, and Trajectory/Feedback contracts.
- Enforced `AM-06` Boot Fail-Fast for explicit `AGCORE_RUNTIME_ENGINE=legacy` and unrecognized engine values (`validate_runtime_engine_config()`), defaulting `RUNTIME_ENGINE` to `langgraph_r1`.
- Registered `demo-console` service binding (`public-safe`) and updated `/api/v1/health` to report `version: "1.3.0"` and `runtime_engine: "langgraph_r1"`.

## [1.2.0] - 2026-11-27 (main)
### Release Gate & Final Defect Closure (`HF-01` ~ `HF-07`, `ADD-01` ~ `ADD-02`, `RB-01` ~ `RB-02`)
- Removed legacy `SERVICE_TOKENS` / `ADMIN_SERVICE_TOKENS` scope-bypass branches; unified caller auth to `AGCORE_CREDENTIAL_SCOPES` + `AGCORE_CREDENTIAL_SECRETS` and verified Trusted Proxy (`X-Trusted-Proxy-Secret`); blocked invalid credential → Trusted Proxy fallback and unregistered service API/tool fallback (`HF-01`, `ADD-01`).
- Enforced full-path verified `ExecutionScope` (Tenant + Owner + Service) across Document Adapter, Document Reference pre-validation, Durable Document Analysis (`create` & `resume` via `ScopedDocuments`), Trajectory, and Feedback without auto-adopting legacy tenant-less data (`HF-02`, `ADD-02`).
- Enforced explicit admin management scope (`allowed_tenants`, `allowed_services`) with fail-closed on `None`/empty and no implicit wildcard; forbade single `:` delimiter collisions across auth, `AnalysisServices`, and boot configuration (`ADD-01`, `RB-01`, `RB-02`).
- Propagated policy-approved `context_budget_tokens` to Document Tool's own `tool_token_budget` (`HF-03`).
- Upgraded SQLite feedback dedup integrity check with exact `WHERE` predicate matching and `SAVEPOINT` runtime dedup enforcement probe (`HF-04`).
- Made SSE cursor validation, replay snapshot, and subscriber registration atomic via `RunEventBus.open_stream()` (`HF-05`).
- Made Active Run Admission Control (`max_active_runs`, HTTP `429`) atomic via synchronous lock reservation with zero `await` inside lock and immediate failure rollback (`HF-06`).
- Unified `AsyncTrajectoryWriter.flush()` and `shutdown()` under a single monotonic deadline (`HF-07`).

### R1.2 Release Candidate Hotfix (CP1 `4c7451a` & CP2 `ed4a196` — `CR-01` ~ `CR-09`)
- Fixed `_register_http_tools()` undefined variable error, scoped `AGTOOLS_ENABLED=false` strictly to AGTools provider, and handled read-only policy persistence gracefully (`CR-01`).
- Unified verified immutable `ExecutionScope` propagation through `AgentRunner` (Legacy + LangGraph) to `tool_node` / `Dispatcher` with Tenant + Owner + Service isolation (`CR-02`).
- Added duplicate credential secret collision fail-fast in `validate_boot_configuration()` and removed all developer PC absolute paths from `app/` and `tests/` (`CR-03`).
- Propagated Policy-approved `context_budget_tokens` into `tool_node()` inside `LangGraphR1Runtime` (`CR-04`).
- Upgraded SQLite feedback dedup integrity check to verify actual `PRAGMA index_list` + `PRAGMA index_info` + partial `WHERE` clause + NULL counts (`CR-05`).
- Prevented overflowed SSE slow subscribers from silently skipping dropped events to receive a normal `run.completed` terminal event (`CR-06`).
- Added Active Run Admission Control (`429`), automatic finished-session eviction on run create/finish, and unified Status/Cancel/SSE expiration contracts (`CR-07`).
- Re-raised DB exceptions in `mark_incomplete()` so `AsyncTrajectoryWriter` retains Dirty Markers until actual commit, with bounded `incomplete_trajectories` (`CR-08`).
- Added terminal `expected_event_count` completion metadata, referential/count completeness validation in `reconstruct_trajectory()`, and fail-closed `export_training_jsonl()` (`CR-09`).

### Final Release Gate Closure (CP1 `5fe923d` & CP2 `304cf49` — `NG-01` ~ `NG-08`)
- Aligned AGTools deployment contract to single `DOCUMENT_SERVICE_TOKEN` ↔ `AGCORE_AGTOOLS_TOKEN`, enforced `AGTOOLS_ENABLED=false` across all outbound paths, and added read-only `scripts/ops_precheck_r12.sh` (`NG-01`).
- Added `AGCORE_CREDENTIAL_SCOPES` / `AGCORE_CREDENTIAL_SECRETS` binding, unregistered service fail-closed (`unregistered_service`), and run-scoped `current_execution_scope` for dynamic HTTP tools (`NG-03`).
- Wired real `AIGatewayClient.execute(_structured=True)` with `httpx.MockTransport` HTTP fixture tests (`NG-02`).
- Added pre-handler zero-budget gate (`tool_budget_exhausted`), Policy-approved budget propagation, and post-adjustment schema re-validation (`NG-04`).
- Added SQLite feedback dedup integrity check with HTTP `503` fail-closed and read-only `scripts/check_sqlite_dedup.sql` (`NG-05`).
- Prevented duplicate terminal emission on terminal-cursor reconnect and drained intermediate replay events in order (`NG-06`).
- Added bounded finished-session TTL and metadata-only Tombstone (`410 SESSION_EXPIRED` / `404 SESSION_NOT_FOUND`) preserving active runs (`NG-07`).
- Added bounded Dirty Set persistence into existing `incomplete_trajectories` table via existing `AsyncTrajectoryWriter` and strict `export_training_jsonl()` filtering (`NG-08`).

### Stage A — Security & Authentication Closure (`6da21a1`)
- Unified `require_authenticated_caller` across Agent, Document Analysis, Demo, and Config APIs (`FIX-201`).
- Split `ADMIN_SERVICE_TOKENS` vs `SERVICE_TOKENS` preventing role escalation via headers, with multi-key rotation and `TRUSTED_PROXY_SECRET` (`FIX-201`).
- Enforced fail-closed `ConstraintSpec.from_runtime(authenticated=False)` and verified `ExecutionScope` runtime entry (`FIX-202`).
- Added placeholder/retired-token rejection, `AGToolsHttpAdapter` fail-closed headers, and production boot fail-fast (`FIX-203`).
- Isolated unowned legacy trajectory records as admin-only without first-submitter ownership adoption (`FIX-204`).

### Stage B — Execution & Telemetry Integrity (`a65e034`)
- Fixed tool outcome status SSoT to `ToolExecutionTrace.status == "completed"` across SSE, PostCallAdapter, and OutcomeEvent (`FIX-205`).
- Enforced strict `remaining_input_tokens=0` zero-allocation, multi-tool batch sum bound, and `additionalProperties: false` schema validation (`FIX-206`).
- Wired per-invocation `ModelReply(usage=...)` capture, retry usage accumulation, `reasoning_effort` preservation, and duplicate streaming suppression (`FIX-207`).
- Routed feedback directly to atomic SQLite commit with partial `UNIQUE INDEX`, `PRAGMA busy_timeout=5000`, persistent `incomplete_trajectories`, and single-deadline shutdown (`FIX-208`).

### Stage C — Performance & Runtime Compatibility
- Reused process-compiled LangGraph `StateGraph` (`compile_count == 1`) with `RunnableConfig` run-scoped context (-63% P99 latency at 50 concurrent runs) (`FIX-209`).
- Added full production `AgentRunner` cross-runtime replay harness with unused-fixture detection (`FIX-210`).
- Eliminated SSE replay-vs-subscribe race, enforced `max_subscribers`, verified HTTP `410 Gone` API contract, and preserved disconnect-without-cancel policy (`FIX-211`).
- Protected legacy `AgentRunner` against late `model_node`/`tool_node` state mutations via `TerminalStateFence` (`FIX-212`).

## [1.1.0-r1.1] - 2026-11-26 (feature/runtime-r1-corrective)
### Security & Execution Integrity (CP1 — Stage A)
- Added immutable `ValidatedAction` contract exclusively minted by `ActionValidator` (`R1-FIX-01`).
- Added `ExecutionScope` caller authentication, Trajectory/Feedback owner & referential integrity, server-normalized rewards, and duplicate feedback blocking (`R1-FIX-02`).
- Replaced blocking single queue with non-blocking `RunEventBus` supporting `Last-Event-ID` replay and HTTP `410 Gone` expiration (`R1-FIX-03`).
- Added monotonic `HardDeadline` and atomic `TerminalStateFence` blocking post-terminal events and late results (`R1-FIX-04`).
- Removed hardcoded default `AGTOOLS_TOKEN`, set `ConstraintSpec.authenticated=False` default, added profile-based CORS and actual-body size limit middleware (`R1-FIX-05`).

### Policy Execution & Telemetry Isolation (CP2 — Stage B)
- Added typed `DecisionType` and wired validated `reasoning_effort` & `context_budget_tokens` into `model_node` (`R1-FIX-06`).
- Hardened `SAFE_RESPOND` to disable tools without mutating `tool_call_count` and enforced context-budget validation on non-terminal fallbacks (`R1-FIX-07`).
- Added bounded `AsyncTrajectoryWriter` with failure isolation, health metrics, explicit `flush()`, and FastAPI Lifespan shutdown (`R1-FIX-08`).
- Added actual provider `token_usage` capture without fabricating unmeasured rewards (`R1-FIX-09`).

### Compatibility, Governance & Lifecycle (CP3 — Stage C/D)
- Added cross-runtime Legacy vs LangGraph deterministic replay harness with fixture exhaustion fail-fast (`R1-FIX-10`).
- Added pseudo-tool syntax streaming guard and citation propagation in `LangGraphR1Runtime` (`R1-FIX-11`).
- Fixed dynamic tool budget (`reg.parameters`, `remaining=0`), added JSON Schema validation, structured JSON truncation, and read-only/side-effect batch separation (`R1-FIX-12`).
- Upgraded telemetry sanitizer to strict explicit field allowlist (`R1-FIX-13`).
- Added `PolicyRegistry` with independent `ENGINE_VERSION`, run-level pinning, and rollback (`R1-FIX-14`).
