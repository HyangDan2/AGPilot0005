"""Composition root. Existing HTTP/Gateway/filesystem adapters are reused."""
import time
from .ports import JsonTaskRepository
from .orchestrator import TaskOrchestrator
from .services import ModelInvoker, RequestPlanner, AnswerWriter, AnswerVerifier, ApiEvidenceSelector
from .composition import AnswerComposer, CompositionVerifier
from .citations import SourceCitationResolver
from .responses import ConversationResponder


class AnalysisServices:
    def __init__(self, legacy, orchestrator, store):
        self.legacy, self.orchestrator, self.store = legacy, orchestrator, store

    async def create(self, owner, service, message, refs, history=None, analysis_refs=None, session_id=None, reasoning_effort='off'):
        if service != 'hd2ai-admin':
            return await self.legacy.create(owner, service, message, refs, reasoning_effort=reasoning_effort)
        refs = list(dict.fromkeys(refs))
        modern = []
        for job_id in analysis_refs or []:
            raw = self.store.read(job_id)
            if not raw or raw.get('owner_id') != owner or raw.get('service_id') != service:
                raise ValueError('analysis_reference_unavailable')
            previous_docs = raw.get('document_ids', [])
            if refs and not set(previous_docs).issubset(refs): continue
            if raw.get('schema_version') == 2: modern.append(job_id)
            refs = list(dict.fromkeys(refs + previous_docs))
        return await self.orchestrator.create(owner, service, message, refs, history, modern, session_id, reasoning_effort)

    def _manager(self, job_id):
        raw = self.store.read(job_id)
        return self.orchestrator if raw and raw.get('schema_version') == 2 else self.legacy

    def get(self, job_id, owner, service): return self._manager(job_id).get(job_id, owner, service)
    def resume(self, job_id, owner, service): return self._manager(job_id).resume(job_id, owner, service)
    def cancel(self, job_id, owner, service): return self._manager(job_id).cancel(job_id, owner, service)


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
