"""Health check endpoint."""
from fastapi import APIRouter
from app.gateway.client import gateway_client

router = APIRouter(tags=["Health"])

@router.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "service": "AGCore",
        "version": "1.3.0",
        "runtime_engine": "langgraph_r1",
        "gateway_connected": not gateway_client.mock_mode,
    }
