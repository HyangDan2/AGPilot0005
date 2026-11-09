"""AGCore Platform FastAPI Application Entrypoint."""
import time
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from app.api.agent import router as agent_router
from app.api.demo import router as demo_router
from app.api.health import router as health_router
from app.config import settings

app = FastAPI(
    title="HD2Lab Agent Core Platform (AGCore)",
    version="1.0.0-mvp",
    description="범용 Agentic AI Runtime 백엔드 서비스",
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS Middleware (Local dev & admin domain)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_and_limit_middleware(request: Request, call_next):
    # Enforce request body size limits (2MB max)
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 2 * 1024 * 1024:
        return JSONResponse(status_code=413, content={"detail": "Request payload too large (max 2MB)."})
        
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

# Include API Routers under /api/v1
app.include_router(health_router, prefix="/api/v1")
app.include_router(agent_router, prefix="/api/v1")
app.include_router(demo_router, prefix="/api/v1")

# Mount built demo console frontend if dist directory exists
import os
from pathlib import Path
from fastapi.staticfiles import StaticFiles

demo_dist = Path(__file__).resolve().parent.parent / "demo" / "dist"
if demo_dist.exists():
    app.mount("/demo", StaticFiles(directory=str(demo_dist), html=True), name="demo")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)
