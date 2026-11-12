"""AGCore Platform FastAPI Application Entrypoint."""
import os
import time
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.openapi.docs import get_swagger_ui_html
from app.api.agent import router as agent_router
from app.api.demo import router as demo_router
from app.api.health import router as health_router
from app.config import settings

BASE_PATH = Path(__file__).resolve().parent.parent

app = FastAPI(
    title="HD2Lab Agent Core Platform (AGCore)",
    version="1.1.0",
    description="범용 Agentic AI Runtime 및 Tool Execution 백엔드 서비스",
    docs_url=None,
    redoc_url=None,
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
app.include_router(agent_router, prefix="/api/v1")
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
