"""R2 Canvas Artifact Engine (AGCore).

Artifact-centric editing platform that lives beside the R1.3 agent runtime:

- ``app.canvas.manager``      artifact lifecycle and metadata (WO-C02)
- ``app.canvas.storage``      swappable metadata/content persistence (WO-C03)
- ``app.canvas.revisions``    revision history, diff, non-destructive restore (WO-C04)
- ``app.canvas.patching``     partial patch engine with conflict control (WO-C05)
- ``app.canvas.events``       SSE artifact event contract, streaming sessions (WO-C06)
- ``app.canvas.editing``      AI partial editing (context budget, proposals) (WO-C07)
- ``app.canvas.tools``        ``canvas.*`` tool manifest and handlers (WO-C08)
- ``app.canvas.export``       export providers and file naming (WO-C13)
- ``app.canvas.security``     preview sandbox, XSS and injection boundaries (WO-C14)
- ``app.canvas.workspace``    Workspace/RAG shared-contract gateway

Nothing in this package imports ``app.runtime``/``app.gateway``, so the R1.3 runtime cutover
stays isolated from Canvas work.
"""
from app.canvas.service import CanvasService, get_canvas_service, reset_canvas_service

__all__ = ["CanvasService", "get_canvas_service", "reset_canvas_service"]
