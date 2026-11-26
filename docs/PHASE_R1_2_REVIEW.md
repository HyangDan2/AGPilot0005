# AGPilot0005 — Runtime Hardening R1.2 Review

- **Branch**: `feature/runtime-r1-corrective`
- **Baseline**: `025558666367c03e642c0cd0497c35b4a91d7a1a`
- **Scope**: `FIX-201` ~ `FIX-212` (12 Independent QA closures)
- **Out-of-Scope Working Tree Files**: 7 pre-existing uncommitted TaskOrchestrator files preserved untouched.

## Stage A — Security & Authentication Closure (`6da21a1`)
- **FIX-201**: Unified `require_authenticated_caller` across `/agent/runs*`, `/agent/analyses*`, `/demo/*`, `/config/*`. Split `ADMIN_SERVICE_TOKENS` vs `SERVICE_TOKENS` so standard tokens cannot escalate to `admin`/`admiral` via HTTP headers. Verified comma-separated multi-token rotation and `TRUSTED_PROXY_SECRET` guard.
- **FIX-202**: Removed `authenticated=True` default from `ConstraintSpec.from_runtime()` and `runner.py`. Runtime now derives authentication strictly from verified `ExecutionScope` and fails closed on unauthenticated entry.
- **FIX-203**: Added placeholder/retired-token rejection in `is_agtools_token_valid()`, fail-closed guard in `AGToolsHttpAdapter._headers()`, and `validate_boot_configuration()` in FastAPI `lifespan` (logs only missing env var names, never secrets).
- **FIX-204**: Isolated unowned legacy trajectory records (`owner_id=NULL` or `service_id=NULL`) as strictly admin-only (`403` for non-admin reads and feedback; never adopts first feedback submitter).

## Stage B — Execution & Telemetry Integrity (`a65e034`)
- **FIX-205**: Replaced erroneous `"success"` status check with `ToolExecutionTrace.status` SSoT (`"completed"` / `"failed"` / `"running"`) across SSE `tool.completed`, `PostCallAdapter`, and `OutcomeEvent`.
- **FIX-206**: Removed 256-token floor on `remaining_input_tokens=0` (strictly `0` bytes allocated), bounded multi-tool batch sum `<= remaining`, enforced `additionalProperties: false`, and preserved structured JSON on budget exhaustion.
- **FIX-207**: Wired per-invocation `ModelReply(usage=...)` (`return_reply=True`) into `model_node()` with `ModelUsage` normalization, overflow retry usage accumulation, `reasoning_effort` preservation, and duplicate streaming delta suppression.
- **FIX-208**: Routed `POST /feedback` directly to atomic SQLite commit (`asyncio.to_thread(append_feedback_atomic)`) backed by partial `UNIQUE INDEX` (`uq_trajectory_feedback_dedup`) normalizing `NULL` `decision_id` and grouping `thumbs_up`/`thumbs_down` under `user_rating`. Added configurable `PRAGMA busy_timeout=5000`, persistent `incomplete_trajectories` table, and single-deadline `AsyncTrajectoryWriter.shutdown()`.

## Stage C — Performance & Runtime Compatibility
- **FIX-209**: Compiled LangGraph `StateGraph` once per process (`_get_compiled_graph()`, `compile_count == 1`) with run-scoped dependencies injected via `RunnableConfig["configurable"]["run_ctx"]`. Reduced concurrent-50 P99 latency by **63%** (`2,538ms → 930ms`) and peak memory by **27%** (`5,573 KB → 4,057 KB`) with 50-run isolation verified.
- **FIX-210**: Added `run_agent_runner_replay()` driving production `AgentRunner` under both `legacy` and `langgraph_r1` with identical fixtures and unused-fixture detection.
- **FIX-211**: Subscribed before replay snapshot in `RunEventBus.stream()` to prevent mid-replay terminal loss, enforced `max_subscribers=32`, verified real API HTTP `410 Gone` (`event_replay_expired`), and preserved disconnect-without-cancel semantics.
- **FIX-212**: Added `session.fence.is_terminal` guards before/after `model_node` and `tool_node` in legacy `AgentRunner` to block late state mutation and duplicate terminal events.

## Final Release Gate Closure (`NG-01` ~ `NG-08`)
- **CP1 (`5fe923d`) — `NG-01`, `NG-03`**:
  - `NG-01`: Aligned `docs/DEPLOYMENT_CHECKLIST.md` to real single-token contract (`AGCORE_AGTOOLS_TOKEN` ↔ `DOCUMENT_SERVICE_TOKEN`, removed unsupported `AGTOOLS_SERVICE_TOKENS`), enforced `AGTOOLS_ENABLED=false` across Adapter/Sync/Registration/Dispatcher (`0` outbound calls), added read-only `scripts/ops_precheck_r12.sh`.
  - `NG-03`: Added `AGCORE_CREDENTIAL_SCOPES` + `AGCORE_CREDENTIAL_SECRETS` (secret separated from ID, rotation supported, `owner_delegation=false` default), blocked unregistered service fallback to `public-safe` (`unregistered_service`), and wired verified `current_execution_scope` `ContextVar` to dynamic HTTP tools.
- **CP2 — `NG-02`, `NG-04` ~ `NG-08`**:
  - `NG-02`: Wired real `AIGatewayClient.execute(_structured=True)` and verified Non-streaming + Streaming + concurrent isolation via `httpx.MockTransport`.
  - `NG-04`: Enforced pre-handler zero-budget gate (`tool_budget_exhausted`, `0` handler calls), passed Policy-approved budget into `tool_node` / `dispatcher`, and re-validated adjusted arguments.
  - `NG-05`: Verified `uq_trajectory_feedback_dedup` index + `0` NULL dedup keys at init; fail-closed HTTP `503` (`FeedbackIntegrityError`) when unverified; added read-only `scripts/check_sqlite_dedup.sql`.
  - `NG-06`: Prevented duplicate terminal event on terminal-cursor reconnect and drained intermediate queued events before terminal delivery.
  - `NG-07`: Added bounded finished-session TTL + metadata-only `_SessionTombstone` returning HTTP `410` (`SESSION_EXPIRED`) for verified owners and HTTP `404` (`SESSION_NOT_FOUND`) for missing/unauthorized callers while preserving active runs.
  - `NG-08`: Reused existing `AsyncTrajectoryWriter` + `incomplete_trajectories` table with bounded `dirty_trajectories` set (bypassing saturated event queue, cleared only after DB commit) and strict `eligible_for_training` + `export_training_jsonl()` filtering.
