# HD²Lab Agent Core Platform (AGCore / AGPilot0005)

> **Standalone agentic AI runtime and tool-execution service**
> *FastAPI • LangGraph runtime • AI Gateway adapter • tool dispatcher • SSE tracing • Document Intelligence (AGPilot0006) integration • NVIDIA NIM / OpenAI wire translation*

---

## 1. Overview

**HD²Lab Agent Core Platform (AGCore)** is a reusable backend for the HD2Lab generative-AI services (HD2AI admin/beta/public, Universe and internal tools). It owns the agent loop — model reasoning, tool calls, multi-step planning and run state — behind a standard REST and Server-Sent Events API, so an agent can be added without coupling it to one web UI or one gateway.

### Core architecture principles
- **Independent runtime**: no dependency on a specific web UI or AI gateway; everything is exposed over REST plus SSE.
- **Bounded execution**: `max_tool_calls` (default 16), `max_agent_steps` (default 20), `run_timeout_seconds` (default 180 s), a 12,000-token context budget, a 2,000-token cap per tool result and a 2 MB request-body ceiling prevent runaway loops and token spend.
- **Service separation**: unstructured document parsing, hierarchy and retrieval belong to **AGPilot0006 (Document Intelligence)** and are reached only over its asynchronous REST API.
- **Wire-level protocol compatibility**: the function-name constraint of OpenAI and NVIDIA NIM (`^[a-zA-Z0-9_-]+$`) is absorbed transparently, so `system.echo` and `system_echo` translate in both directions.
- **Dynamic runtime policy**: run limits and per-route token budgets can be read and changed while the service runs, without a restart.

---

## 2. Tools

The registry ships with eight read-only tools:

| Tool | Purpose |
| --- | --- |
| `system.echo` | Return the supplied message unchanged (connectivity probe) |
| `system.get_datetime` | Current server date, time and weekday for a timezone (default `Asia/Seoul`) |
| `math.calculate` | Safe arithmetic and basic maths expressions (no `eval`) |
| `document.get_outline` | Hierarchical outline of a document |
| `document.read_section` | Body of one section |
| `document.search` | Keyword and semantic search returning chunks with page positions |
| `document.read_chunk` | Body, token count and source location of one chunk |
| `document.get_figure_info` | Caption and download URL for a figure, chart or rendered slide |

Document tools call AGPilot0006 through `app/adapters/document.py`. Tools are registered in `app/tools/registry.py` and dispatched through `app/tools/dispatcher.py`; `GET /api/v1/agents` lists the registry contents as the single `general-agent` profile.

---

## 3. API

Base path `/api/v1`.

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service status and AI Gateway connection state |
| `POST` | `/agent/runs` | Create a run: asynchronous `202` by default, or an SSE stream with `stream: true` |
| `GET` | `/agent/runs/{run_id}` | Status, final answer, per-step tool traces and resource usage |
| `GET` | `/agent/runs/{run_id}/events` | SSE event stream for a run |
| `POST` | `/agent/runs/{run_id}/cancel` | Cancel a running agent |
| `GET` | `/agents` | Registered agent profiles and their allowed tools |
| `POST` | `/agent/analyses` | Owner-scoped persistent document analysis (`202`, `context_refs` 1–10) |
| `GET` | `/agent/analyses/{job_id}` | Analysis state, plan progress and answer. Send `Accept: text/event-stream` for updates. |
| `POST` | `/agent/analyses/{job_id}/resume` | Resume a paused analysis within its original budget |
| `POST` | `/agent/analyses/{job_id}/cancel` | Cancel an analysis |
| `GET` | `/config/agent` | Current runtime limits and per-route token budgets |
| `PATCH` | `/config/agent` | Change tool-call / step / timeout limits (hot reload) |
| `PATCH` | `/config/agent/tokens/{route}` | Customise a route's token budget |
| `POST` | `/config/agent/tokens/{route}/reset` | Restore that route's gateway-recommended budgets |
| `POST` | `/config/agent/reset` | Reset the runtime limits to policy defaults |
| `POST` | `/demo/connect` | Pre-flight a model provider (direct NIM/OpenAI or gateway) |
| `POST` | `/demo/run` | Chat or agent execution stream for the bundled demo console |
| `GET` | `/demo/tools` | Full JSON schema of the registered tools |

`AgentRunRequest` accepts `agent_id`, `message`, an optional `session_id`, up to 16 `history` turns, `context_refs`, `analysis_refs` and `options` (`reasoning_effort`, `max_steps`, `max_tool_calls`, `temperature`, `timeout_seconds`). Run status is one of `queued`, `planning`, `running`, `verifying`, `completed`, `needs_input`, `partial`, `paused`, `failed` or `cancelled`.

### SSE events

`run.started`, `run.deferred`, `step.started`, `model.started`, `model.completed`, `reasoning.delta`, `tool.started`, `tool.completed`, `step.completed`, `answer.delta`, `run.completed`, `run.failed`, `run.cancelled`.

Full request/response payloads are in [docs/API_CONTRACT.md](docs/API_CONTRACT.md) and the generated [docs/openapi.json](docs/openapi.json).

### Document tasks

Requests from `hd2ai-admin`, `hd2ai-beta` or `hd2ai-public` that carry `context_refs` or `analysis_refs` create a persistent Task and answer with `run.deferred` plus a job id; the `/agent/analyses/*` endpoints then read, resume or cancel it. With `Accept: text/event-stream`, the status endpoint emits `analysis.snapshot` events. Each event replaces the previous snapshot, including its answer. The stream ends on a terminal state. Clients can recover using the same job ID via the JSON status endpoint. The task runtime lives in `app/runtime/tasks/` (typed task/plan/budget/evidence state, planner, writer, verifier, answer composition and presentation). A final answer that fails verification stays `partial` or `needs_input` rather than publishing unverified or concatenated group text.

---

## 4. Authentication and access control

- **Administrator SSO proxy (production, `/admin/agcore/`)**: Nginx injects `X-HD2Lab-User`, `X-HD2Lab-Role` and `X-HD2Lab-Service`; state-changing requests must also carry a valid `X-CSRF-Token`.
- **Service token (direct API)**: `Authorization: Bearer <service token>` from `AGCORE_SERVICE_TOKENS`.
- **Input and resource limits**: bodies over 2 MB return `413`, private/local addresses are refused (SSRF protection), and responses set `X-Content-Type-Options: nosniff` and `X-Frame-Options: DENY`.

---

## 5. Running locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8200
```

- **Demo console:** http://127.0.0.1:8200/demo/ — chat and agent modes with sample prompts, a model-connection panel (NVIDIA preset, OpenAI preset or gateway), a runtime-config panel and the tool-schema browser
- **Swagger UI:** http://127.0.0.1:8200/docs (HD2Lab dark palette, works behind the `/admin/agcore` prefix)
- **OpenAPI schema:** http://127.0.0.1:8200/openapi.json

The bundled demo console is prebuilt into `demo/dist/`; rebuild it with `cd demo && npm install && npm run build`.

### Tests

```bash
pytest tests/ -v
```

The suite (`tests/`, 16 modules, `pytest-asyncio` auto mode) covers the API surface, gateway adapter and credential handling, wire translation, tools, context, the document adapter and analysis flow, the task orchestrator, answer composition, the beta baseline and token-contract sync.

---

## 6. Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGCORE_HOST`, `AGCORE_PORT` | `127.0.0.1`, `8200` | Bind address |
| `AGCORE_GATEWAY_URL`, `AGCORE_GATEWAY_SOCKET` | `http://127.0.0.1:8080`, `/run/hd2lab-ai-gateway/api.sock` | AI Gateway HTTP and Unix-socket endpoints |
| `AGCORE_GATEWAY_ROUTE`, `AGCORE_GATEWAY_TOKEN` | `hd2ai-admin`, — | Gateway route and its token |
| `AGCORE_MAX_TOOL_CALLS`, `AGCORE_MAX_AGENT_STEPS` | `16`, `20` | Run limits |
| `AGCORE_CONTEXT_BUDGET_TOKENS`, `AGCORE_MAX_TOOL_RESULT_TOKENS` | `12000`, `2000` | Context and tool-result ceilings |
| `AGCORE_TOOL_TIMEOUT_SECONDS`, `AGCORE_RUN_TIMEOUT_SECONDS` | `30`, `180` | Tool and run timeouts |
| `AGCORE_SERVICE_TOKENS` | — | Comma-separated tokens accepted as service callers |
| `AGCORE_ENV`, `AGCORE_ALLOW_MOCK_FALLBACK` | `development`, `true` | Environment and mock-provider policy |
| `AGCORE_AGTOOLS_URL`, `AGCORE_AGTOOLS_TENANT`, `AGCORE_AGTOOLS_TOKEN` | AGPilot0006 URL, `default_tenant`, — | Document Intelligence connection |
| `AGCORE_ANALYSIS_DIR`, `AGCORE_ANALYSIS_MAX_CHUNKS`, `AGCORE_ANALYSIS_MAX_MODEL_CALLS`, `AGCORE_ANALYSIS_MAX_SECONDS` | `/var/lib/hd2lab-agcore/analysis`, `1000`, `500`, `7200` | Persistent analysis storage and budget |
| `AGCORE_TASK_MAX_DOCUMENT_CALLS`, `AGCORE_TASK_MAX_REPLANS`, `AGCORE_TASK_MAX_ANSWER_REPAIRS` | `5000`, `2`, `2` | Task budget and repair limits |

In production the gateway token is injected either as `AGCORE_GATEWAY_TOKEN` or through systemd `LoadCredential=agcore_gateway_token:…` (read from `CREDENTIALS_DIRECTORY/agcore_gateway_token`). The gateway Unix socket is owned by the `www-data` group, so the unit needs `SupplementaryGroups=www-data`, and `AGCORE_ALLOW_MOCK_FALLBACK=false` should be set in production.

### Service management (Raspberry Pi host)

```bash
sudo systemctl status hd2lab-agcore.service
sudo systemctl restart hd2lab-agcore.service
```

---

## 7. Documentation

- [API contract (docs/API_CONTRACT.md)](docs/API_CONTRACT.md)
- [Architecture (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)
- [AGPilot0006 tool adapter contract](docs/TOOL_ADAPTER_CONTRACT_AGPILOT0006.md)
- [Demo console guide (docs/DEMO_CONSOLE.md)](docs/DEMO_CONSOLE.md)
- [Gateway compatibility (docs/GATEWAY_COMPATIBILITY.md)](docs/GATEWAY_COMPATIBILITY.md)
- [HD2AI integration (docs/HD2AI_INTEGRATION.md)](docs/HD2AI_INTEGRATION.md)
- [Operations (docs/OPERATIONS.md)](docs/OPERATIONS.md) and [QA (docs/QA.md)](docs/QA.md)
- [OpenAPI schema (docs/openapi.json)](docs/openapi.json)

### Answer model attribution

Completed run events, run status and document analysis status expose an optional `model_label`. It identifies the final answer model reported by Gateway, including failover. Planning and verification calls never override attribution. Unknown or mixed attribution is null.
