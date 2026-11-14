"""Consumer-specific ports and adapters for existing implementations."""
from typing import Protocol
from app.schemas.gateway import ModelReply
from app.adapters.document import document_owner_id
from .domain import AccessContext, Task


class ModelPort(Protocol):
    async def complete(self, **kwargs) -> ModelReply: ...


class DocumentReader(Protocol):
    async def get_outline(self, doc_id: str) -> dict: ...
    async def list_chunks(self, doc_id: str, cursor=None, limit=100) -> dict: ...
    async def read_chunk(self, doc_id: str, chunk_id: str) -> dict: ...
    async def search(self, doc_id: str, query: str, limit=5) -> dict: ...


class TaskRepository(Protocol):
    def load(self, job_id: str) -> Task | None: ...
    def save(self, task: Task) -> None: ...


class ScopedDocuments:
    """Confine the legacy ContextVar to the HTTP adapter boundary."""
    def __init__(self, adapter, access: AccessContext):
        self.adapter, self.access = adapter, access

    async def call(self, operation, *args, **kwargs):
        token = document_owner_id.set(self.access.owner)
        try:
            return await getattr(self.adapter, operation)(*args, **kwargs)
        finally:
            document_owner_id.reset(token)


class JsonTaskRepository:
    def __init__(self, store, clock):
        self.store, self.clock = store, clock

    def load(self, job_id):
        raw = self.store.read(job_id)
        return Task.model_validate(raw) if raw and raw.get('schema_version') == 2 else None

    def save(self, task):
        current = self.store.read(task.job_id)
        if current and current.get('storage_revision', 0) != task.storage_revision:
            raise ValueError('task_write_conflict')
        task.updated_at = self.clock()
        task.storage_revision += 1
        self.store.write(task.model_dump())
