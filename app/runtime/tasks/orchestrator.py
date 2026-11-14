"""Durable orchestration: policy/state sequencing, with algorithms injected."""
import asyncio
import hashlib
import re
import uuid
from .domain import AccessContext, Task, BudgetLedger, Evidence, BudgetExceeded
from .ports import ScopedDocuments
from .services import estimate
from .progress import TaskProgress
from .presentation import AnswerPresenter


class TaskOrchestrator:
    def __init__(self, repository, documents, planner, writer, verifier, clock,
                 max_chunks=1000, max_models=500, max_seconds=7200, max_document_calls=5000,
                 input_tokens=12000, max_replans=2, evidence_selector=None, max_answer_repairs=2,
                 composer=None, responder=None, progress=None, presenter=None):
        self.repository, self.documents = repository, documents
        self.planner, self.writer, self.verifier, self.clock = planner, writer, verifier, clock
        self.max_chunks, self.max_models, self.max_seconds = max_chunks, max_models, max_seconds
        self.max_document_calls, self.input_tokens, self.max_replans = max_document_calls, input_tokens, max_replans
        self.tasks = {}
        self.capacity = asyncio.Semaphore(2)
        self.evidence_selector = evidence_selector
        self.max_answer_repairs = max_answer_repairs
        self.composer = composer
        self.responder = responder
        self.progress = progress or TaskProgress(repository, clock)
        self.presenter = presenter or AnswerPresenter()

    async def create(self, owner, service, question, refs, history=None, analysis_refs=None, session_id=None, reasoning_effort='off'):
        refs = list(dict.fromkeys(refs))
        if not question.strip() or len(question.encode()) > 1024 * 1024 or len(refs) > 10:
            raise ValueError('invalid_task_request')
        if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', x) for x in refs):
            raise ValueError('invalid_document_reference')
        now = self.clock()
        task = Task(job_id='ana_' + uuid.uuid4().hex, owner_id=owner, service_id=service,
                    question=question, session_id=session_id, history=history or [], document_ids=refs,
                    analysis_refs=analysis_refs or [], created_at=now, updated_at=now, reasoning_effort=reasoning_effort,
                    budget=BudgetLedger(deadline=now + self.max_seconds, max_model_calls=self.max_models,
                                        max_document_calls=self.max_document_calls))
        access = AccessContext(owner=owner, service=service)
        for ref in task.analysis_refs:
            previous = self.repository.load(ref)
            if previous is None or not previous.owned_by(access):
                raise ValueError('analysis_reference_unavailable')
            if refs and not set(previous.document_ids).issubset(refs):
                continue
            task.document_ids = list(dict.fromkeys(task.document_ids + previous.document_ids))
            task.evidence.update({k:v.model_copy(update={'snapshot_id':previous.snapshots.get(v.doc_id, '')})
                                  for k,v in previous.evidence.items()})
        if len(task.document_ids) > 10:
            raise ValueError('too_many_documents')
        self.repository.save(task)
        self._start(task.job_id)
        return self.public(task)

    def public(self, task):
        selected = set(task.selected_keys)
        leaked = bool(task.plan and task.plan.intent == 'clarify' and task.answer == task.plan.objective)
        return {'job_id': task.job_id, 'status': task.status,
                'processed_chunks': len(selected.intersection(task.evidence)),
                'total_chunks': len(selected), 'model_calls': task.budget.model_calls,
                'document_calls': task.budget.document_calls,
                'answer': '이전 버전의 응답 오류입니다. 질문을 다시 보내 주세요.' if leaked else task.answer,
                'answer_kind': 'invalid' if leaked else task.public_reply.kind if task.public_reply else 'answer',
                'phase': task.phase, 'events': task.events, 'reasoning_text': task.reasoning_text,
                'citations': self.presenter.citations(task), 'error_code': task.error_code,
                'created_at': task.created_at, 'updated_at': task.updated_at,
                'coverage_scope': task.plan.scope if task.plan else None,
                'verified_groups': len(task.verified_groups)}

    def get(self, job_id, owner, service):
        task = self.repository.load(job_id)
        if task is None or not task.owned_by(AccessContext(owner=owner, service=service)):
            return None
        worker = self.tasks.get(job_id)
        if task.status in ('queued', 'planning', 'running', 'verifying') and (worker is None or worker.done()):
            task.status, task.error_code = 'paused', 'worker_restarted'
            self.repository.save(task)
        return self.public(task)

    def resume(self, job_id, owner, service):
        result = self.get(job_id, owner, service)
        if result is None: return None
        task = self.repository.load(job_id)
        if task.status in ('paused', 'partial', 'failed', 'cancelled'):
            task.resume(self.clock())
            self.repository.save(task)
            self._start(job_id)
        return self.public(task)

    def cancel(self, job_id, owner, service):
        task = self.repository.load(job_id)
        if task is None or not task.owned_by(AccessContext(owner=owner, service=service)):
            return None
        if task.status in ('completed', 'needs_input'): return self.public(task)
        worker = self.tasks.get(job_id)
        if worker and not worker.done(): worker.cancel()
        task.status, task.error_code = 'cancelled', None
        self.repository.save(task)
        return self.public(task)

    def _start(self, job_id):
        if job_id not in self.tasks or self.tasks[job_id].done():
            self.tasks[job_id] = asyncio.create_task(self._run(job_id))

    async def _document(self, task, name, *args):
        task.budget.reserve('document', self.clock())
        call_id = 'document-' + str(task.budget.document_calls)
        label = {'get_outline':'문서 목차 확인','list_chunks':'근거 목록 확인','read_chunk':'문서 본문 읽기','search':'문서 검색'}.get(name, name)
        started = self.clock()
        self.progress.event(task, 'tool', label, item_id=call_id)
        scoped = ScopedDocuments(self.documents, AccessContext(owner=task.owner_id, service=task.service_id))
        remaining = task.budget.deadline - self.clock()
        async with asyncio.timeout(min(30, remaining)):
            result = await scoped.call(name, *args)
        if result.get('error'): raise ValueError('document_' + str(result.get('code', 'read_failed')))
        self.progress.event(task, 'tool', label, status='done', item_id=call_id, duration_ms=round((self.clock()-started)*1000))
        return result

    async def _metadata(self, task):
        metadata = []
        for doc_id in task.document_ids:
            result = await self._document(task, 'get_outline', doc_id)
            if result.get('status') != 'completed': raise ValueError('document_not_ready')
            task.document_titles[doc_id] = str(result.get('document_name') or result.get('filename') or '첨부 문서')[:240]
            # Metadata budget only; source content never silently truncated.
            headings = []
            def walk(nodes):
                for node in nodes:
                    if len(headings) < 30: headings.append(str(node.get('title', ''))[:120])
                    walk(node.get('children', node.get('subsections', [])) if isinstance(node.get('children', node.get('subsections', [])), list) and all(isinstance(x, dict) for x in node.get('children', node.get('subsections', []))) else [])
            outline = result.get('outline', [])
            if isinstance(outline, list): walk(outline)
            metadata.append({'doc_id': doc_id, 'headings': headings})
        return metadata

    async def _manifest(self, task):
        entries, snapshots = [], {}
        for doc_id in task.document_ids:
            cursor, seen = None, set()
            while True:
                page = await self._document(task, 'list_chunks', doc_id, cursor, 100)
                snapshot = page['snapshot_id']
                if doc_id in snapshots and snapshots[doc_id] != snapshot: raise ValueError('document_snapshot_changed')
                snapshots[doc_id] = snapshot
                entries.extend({'doc_id': doc_id, **item} for item in page['chunks'])
                if len(entries) > self.max_chunks: raise BudgetExceeded('chunk_budget_exceeded')
                cursor = page.get('next_cursor')
                if not cursor:
                    if sum(e['doc_id'] == doc_id for e in entries) != page['total_chunks']: raise ValueError('chunk_listing_incomplete')
                    break
                if cursor in seen: raise ValueError('invalid_chunk_cursor')
                seen.add(cursor)
        keys = [e['doc_id'] + '/' + e['chunk_id'] for e in entries]
        if not keys or len(keys) != len(set(keys)): raise ValueError('chunk_listing_invalid')
        if task.snapshots and task.snapshots != snapshots: raise ValueError('document_snapshot_changed')
        previous_snapshots = task.snapshots
        task.entries, task.snapshots = entries, snapshots
        hashes = {e['doc_id'] + '/' + e['chunk_id']: e['content_hash'] for e in entries}
        task.evidence = {k: v.model_copy(update={'snapshot_id':snapshots.get(v.doc_id, '')}) for k, v in task.evidence.items()
                         if hashes.get(k) == v.content_hash and (v.snapshot_id == snapshots.get(v.doc_id)
                         or (not v.snapshot_id and previous_snapshots.get(v.doc_id) == snapshots.get(v.doc_id)))}
        self.repository.save(task)

    async def _select(self, task):
        all_keys = [e['doc_id'] + '/' + e['chunk_id'] for e in task.entries]
        if task.plan.scope == 'all':
            task.selected_keys = all_keys
        elif not task.selected_keys:
            found = set()
            for doc_id in task.document_ids:
                for term in (task.plan.search_terms or [task.plan.objective])[:3]:
                    result = await self._document(task, 'search', doc_id, term, 5)
                    found.update(doc_id + '/' + x['chunk_id'] for x in result.get('results', []) if x.get('chunk_id'))
            task.selected_keys = [k for k in all_keys if k in found]
            if not task.selected_keys:
                # Search can miss phrasing. Escalate within the same task and ledger.
                if task.replans >= self.max_replans:
                    raise ValueError('replan_budget_exceeded')
                task.replans += 1
                task.plan = task.plan.model_copy(update={'scope': 'all', 'revision': task.plan.revision + 1})
                task.selected_keys = all_keys
        self.repository.save(task)

    async def _read(self, task):
        entries = {e['doc_id'] + '/' + e['chunk_id']: e for e in task.entries}
        for key in task.selected_keys:
            if key in task.evidence:
                self.progress.event(task, 'cache', '이전 근거 재사용', status='done', item_id='cache-' + key, cached=True)
                continue
            entry = entries[key]
            chunk = await self._document(task, 'read_chunk', entry['doc_id'], entry['chunk_id'])
            content = chunk.get('content', '')
            digest = hashlib.sha256(content.encode()).hexdigest()
            if digest != entry['content_hash'] or chunk.get('chunk_id') != entry['chunk_id']:
                raise ValueError('document_snapshot_changed')
            task.evidence[key] = Evidence(key=key, doc_id=entry['doc_id'], chunk_id=entry['chunk_id'],
                                         content_hash=digest, snapshot_id=task.snapshots[entry['doc_id']], content=content, source=chunk.get('source') or {})
            self.repository.save(task)

    def _groups(self, task):
        if task.plan.scope == 'none': return [[]]
        groups, group, size = [], [], 0
        # Source parts stay lossless; grouping is an execution policy, not summarization.
        for key in task.selected_keys:
            item = task.evidence[key]
            parts = [item.content[i:i+2800] for i in range(0, len(item.content), 2800)] or ['']
            for index, content in enumerate(parts):
                width = estimate(content) + len(key) + 100
                if group and size + width > 4300:
                    groups.append(group); group, size = [], 0
                group.append({'key': key, 'part': index + 1, 'content': content, 'source': item.source})
                size += width
        if group: groups.append(group)
        return groups

    async def _answer(self, task):
        groups = self._groups(task)
        if task.plan.intent == 'extract' and task.plan.entity_type == 'api' and self.evidence_selector:
            groups = await self.evidence_selector.groups(task, groups)
        for sources in groups:
            group_id = hashlib.sha256(str([(s['key'], s['part'], s.get('focus')) for s in sources]).encode()).hexdigest()
            if group_id in task.verified_groups: continue
            repair = None
            for attempt in range(self.max_answer_repairs + 1):
                task.status = 'running'
                self.repository.save(task)
                draft = task.drafts.get(group_id) if attempt == 0 else None
                if draft is None:
                    self.progress.phase(task, 'write', '근거 묶음 답변 작성')
                    draft = await self.writer.write(task, sources, repair)
                    task.drafts[group_id] = draft
                    self.repository.save(task)
                task.status = 'verifying'
                self.progress.phase(task, 'verify', '원문 근거 대조')
                self.repository.save(task)
                verification = await self.verifier.verify(task, draft, sources)
                task.verification_reports[group_id] = verification
                self.repository.save(task)
                if verification.satisfied:
                    task.record_verification(group_id, verification)
                    self.repository.save(task)
                    break
                if attempt >= self.max_answer_repairs:
                    raise ValueError('answer_requirements_unsatisfied')
                task.answer_repairs += 1
                self.repository.save(task)
                repair = verification.model_dump()
            else:
                raise ValueError('answer_requirements_unsatisfied')
        if task.requires_composition():
            if self.composer is None:
                raise ValueError('final_answer_composer_unavailable')
            await self.composer.compose(task)
        task.status = 'verifying'
        task.complete()
        task.answer = self.presenter.render(task)
        self.repository.save(task)

    async def _reply(self, task):
        if self.responder is None:
            raise ValueError('conversation_responder_unavailable')
        self.progress.phase(task, 'response', '대화 응답 작성' if task.plan.intent == 'conversation' else '필요한 정보 확인')
        await self.responder.respond(task)
        task.status = 'verifying'
        task.complete()
        self.repository.save(task)

    async def _run(self, job_id):
        task = self.repository.load(job_id)
        try:
            async with self.capacity:
                async with asyncio.timeout(max(0, task.budget.deadline - self.clock())):
                    if task.plan is None:
                        task.status = 'planning'
                        self.progress.phase(task, 'planning', '요청 의도 확인')
                        metadata = [{'doc_id': d, 'headings': []} for d in task.document_ids]
                        await self.planner.plan(task, metadata)
                        self.repository.save(task)
                    if task.plan.scope == 'none':
                        await self._reply(task)
                    else:
                        self.progress.phase(task, 'read', '문서 근거 확인')
                        await self._metadata(task)
                        await self._manifest(task)
                        await self._select(task)
                        await self._read(task)
                        await self._answer(task)
                    self.progress.event(task, 'run', '응답 완료' if task.status == 'completed' else '사용자 확인 필요', status='done')
        except asyncio.CancelledError:
            latest = self.repository.load(job_id)
            if latest.status != 'cancelled':
                latest.status = 'cancelled'; self.repository.save(latest)
        except Exception as exc:
            latest = self.repository.load(job_id)
            if latest.status == 'cancelled': return
            latest.status = 'partial' if latest.evidence else 'failed'
            latest.error_code = ('task_deadline_exceeded' if isinstance(exc, TimeoutError)
                                 else str(exc) if isinstance(exc, ValueError) else 'task_execution_failed')
            # Expose only stable codes; never provider bodies or private document content.
            if not re.fullmatch(r'[a-z_0-9]{1,80}', latest.error_code): latest.error_code = 'task_contract_invalid'
            if latest.requires_composition():
                latest.answer = None
            elif latest.drafts:
                latest.answer = '\n\n'.join(d.render() for k, d in latest.drafts.items() if k in latest.verified_groups) or None
            self.repository.save(latest)
