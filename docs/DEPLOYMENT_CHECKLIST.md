# AGCore Phase R1.2 — Production Deployment Checklist

> **Mandatory Runtime Constraint**: Current in-memory `AgentRunner` session & `RunEventBus` state is process-local. Production MUST run with **`uvicorn --workers 1`** until external session persistence is implemented in a future phase.

## 1. HD2AI & Reverse Proxy Header Compatibility
- Existing Nginx (`/admin/agcore/` and `/HD2AI/agent/runs*`) strips external `Authorization`/`Cookie` and injects `X-HD2Lab-User`, `X-HD2Lab-Service`, `X-HD2Lab-Role`.
- **Required Nginx Addition Before Production Cutover**:
  1. Strip any client-supplied `X-Trusted-Proxy-Secret`, `X-HD2Lab-User`, `X-HD2Lab-Role`, `X-HD2Lab-Service` at the edge.
  2. Inject `proxy_set_header X-Trusted-Proxy-Secret "<rotated-proxy-secret>";` on `/admin/agcore/` and `/HD2AI/agent/` blocks matching `AGCORE_TRUSTED_PROXY_SECRET`.
- **SSE Proxy Buffering**: Keep `proxy_buffering off;`, `proxy_cache off;`, and `X-Accel-Buffering: no` + `Cache-Control: no-store`.

## 2. Service Token Role Scoping & Key Rotation
- `AGCORE_SERVICE_TOKENS`: comma-separated standard service tokens (`current,previous`) — **cannot** access `/config/*` or claim `admin`/`admiral`.
- `AGCORE_ADMIN_SERVICE_TOKENS`: comma-separated admin tokens (`current,previous`) — required for `/config/tools/*`, `/config/agent/*`, and unowned legacy trajectory inspection.

## 3. Manual AGCore ↔ AGTools Secret Rotation Procedure (Zero-Downtime)
1. Generate a new 32-byte random token (`openssl rand -hex 32`).
2. Add the new token alongside the old token in AGTools (`AGTOOLS_SERVICE_TOKENS="new,old"`) and reload `hd2lab-agtools`.
3. Set `AGCORE_AGTOOLS_TOKEN="<new>"` in Pi `/etc/hd2lab-agcore/*.env` (both `8200` and `8201`) and restart `hd2lab-agcore` & `hd2lab-agcore-public`.
4. Verify live `POST /api/v1/config/tools/sync` (`200 OK`) and document/web search smoke tests.
5. Remove the retired token from AGTools and reload `hd2lab-agtools`.
6. **Rollback**: Re-add previous token in AGTools and revert `AGCORE_AGTOOLS_TOKEN` in AGCore env.

## 4. Required Production Environment Variables (Boot Fail-Fast)
- `AGCORE_ENV=production`
- `AGCORE_AGTOOLS_TOKEN=<rotated-secret>` (required when `AGCORE_AGTOOLS_ENABLED=true`; empty/whitespace/placeholder fails boot)
- `AGCORE_TRUSTED_PROXY_SECRET=<secret>` and/or `AGCORE_SERVICE_TOKENS` + `AGCORE_ADMIN_SERVICE_TOKENS`
- `AGCORE_RUNTIME_ENGINE=legacy` (keep `legacy` default until separate cutover approval)
- `AGCORE_SQLITE_BUSY_TIMEOUT_MS=5000`

## 5. SQLite Backup, Migration & Rollback
1. **Backup**: `sqlite3 /var/lib/hd2lab-agcore/trajectory.db ".backup '/var/backups/hd2lab/trajectory-pre-r1.2.db'"`
2. **Migration**: Startup automatically runs idempotent `ALTER TABLE` (`owner_id`, `service_id`, `feedback_dedup_key`), creates `incomplete_trajectories`, and creates `uq_trajectory_feedback_dedup` (if pre-existing duplicate feedback rows exist, index creation logs an error without deleting any rows).
3. **Rollback**: Stop AGCore, restore previous release symlink and `trajectory-pre-r1.2.db`, restart service.

## 6. Timeout Hierarchy & Graceful Shutdown
- Nginx `proxy_read_timeout` (`300s`) > AGCore `RUN_TIMEOUT_SECONDS` (`180s`) > Tool `TOOL_TIMEOUT_SECONDS` (`30s`, capped to remaining run deadline).
- FastAPI `lifespan` drains `AsyncTrajectoryWriter` under a single `5.0s` shutdown deadline (`TimeoutStopSec=15s` in systemd).
