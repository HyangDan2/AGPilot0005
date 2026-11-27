"""AGCore Platform FastAPI Application Entrypoint."""
import os
import time
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from app.api.agent import router as agent_router
from app.api.document_analysis import router as document_analysis_router
from app.api.demo import router as demo_router
from app.api.health import router as health_router
from app.api.config import router as config_router
from app.api.tools_admin import router as tools_admin_router
from app.api.canvas import router as canvas_router
from app.tools.policy import tool_policy
from app.tools.registry import registry
from app.config import settings
from app.core.runtime_config import sync_contracts_from_gateway
from app.gateway.client import gateway_client

BASE_PATH = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(_app: FastAPI):
    from app.config import validate_boot_configuration
    validate_boot_configuration()
    await sync_contracts_from_gateway(gateway_client, [gateway_client.route, "hd2ai-admin", "hd2ai-public"])
    await tool_policy.sync_all(registry)
    try:
        yield
    finally:
        from app.tracing.store import shutdown_default_trajectory_logger
        shutdown_default_trajectory_logger(timeout_seconds=5.0)


app = FastAPI(
    title="HD2Lab Agent Core Platform (AGCore)",
    version="1.3.0",
    description="범용 Agentic AI Runtime 및 Tool Execution 백엔드 서비스",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# Deployment Profile-based CORS Middleware (R1-FIX-05 C.1)
_cors_origins = [o.strip() for o in settings.CORS_ALLOW_ORIGINS.split(",") if o.strip()] or ["https://hd2lab.duckdns.org"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=("*" not in _cors_origins),
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

@app.middleware("http")
async def security_and_limit_middleware(request: Request, call_next):
    # R1-FIX-05 C.2-C.4: Validate Content-Length safely AND enforce actual received body size
    max_bytes = settings.MAX_REQUEST_BODY_BYTES
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            parsed_len = int(content_length.strip())
            if parsed_len < 0:
                return JSONResponse(status_code=400, content={"detail": "invalid_content_length"})
        except ValueError:
            return JSONResponse(status_code=400, content={"detail": "invalid_content_length"})
        if parsed_len > max_bytes:
            return JSONResponse(status_code=413, content={"detail": "Request payload too large."})

    if request.method in ("POST", "PUT", "PATCH"):
        body = await request.body()
        if len(body) > max_bytes:
            return JSONResponse(status_code=413, content={"detail": "Request payload too large."})

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    return response

# Brand Favicons and Assets
@app.get("/favicon.ico", include_in_schema=False)
async def favicon_ico():
    path = BASE_PATH / "favicon.ico"
    return FileResponse(str(path)) if path.exists() else JSONResponse(status_code=404, content={"detail": "Not Found"})

@app.get("/favicon.svg", include_in_schema=False)
async def favicon_svg():
    path = BASE_PATH / "favicon.svg"
    return FileResponse(str(path), media_type="image/svg+xml") if path.exists() else JSONResponse(status_code=404, content={"detail": "Not Found"})

# Custom Styled Swagger UI (Supports reverse proxy prefix /admin/agcore)
@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html(request: Request):
    """Custom styled Swagger documentation adhering to HD2Lab Dark Palette."""
    orig_uri = request.headers.get("x-original-uri", "")
    forwarded_prefix = request.headers.get("x-forwarded-prefix", "")
    prefix = ""
    if "/admin/agcore" in orig_uri or forwarded_prefix.startswith("/admin/agcore") or request.url.path.startswith("/admin/agcore") or "agcore" in str(request.headers.get("referer", "")):
        prefix = "/admin/agcore"
    openapi_url = f"{prefix}/openapi.json" if prefix else "./openapi.json"
    swagger_favicon = f"{prefix}/favicon.svg" if prefix else "./favicon.svg"
    return get_swagger_ui_html(
        openapi_url=openapi_url,
        title="HD2Lab Agent Core Platform (AGCore) — API Docs",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_favicon_url=swagger_favicon,
    )

# Include API Routers under /api/v1
app.include_router(health_router, prefix="/api/v1")
app.include_router(config_router, prefix="/api/v1")
app.include_router(tools_admin_router, prefix="/api/v1")
app.include_router(agent_router, prefix="/api/v1")
app.include_router(document_analysis_router, prefix="/api/v1")
app.include_router(canvas_router, prefix="/api/v1")
app.include_router(demo_router, prefix="/api/v1")

# Mount built demo console frontend if dist directory exists
demo_dist = BASE_PATH / "demo" / "dist"
if demo_dist.exists():
    @app.get("/", include_in_schema=False)
    async def root_index():
        index_file = demo_dist / "index.html"
        if index_file.exists():
            return FileResponse(str(index_file))
        return RedirectResponse(url="demo/")

    @app.get("/demo", include_in_schema=False)
    async def demo_slash_redirect():
        return RedirectResponse(url="/demo/")

    if (demo_dist / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(demo_dist / "assets")), name="assets")

    app.mount("/demo", StaticFiles(directory=str(demo_dist), html=True), name="demo")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)


@app.get("/tool-policy-panel.js", include_in_schema=False)
async def tool_policy_panel_js():
    path = BASE_PATH / "demo" / "dist" / "tool-policy-panel.js"
    return FileResponse(str(path), media_type="application/javascript") if path.exists() else JSONResponse(status_code=404, content={"detail": "Not Found"})
