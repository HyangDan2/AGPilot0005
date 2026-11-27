"""Service container for the workspace engine (used by the API and the preview app)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app.workspace.citations import CitationService
from app.workspace.config import WorkspaceSettings, get_settings
from app.workspace.context import ContextAssembler
from app.workspace.embedding import EmbeddingProvider, build_embedder
from app.workspace.indexer import WorkspaceIndexer
from app.workspace.ingest import CompositeIngestor
from app.workspace.manager import WorkspaceManager
from app.workspace.retrieval import WorkspaceRetriever
from app.workspace.storage import WorkspaceStorage
from app.workspace.store import WorkspaceStore
from app.workspace.tools import WorkspaceToolAdapter
from app.workspace.watcher import WorkspaceWatcher


@dataclass
class WorkspaceRuntime:
    settings: WorkspaceSettings
    store: WorkspaceStore
    storage: WorkspaceStorage
    manager: WorkspaceManager
    indexer: WorkspaceIndexer
    retriever: WorkspaceRetriever
    assembler: ContextAssembler
    citations: CitationService
    embedder: Optional[EmbeddingProvider]
    tools: WorkspaceToolAdapter
    watcher: Optional[WorkspaceWatcher] = None

    @classmethod
    def build(cls, settings: Optional[WorkspaceSettings] = None, *, start_worker: bool = False) -> "WorkspaceRuntime":
        settings = settings or get_settings()
        store = WorkspaceStore(settings.resolved_db_path(), busy_timeout_ms=settings.sqlite_busy_timeout_ms)
        storage = WorkspaceStorage(
            settings.resolved_storage_root(),
            max_file_bytes=settings.max_file_bytes,
            chunk_bytes=settings.upload_chunk_bytes,
        )
        manager = WorkspaceManager(store, storage, settings)
        embedder = build_embedder(settings)
        ingestor = CompositeIngestor(settings)
        indexer = WorkspaceIndexer(manager, settings, ingestor, embedder=embedder)
        retriever = WorkspaceRetriever(store, settings, embedder)
        citations = CitationService(store)
        assembler = ContextAssembler(store, settings, retriever, citations)
        tools = WorkspaceToolAdapter(manager, retriever, assembler)
        watcher = WorkspaceWatcher(manager, settings)
        runtime = cls(
            settings=settings,
            store=store,
            storage=storage,
            manager=manager,
            indexer=indexer,
            retriever=retriever,
            assembler=assembler,
            citations=citations,
            embedder=embedder,
            tools=tools,
            watcher=watcher,
        )
        manager.recover()
        if start_worker:
            indexer.start()
            watcher.start()
        return runtime

    def shutdown(self) -> None:
        self.indexer.stop()
        if self.watcher is not None:
            self.watcher.stop()


_RUNTIME: Optional[WorkspaceRuntime] = None


def get_runtime() -> WorkspaceRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = WorkspaceRuntime.build()
    return _RUNTIME


def set_runtime(runtime: Optional[WorkspaceRuntime]) -> None:
    """Testing/preview helper to inject a runtime instance."""

    global _RUNTIME
    _RUNTIME = runtime


def reset_runtime() -> None:
    global _RUNTIME
    if _RUNTIME is not None:
        _RUNTIME.shutdown()
    _RUNTIME = None
