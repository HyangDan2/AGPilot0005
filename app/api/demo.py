"""Demo Console API Endpoints for Standalone Testing."""
import asyncio
import json
import os
import time
import uuid
from typing import Any, Dict, List, Literal, Optional
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.gateway.client import gateway_client
from app.gateway.direct import DirectLLMClient, SSRFSecurityError, validate_base_url
from app.runtime.runner import runner
from app.schemas.agent import AgentRunOptions, AgentRunRequest
from app.schemas.gateway import GatewayMessage
from app.tools.registry import registry

router = APIRouter(prefix="/demo", tags=["Demo Console"])

class DemoConnectRequest(BaseModel):
    mode: Literal["direct", "gateway"] = "direct"
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None

class DemoRunRequest(BaseModel):
    mode: Literal["chat", "agent"] = "agent"
    model_mode: Literal["direct", "gateway"] = "direct"
    base_url: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    message: str
    stream: bool = True
    options: Optional[AgentRunOptions] = None

@router.post("/connect")
async def test_connection(req: DemoConnectRequest):
    """Verifies connection to either direct model provider or AI Gateway."""
    if req.mode == "gateway":
        # Check existing AI Gateway status
        # Explicit rule: Do not silently fallback to direct mode.
        socket_exists = os.path.exists(settings.GATEWAY_SOCKET)
        is_mock = gateway_client.mock_mode
        if socket_exists:
            return {
                "success": False,
                "ready": False,
                "mode": "gateway",
                "message": "기존 AI Gateway가 감지되었으나, Tool Calling 내부 계약(schema3 tools 호환)이 아직 활성화되지 않았습니다. 패치 적용 전까지 Direct Test Mode를 권장합니다.",
            }
        else:
            return {
                "success": False,
                "ready": False,
                "mode": "gateway",
                "message": "기존 AI Gateway 소켓(/run/hd2lab-ai-gateway/api.sock)에 연결할 수 없습니다. AI Gateway 상태를 확인하거나 Direct Test Mode를 사용해 주세요.",
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
    custom_client: Optional[DirectLLMClient] = None
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
        if not os.path.exists(settings.GATEWAY_SOCKET):
            raise HTTPException(
                status_code=503,
                detail="기존 AI Gateway에 연결할 수 없으며 Tool Calling이 아직 활성화되지 않았습니다.",
            )

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
