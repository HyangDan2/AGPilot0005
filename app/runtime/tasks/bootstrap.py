"""Composition root. Existing HTTP/Gateway/filesystem adapters are reused."""
import time
from app.config import is_durable_analysis_enabled_for_service
from app.core.session_attachments import is_session_attachment
from .ports import JsonTaskRepository
from .orchestrator import TaskOrchestrator
from .services import ModelInvoker, RequestPlanner, AnswerWriter, AnswerVerifier, ApiEvidenceSelector
from .composition import AnswerComposer, CompositionVerifier
from .citations import SourceCitationResolver
from .responses import ConversationResponder


class AnalysisServices:
    def __init__(self, legacy, orchestrator, store):
        self.legacy, self.orchestrator, self.store = legacy, orchestrator, store

    @staticmethod
    def _effective_owner(owner: str, tenant_id: str | None) -> str:
        from app.core.auth import validate_scope_identifier

        safe_owner = validate_scope_identifier(owner, "owner_id")
        if tenant_id is not None:
            safe_tenant = validate_scope_identifier(tenant_id, "tenant_id")
            return f"{safe_tenant}::{safe_owner}"
        return safe_owner

    async def create(
        self,
        owner,
        service,
        message,
        refs,
        history=None,
        analysis_refs=None,
        session_id=None,
        reasoning_effort='off',
        tenant_id=None,
    ):
        scoped_owner = self._effective_owner(owner, tenant_id)
        if not is_durable_analysis_enabled_for_service(service):
            return await self.legacy.create(scoped_owner, service, message, refs, reasoning_effort=reasoning_effort)
        refs = list(dict.fromkeys(refs))
        modern = []
        for job_id in analysis_refs or []:
            raw = self.store.read(job_id)
            if not raw or raw.get('owner_id') != scoped_owner or raw.get('service_id') != service:
                raise ValueError('analysis_reference_unavailable')
            previous_docs = raw.get('document_ids', [])
            if any(is_session_attachment(doc) for doc in (*refs, *previous_docs)) and (
                not session_id or raw.get('session_id') != session_id
            ):
                raise ValueError('analysis_reference_unavailable')
            if refs and not set(previous_docs).issubset(refs): continue
            if raw.get('schema_version') == 2: modern.append(job_id)
            refs = list(dict.fromkeys(refs + previous_docs))
        return await self.orchestrator.create(scoped_owner, service, message, refs, history, modern, session_id, reasoning_effort)

    def _manager(self, job_id):
        raw = self.store.read(job_id)
        return self.orchestrator if raw and raw.get('schema_version') == 2 else self.legacy

    def session_allowed(self, job_id, conversation_id):
        raw = self.store.read(job_id)
        if not raw or not any(is_session_attachment(doc) for doc in raw.get('document_ids', [])):
            return True
        return bool(conversation_id and raw.get('session_id') == conversation_id)

    def references_attachments(self, job_ids, owner, service, tenant_id):
        effective_owner = self._effective_owner(owner, tenant_id)
        for job_id in job_ids:
            raw = self.store.read(job_id)
            if raw and raw.get('owner_id') == effective_owner and raw.get('service_id') == service:
                if any(is_session_attachment(doc) for doc in raw.get('document_ids', [])):
                    return True
        return False

    def get(self, job_id, owner, service, tenant_id=None):
        return self._manager(job_id).get(job_id, self._effective_owner(owner, tenant_id), service)

    def resume(self, job_id, owner, service, tenant_id=None):
        return self._manager(job_id).resume(job_id, self._effective_owner(owner, tenant_id), service)

    def cancel(self, job_id, owner, service, tenant_id=None):
        return self._manager(job_id).cancel(job_id, self._effective_owner(owner, tenant_id), service)


def services(app):
    if not hasattr(app.state, 'analysis_services'):
        from app.runtime.document_analysis import analysis_manager
        from app.adapters.document import document_adapter
        from app.gateway.client import gateway_client
        from app.config import settings
        store = analysis_manager.store
        repository = JsonTaskRepository(store, time.time)
        invoker = ModelInvoker(gateway_client, repository, time.time, input_tokens=settings.CONTEXT_BUDGET_TOKENS)
        orchestrator = TaskOrchestrator(repository, document_adapter, RequestPlanner(invoker),
            AnswerWriter(invoker), AnswerVerifier(invoker), time.time,
            max_chunks=settings.ANALYSIS_MAX_CHUNKS, max_models=settings.ANALYSIS_MAX_MODEL_CALLS,
            max_seconds=settings.ANALYSIS_MAX_SECONDS, max_document_calls=settings.TASK_MAX_DOCUMENT_CALLS,
            max_replans=settings.TASK_MAX_REPLANS, evidence_selector=ApiEvidenceSelector(invoker, repository),
            max_answer_repairs=settings.TASK_MAX_ANSWER_REPAIRS,
            responder=ConversationResponder(invoker), progress=invoker.progress,
            composer=AnswerComposer(invoker, repository, settings.TASK_MAX_ANSWER_REPAIRS,
                                    verifier=CompositionVerifier(invoker), citation_resolver=SourceCitationResolver()))
        app.state.analysis_services = AnalysisServices(analysis_manager, orchestrator, store)
    return app.state.analysis_services
