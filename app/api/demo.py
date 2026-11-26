"""Demo Console API Endpoints for Standalone Testing."""
import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core.auth import require_authenticated_caller
from app.gateway.client import authorized_gateway_clients, gateway_client
from app.gateway.direct import DirectLLMClient, SSRFSecurityError, validate_base_url
from app.runtime.runner import runner
from app.schemas.agent import AgentRunOptions, AgentRunRequest
from app.schemas.gateway import GatewayMessage
from app.tools.registry import registry

router = APIRouter(prefix="/demo", tags=["Demo Console"], dependencies=[Depends(require_authenticated_caller)])

class DemoConnectRequest(BaseModel):
    mode: Literal["direct", "gateway"] = "direct"
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    gateway_route: Optional[str] = None

class DemoRunRequest(BaseModel):
    mode: Literal["chat", "agent"] = "agent"
    model_mode: Literal["direct", "gateway"] = "direct"
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    gateway_route: Optional[str] = None
    message: str
    stream: bool = True
    options: Optional[AgentRunOptions] = None


def _resolve_authorized_gateway(route: Optional[str]):
    """Resolve an authorized AI Gateway client strictly from AGCore's provisioned routes."""
    clients = authorized_gateway_clients()
    target = (route or settings.GATEWAY_ROUTE or "").strip()
    if not target or target not in clients:
        return target, None, list(clients.keys())
    return target, clients[target], list(clients.keys())


@router.get("/gateway-routes")
async def list_authorized_gateway_routes():
    """Lists only AI Gateway routes AGCore holds explicit credentials for (no secrets)."""
    clients = authorized_gateway_clients()
    socket_exists = os.path.exists(settings.GATEWAY_SOCKET)
    route_names = list(clients.keys())

    async def _probe(rname: str) -> Dict[str, Any]:
        base_item: Dict[str, Any] = {
            "route": rname,
            "authorized": True,
            "ready": False,
            "model_label": None,
            "context_tokens": None,
            "max_output_tokens": None,
            "reasoning_effort": None,
            "error": None,
        }
        if not socket_exists:
            base_item["error"] = "AI Gateway 소켓 미연결"
            return base_item
        res = await clients[rname].get_route_status(rname)
        if res.get("ok") and isinstance(res.get("data"), dict):
            d = res["data"]
            base_item.update({
                "ready": bool(d.get("ready")),
                "model_label": d.get("model_label"),
                "context_tokens": d.get("context_tokens"),
                "max_output_tokens": d.get("max_output_tokens"),
                "reasoning_effort": d.get("reasoning_effort"),
            })
        else:
            base_item["error"] = res.get("error") or "상태 조회 실패"
        return base_item

    items = await asyncio.gather(*(_probe(r) for r in route_names)) if route_names else []
    return {
        "socket_available": socket_exists,
        "default_route": settings.GATEWAY_ROUTE if settings.GATEWAY_ROUTE in clients else (route_names[0] if route_names else None),
        "routes": items,
    }


@router.post("/connect")
async def test_connection(req: DemoConnectRequest):
    """Verifies connection to either direct model provider or AI Gateway."""
    if req.mode == "gateway":
        # Explicit rule: Do not silently fallback to direct mode.
        target_route, gw_client, allowed = _resolve_authorized_gateway(req.gateway_route)
        if gw_client is None:
            return {
                "success": False,
                "ready": False,
                "status_code": 403,
                "mode": "gateway",
                "route": target_route,
                "message": f"AGCore에 호출 권한이 없는 AI Gateway 라우트입니다: '{target_route}' (허용: {', '.join(allowed) or '없음'})",
            }
        socket_exists = os.path.exists(settings.GATEWAY_SOCKET)
        if not socket_exists:
            return {
                "success": False,
                "ready": False,
                "status_code": 503,
                "mode": "gateway",
                "route": target_route,
                "message": "AI Gateway 소켓(/run/hd2lab-ai-gateway/api.sock)에 연결할 수 없습니다. AI Gateway 상태를 확인하거나 Direct Test Mode를 사용해 주세요.",
            }

        t0 = time.perf_counter()
        res = await gw_client.get_route_status(target_route)
        duration_ms = int((time.perf_counter() - t0) * 1000)
        if not res.get("ok"):
            return {
                "success": False,
                "ready": False,
                "status_code": res.get("status_code") or 502,
                "duration_ms": duration_ms,
                "mode": "gateway",
                "route": target_route,
                "message": f"AI Gateway ({target_route}) 상태 확인 실패: {res.get('error') or '알 수 없는 오류'}",
            }
        data = res.get("data") or {}
        ready = bool(data.get("ready"))
        model_label = data.get("model_label")
        if not ready:
            return {
                "success": False,
                "ready": False,
                "status_code": 200,
                "duration_ms": duration_ms,
                "mode": "gateway",
                "route": target_route,
                "model_label": model_label,
                "message": f"AI Gateway ({target_route}) 라우트가 현재 비활성화 상태입니다.",
            }
        return {
            "success": True,
            "ready": True,
            "status_code": 200,
            "duration_ms": duration_ms,
            "mode": "gateway",
            "route": target_route,
            "model_label": model_label,
            "context_tokens": data.get("context_tokens"),
            "max_output_tokens": data.get("max_output_tokens"),
            "message": f"AI Gateway ({target_route}) 연결 정상 — 활성 모델: {model_label or '미지정'}",
        }

    # Direct Test Mode
    if not req.base_url or not req.model:
        raise HTTPException(status_code=400, detail="Base URL과 Model을 모두 입력해 주세요.")

    try:
        client = DirectLLMClient(
            base_url=req.base_url,
            api_key=req.api_key or "",
            model=req.model,
            allow_localhost=True,
        )
    except SSRFSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"설정 오류: {str(e)}")

    res = await client.test_connection()
    return res

@router.post("/run")
async def run_demo(req: DemoRunRequest, request: Request):
    """Executes either standard Chat or LangGraph Agent with real-time SSE stream."""
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="메시지를 입력해 주세요.")

    # 1. Setup client
    custom_client: Optional[Any] = None
    if req.model_mode == "direct":
        if not req.base_url or not req.model:
            raise HTTPException(status_code=400, detail="Direct Test Mode에서는 Base URL과 Model이 필수입니다.")
        try:
            custom_client = DirectLLMClient(
                base_url=req.base_url,
                api_key=req.api_key or "",
                model=req.model,
                allow_localhost=True,
            )
        except SSRFSecurityError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.model_mode == "gateway":
        target_route, gw_client, allowed = _resolve_authorized_gateway(req.gateway_route)
        if gw_client is None:
            raise HTTPException(
                status_code=403,
                detail=f"AGCore에 호출 권한이 없는 AI Gateway 라우트입니다: '{target_route}' (허용: {', '.join(allowed) or '없음'})",
            )
        if not os.path.exists(settings.GATEWAY_SOCKET):
            raise HTTPException(
                status_code=503,
                detail="AI Gateway 소켓에 연결할 수 없습니다.",
            )
        custom_client = gw_client

    # 2. Chat Mode: Simple direct completion without tools
    if req.mode == "chat":
        return StreamingResponse(
            _stream_chat_mode(req, custom_client),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    # 3. Agent Mode: Use existing AGCore LangGraph Runner
    agent_req = AgentRunRequest(
        agent_id="demo-agent",
        message=req.message,
        stream=True,
        options=req.options,
    )
    session = await runner.create_run(
        agent_req,
        service_id="demo-console",
        custom_client=custom_client,
    )
    await runner.start_run(session)

    return StreamingResponse(
        runner.stream_events(session.state.run_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )

async def _stream_chat_mode(req: DemoRunRequest, client: Optional[DirectLLMClient]):
    """Streams standard LLM chat completion formatted identically to SSE events."""
    run_id = f"chat-{uuid.uuid4().hex[:12]}"
    now = time.time()
    
    # Emit run.started
    yield f"event: run.started\ndata: {json.dumps({'run_id': run_id, 'event_id': f'{run_id}-1', 'timestamp': now, 'status': 'running', 'mode': 'chat'})}\n\n"
    
    target_client = client or gateway_client
    full_text = ""
    error_msg = None

    async def on_delta(token: str):
        nonlocal full_text
        full_text += token
        evt = {
            "run_id": run_id,
            "event_id": f"{run_id}-delta",
            "timestamp": time.time(),
            "status": "running",
            "delta": token,
        }
        # Yield is inside generator so we collect in a queue or direct stream
    
    q: asyncio.Queue[Optional[str]] = asyncio.Queue()

    async def run_chat():
        try:
            msgs = [GatewayMessage(role="user", content=req.message)]
            async def queue_delta(t: str):
                await q.put(t)

            content, _, _ = await target_client.execute(
                messages=msgs,
                tools=None,
                tool_choice="none",
                stream=True,
                on_content_delta=queue_delta,
            )
            await q.put(None)
        except Exception as e:
            await q.put(f"__ERROR__:{str(e)}")

    task = asyncio.create_task(run_chat())
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                break
            if chunk.startswith("__ERROR__:"):
                error_msg = chunk[len("__ERROR__:"):]
                yield f"event: run.failed\ndata: {json.dumps({'run_id': run_id, 'code': 'chat_error', 'message': error_msg})}\n\n"
                return
            
            full_text += chunk
            yield f"event: answer.delta\ndata: {json.dumps({'run_id': run_id, 'delta': chunk})}\n\n"

        yield f"event: run.completed\ndata: {json.dumps({'run_id': run_id, 'answer': full_text, 'total_steps': 1, 'total_tool_calls': 0})}\n\n"
    finally:
        if not task.done():
            task.cancel()

@router.get("/tools")
async def list_demo_tools():
    """Lists registered tools for the demo console."""
    tools = registry.list_tools()
    return {
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "is_read_only": t.is_read_only,
                "active": True,
            }
            for t in tools
        ]
    }
