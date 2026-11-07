"""Agent Execution and Tracing REST API Endpoints."""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from app.runtime.runner import runner
from app.schemas.agent import AgentRunRequest, AgentRunResponse
from app.tools.registry import registry

router = APIRouter(prefix="/agent", tags=["Agent"])

@router.post("/runs")
async def create_agent_run(req: AgentRunRequest, request: Request):
    """Initiates an agent run session."""
    # Service identity extracted from header or default
    service_id = request.headers.get("X-HD2Lab-Service", "hd2ai-admin")
    session = await runner.create_run(req, service_id=service_id)
    await runner.start_run(session)
    
    if req.stream:
        return StreamingResponse(
            runner.stream_events(session.state.run_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )
        
    return JSONResponse(
        status_code=202,
        content={
            "run_id": session.state.run_id,
            "agent_id": req.agent_id,
            "session_id": session.state.session_id,
            "status": session.state.status,
            "created_at": session.created_at,
        },
    )

@router.get("/runs/{run_id}", response_model=AgentRunResponse)
async def get_agent_run(run_id: str):
    """Retrieves current run status, final answer, and execution traces."""
    session = runner.get_session(run_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        
    state = session.state
    return AgentRunResponse(
        run_id=state.run_id,
        agent_id="general-agent",
        session_id=state.session_id,
        status=state.status,
        answer=state.final_answer,
        traces=state.traces,
        usage={
            "steps": state.step_count,
            "tool_calls": state.tool_call_count,
            "duration_seconds": round(session.finished_at - session.created_at, 2) if session.finished_at else None,
        },
        created_at=session.created_at,
        finished_at=session.finished_at,
        error=state.error,
    )

@router.get("/runs/{run_id}/events")
async def get_agent_events(run_id: str):
    """Streams SSE events for a specific run."""
    session = runner.get_session(run_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found.")
        
    return StreamingResponse(
        runner.stream_events(run_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )

@router.post("/runs/{run_id}/cancel")
async def cancel_agent_run(run_id: str):
    """Cancels an in-progress agent run."""
    success = await runner.cancel_run(run_id)
    if not success:
        raise HTTPException(status_code=400, detail="Cannot cancel run (already finished or not found).")
    return {"run_id": run_id, "status": "cancelled", "message": "Run cancelled."}

@router.get("/agents")
async def list_agents():
    """Lists registered agents and their allowed tools."""
    tools = registry.list_tools()
    return {
        "agents": [
            {
                "id": "general-agent",
                "name": "General Assistant Agent",
                "description": "다양한 도구를 활용할 수 있는 범용 에이전트",
                "allowed_tools": [t.name for t in tools],
            }
        ]
    }
