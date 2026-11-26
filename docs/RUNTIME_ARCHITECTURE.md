# AGCore Runtime Architecture & Contracts (Phase R1.1)

## 1. Execution Responsibility Boundary
- **Caller Auth (`app/core/auth.py`)**: Resolves verified `ExecutionScope` (Service Token / Trusted Proxy / Local Dev). Production rejects unauthenticated requests, forged headers, empty/whitespace tokens, and implicit mock fallback.
- **ConstraintSpec (`app/runtime/policy/constraints.py`)**: Immutable SSOT for permissions, budgets, and limits (`authenticated=False` fail-closed default).
- **AGPolicy (`app/runtime/policy/ag_policy.py`)**: Read-only proposer receiving frozen `StateView` + `ConstraintSpec`. Cannot mutate runtime state or execute tools/gateway calls.
- **ActionValidator & ValidatedAction (`app/runtime/policy/validator.py`)**: Exclusively mints immutable `ValidatedAction` bound to exact `tool_call_id`, `tool_name`, frozen arguments, actual call count, scope, and deadline. Re-validates all fallback actions and prevents infinite retry loops.
- **HardDeadline & TerminalStateFence (`app/runtime/fence.py`)**: Monotonic run-wide deadline capping child calls + atomic single-confirmation terminal state latch blocking late callbacks and duplicate terminal events.
- **RunEventBus (`app/runtime/event_bus.py`)**: Decouples runtime emission from SSE subscribers. Bounded replay journal + separate terminal event retention + `Last-Event-ID` validation returning HTTP `410 Gone` (`event_replay_expired`) before streaming begins.
- **Trajectory Event Store & Async Writer (`app/tracing/`)**: SQLite WAL append-only store (`BEFORE UPDATE`/`BEFORE DELETE` triggers), strict field-allowlist sanitizer (`sanitizer.py`), bounded non-blocking `AsyncTrajectoryWriter` with explicit `flush()` and FastAPI Lifespan graceful shutdown.
- **Policy Lifecycle (`app/runtime/policy/registry.py`)**: Separates `ENGINE_VERSION` (`1.1.0-r1.1`) from `AGPolicy.version`, supports run-level pinning and rollback while preserving immediate Live ACL enforcement.
