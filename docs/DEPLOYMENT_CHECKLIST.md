# AGCore Phase R1.2 Final — Production Deployment Checklist

> **Mandatory Runtime Constraint**: Current in-memory `AgentRunner` session & `RunEventBus` state is process-local. Production MUST run with **`uvicorn --workers 1`** until external session persistence is implemented in a future phase.

## 1. Real AGCore ↔ AGTools Authentication Contract (`NG-01`)
- **AGCore (`AGPilot0005`)**: `AGCORE_AGTOOLS_TOKEN` (single active outbound token; empty, whitespace, placeholder, or retired default fails boot when `AGCORE_AGTOOLS_ENABLED=true`).
- **AGTools (`AGPilot0006` main verified contract)**: single `DOCUMENT_SERVICE_TOKEN` environment variable.
- **Important Notice (No Zero-Downtime Rotation)**: Because current AGTools (`AGPilot0006`) validates a **single** `DOCUMENT_SERVICE_TOKEN` (multi-token support is deferred to a separate AGPilot0006 work order), rotating the AGTools token requires a **brief coordinated maintenance window**:
  1. Run read-only preflight `scripts/ops_precheck_r12.sh` to verify actual deployed AGTools version and active env contract.
  2. Generate a new 32-byte random token (`openssl rand -hex 32`).
  3. Update `DOCUMENT_SERVICE_TOKEN` in AGTools env and `AGCORE_AGTOOLS_TOKEN` in AGCore env (`8200` and `8201`).
  4. Restart `hd2lab-agtools`, `hd2lab-agcore`, and `hd2lab-agcore-public` together within the maintenance window.
  5. Verify `GET /api/v1/health`, `POST /api/v1/config/tools/sync`, and live smoke tests.
  6. **Rollback**: Generate/apply a fresh previously-unexposed token on both sides (do **NOT** roll back to the retired exposed default token).
- **AGTools Disabled Mode**: Setting `AGCORE_AGTOOLS_ENABLED=false` cleanly blocks outbound calls across Document Adapter, Provider Sync, Dynamic HTTP Tool Registration, and Tool Dispatcher (`0` outbound HTTP requests).

## 2. Credential Scope Binding & HD2AI Beta Compatibility (`NG-03`)
- **HD2AI Beta Call Path**: Browser → Nginx `/admin/agcore/` (Admin SSO Trusted Proxy injecting `X-HD2Lab-User`, `X-HD2Lab-Role`) → AGCore `127.0.0.1:8200`.
- **Required Nginx Addition Before Production Cutover**:
  1. Strip any client-supplied `X-Trusted-Proxy-Secret`, `X-HD2Lab-User`, `X-HD2Lab-Role`, `X-HD2Lab-Service` at the edge.
  2. Inject `proxy_set_header X-Trusted-Proxy-Secret "<rotated-proxy-secret>";` on `/admin/agcore/` and `/HD2AI/agent/` blocks matching `AGCORE_TRUSTED_PROXY_SECRET`.
- **Credential Scope Configuration (Service-to-Service / BFF / Future Desktop)**:
  - `AGCORE_CREDENTIAL_SCOPES` (JSON keyed by `credential_id` — NEVER raw secret!):
    ```json
    {
      "beta-bff": {
        "allowed_services": ["hd2ai-beta"],
        "allowed_roles": ["service"],
        "allowed_tenants": ["default_tenant"],
        "owner_delegation": false,
        "fixed_owner": "beta-bff-service"
      }
    }
    ```
  - `AGCORE_CREDENTIAL_SECRETS` (JSON mapping `credential_id` → list of `[current, previous]` secrets for zero-downtime caller key rotation).
  - Unregistered services return `403` / `unregistered_service` (no implicit fallback to `public-safe`).

## 3. Required Production Environment Variables (Boot Fail-Fast)
- `AGCORE_ENV=production`
- `AGCORE_AGTOOLS_ENABLED=true|false`
- `AGCORE_AGTOOLS_TOKEN=<rotated-secret>` (required when `AGCORE_AGTOOLS_ENABLED=true`)
- `AGCORE_TRUSTED_PROXY_SECRET=<secret>` and/or `AGCORE_CREDENTIAL_SCOPES` + `AGCORE_CREDENTIAL_SECRETS`
- `AGCORE_RUNTIME_ENGINE=legacy` (keep `legacy` default; LangGraph cutover is separate R1.3 approval)
- `AGCORE_SQLITE_BUSY_TIMEOUT_MS=5000`

## 4. SQLite Backup, Preflight, Migration & Rollback (`NG-05`)
1. **Preflight (Read-Only)**: Run `sqlite3 /var/lib/hd2lab-agcore/trajectory.db < scripts/check_sqlite_dedup.sql` to inspect schema, `uq_trajectory_feedback_dedup` index, NULL dedup keys, and logical duplicate counts (aggregate counts only, no raw user data).
2. **Backup**: `sqlite3 /var/lib/hd2lab-agcore/trajectory.db ".backup '/var/backups/hd2lab/trajectory-pre-r1.2.db'"`
3. **Migration**: Startup idempotently adds missing columns/index. If pre-existing duplicate feedback rows prevent unique index creation, feedback writes fail closed with **HTTP `503`** while regular agent runs continue unaffected.
4. **Rollback**: Stop AGCore, restore previous release symlink and `trajectory-pre-r1.2.db`, restart service.

## 5. HD2AI Frontend Compatibility (`404` / `410` / `503`)
- **HD2AI Frontend Compatibility (`404` / `410` / `429` / `503`)**
  - `agentClient.ts` treats non-2xx on `GET /runs/{id}`, `POST /runs`, `/cancel`, or `/events` as user-visible error message; `submitRunFeedback` returns `false` on `409`/`503` and shows toast `"피드백 전송에 실패했습니다."` without breaking chat.
  - Session expiration returns **HTTP `410` (`SESSION_EXPIRED`)** for verified owners and **HTTP `404` (`SESSION_NOT_FOUND`)** for missing/unauthorized sessions consistently across Status, Cancel, and SSE; SSE cursor expiration returns **HTTP `410` (`event_replay_expired`)**.
  - Active run admission overflow returns **HTTP `429` (`active_run_limit_exceeded`)** without affecting existing active runs.
  - Duplicate credential secrets across `AGCORE_CREDENTIAL_SECRETS` fail fast at boot (`duplicate_credential_secret`).

## 6. SSE Buffering, Timeout Hierarchy & Graceful Shutdown
- Nginx: `proxy_buffering off;`, `proxy_cache off;`, `proxy_read_timeout 300s;` > AGCore `RUN_TIMEOUT_SECONDS` (`180s`) > Tool `TOOL_TIMEOUT_SECONDS` (`30s`, capped to remaining deadline).
- FastAPI `lifespan` drains `AsyncTrajectoryWriter` (events + dirty markers) under a single `5.0s` deadline (`TimeoutStopSec=15s`).
