# Changelog

## [1.2.0-r1.2] - 2026-11-26 (feature/runtime-r1-corrective)
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
