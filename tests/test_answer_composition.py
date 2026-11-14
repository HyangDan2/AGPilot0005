import asyncio
import json
import time
from types import SimpleNamespace
import pytest
from app.runtime.tasks.composition import AnswerComposer, COMPOSE, VERIFY
from app.runtime.tasks.domain import Draft, Section, Verification
from test_task_orchestrator import setup, Model, Documents
from app.runtime.tasks.services import ModelInvoker
from app.schemas.gateway import ModelReply
from app.runtime.tasks.citations import SourceCitationResolver


class SummaryModel(Model):
    def __init__(self, reject_final=False, duplicate=False):
        super().__init__(intent='summary')
        self.reject_final, self.duplicate = reject_final, duplicate
        self.compositions = self.final_checks = 0
        self.synthesis_requests = []
        self.verification_requests = []

    async def complete(self, **kwargs):
        instructions = kwargs['task_instructions']
        data = json.loads(kwargs['messages'][0].content)
        if instructions == COMPOSE:
            self.compositions += 1
            self.synthesis_requests.append(data)
            sections = [s for item in data['inputs'] for s in item['sections']]
            refs = list(dict.fromkeys(r for s in sections for r in s['refs']))
            # A deterministic fake preserves facts but removes repeated local introductions.
            facts = list(dict.fromkeys(line for s in sections for line in s['body'].splitlines()
                                       if line.startswith('Function')))
            value = {'sections': [{'heading': '주요 기능', 'body': '\n'.join(facts) or '카메라 SDK의 개요와 사용을 위한 전제 조건을 설명합니다.', 'refs': refs, 'labels': []}]}
            if self.duplicate:
                value['sections'] *= 2
        elif instructions == VERIFY or 'verification_mode' in data:
            self.final_checks += 1
            self.verification_requests.append(data)
            value = {'satisfied': not self.reject_final,
                     'missing_requirements': ['중요 내용 누락'] if self.reject_final and data.get('verification_mode') != 'grounding' else [],
                     'unsupported_claims': ['사실 근거 불충분'] if self.reject_final and data.get('verification_mode') == 'grounding' else []}
        elif instructions.startswith('문서 작업의 산출물'):
            refs = list(dict.fromkeys(s['key'] for s in data['sources']))
            value = {'sections': [
                {'heading': '문서 핵심 내용 요약', 'body': '이 문서는 카메라 SDK 매뉴얼의 일부이며 제공된 묶음을 설명합니다.', 'refs': refs, 'labels': []},
                {'heading': '주요 섹션 및 출처', 'body': '\n'.join(s['content'] for s in data['sources']), 'refs': refs, 'labels': []}]}
        else:
            return await super().complete(**kwargs)
        return ModelReply(content=json.dumps(value, ensure_ascii=False))


def configured(tmp_path, model=None, n=4, size=1400):
    docs = Documents(n)
    docs.contents = {k: v + '\n' + '설명 ' * size for k, v in docs.contents.items()}
    repo, model, docs, orchestrator = setup(tmp_path, model or SummaryModel(), docs)
    orchestrator.composer = AnswerComposer(ModelInvoker(model, repo, time.time), repo)
    return repo, model, docs, orchestrator


@pytest.mark.asyncio
async def test_repeated_group_answers_become_one_verified_document_answer(tmp_path):
    repo, model, docs, o = configured(tmp_path)
    result = await o.create('a', 'hd2ai-admin', '전체 문서를 요약해', ['doc_sdk'])
    await o.tasks[result['job_id']]
    task = repo.load(result['job_id'])
    assert task.status == 'completed'
    assert len(task.drafts) > 1 and len(task.verified_groups) == len(task.drafts)
    assert all('문서 핵심 내용 요약' in d.render() for d in task.drafts.values())
    assert task.answer.count('### 주요 기능') == 1
    assert '제공된 묶음' not in task.answer
    assert all(f'Function{i}(' in task.answer for i in range(4))
    assert task.final_verification.satisfied and model.final_checks >= 1
    assert {'requirements', 'grounding', 'coverage'}.issubset({r.get('verification_mode') for r in model.verification_requests})
    assert all(r['user_plan'] == task.plan.model_dump() for r in model.verification_requests if r.get('verification_mode') == 'coverage')
    # Changing the final answer or its evidence invalidates the verification seal.
    task.status = 'verifying'
    task.final_draft.sections[0].body += ' 근거 없는 내용 추가'
    with pytest.raises(ValueError, match='final_answer_unverified'):
        task.complete()


@pytest.mark.asyncio
@pytest.mark.parametrize('duplicate,reject', [(True, False), (False, True)])
async def test_global_failure_cannot_publish_concatenated_intermediates(tmp_path, duplicate, reject):
    repo, model, _, o = configured(tmp_path, SummaryModel(reject_final=reject, duplicate=duplicate))
    result = await o.create('a', 'hd2ai-admin', '전체 요약', ['doc_sdk'])
    await o.tasks[result['job_id']]
    task = repo.load(result['job_id'])
    assert len(task.verified_groups) == len(task.drafts)
    assert task.status == 'partial' and task.answer is None
    assert task.error_code == 'final_answer_requirements_unsatisfied'
    assert model.compositions == 3
    assert any(r.get('repair') and r.get('previous_answer') for r in model.synthesis_requests)
    # Resume honors the persisted repair limit instead of paying for repeated failures.
    o.resume(task.job_id, 'a', 'hd2ai-admin')
    await o.tasks[task.job_id]
    assert model.compositions == 3


@pytest.mark.asyncio
async def test_synthesis_is_required_even_when_every_source_group_passed(tmp_path):
    repo, _, _, o = configured(tmp_path)
    o.composer = None
    result = await o.create('a', 'hd2ai-admin', '전체 요약', ['doc_sdk'])
    await o.tasks[result['job_id']]
    task = repo.load(result['job_id'])
    assert task.status == 'partial' and task.error_code == 'final_answer_composer_unavailable'
    task.status = 'verifying'
    with pytest.raises(ValueError, match='final_answer_unverified'):
        task.complete()


@pytest.mark.asyncio
async def test_large_drafts_use_checkpointed_tree_without_losing_source_facts(tmp_path):
    repo, model, _, o = configured(tmp_path, n=18)
    # Keep verbose original intermediate bodies to force bounded tree composition.
    original = o.writer.write
    async def verbose(*args, **kwargs):
        draft = await original(*args, **kwargs)
        draft.sections[0].body += '\n' + '문서 설명 자료. ' * 220
        return draft
    o.writer.write = verbose
    result = await o.create('a', 'hd2ai-admin', '전체 요약', ['doc_sdk'])
    await o.tasks[result['job_id']]
    task = repo.load(result['job_id'])
    assert task.status == 'completed'
    assert len(task.composition_nodes) > 1
    assert all(f'Function{i}(' in task.answer for i in range(18))
    assert all(n.verification.satisfied for n in task.composition_nodes.values())
    intermediate = [r for r in model.synthesis_requests if not r['global_final']]
    assert intermediate
    assert all(r['plan']['required_outputs'] != task.plan.required_outputs for r in intermediate)
    assert model.synthesis_requests[-1]['plan'] == task.plan.model_dump()
    before = model.compositions
    await o.composer.compose(task)
    assert model.compositions == before
    for request in model.requests:
        assert request['max_tokens'] >= 512


@pytest.mark.asyncio
async def test_cancel_during_final_check_resumes_saved_composition(tmp_path):
    class BlockingModel(SummaryModel):
        def __init__(self):
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()
        async def complete(self, **kwargs):
            if kwargs['task_instructions'] == VERIFY or 'verification_mode' in json.loads(kwargs['messages'][0].content):
                self.entered.set()
                await self.release.wait()
            return await super().complete(**kwargs)
    model = BlockingModel()
    repo, _, _, o = configured(tmp_path, model, size=100)
    result = await o.create('a', 'hd2ai-admin', '전체 요약', ['doc_sdk'])
    await asyncio.wait_for(model.entered.wait(), 5)
    o.cancel(result['job_id'], 'a', 'hd2ai-admin')
    await o.tasks[result['job_id']]
    task = repo.load(result['job_id'])
    assert task.status == 'cancelled' and task.final_draft is None
    assert any(n.draft and n.verification is None for n in task.composition_nodes.values())
    count = model.compositions
    model.release.set()
    o.resume(task.job_id, 'a', 'hd2ai-admin')
    await o.tasks[task.job_id]
    assert repo.load(task.job_id).status == 'completed'
    assert model.compositions == count
    assert any(r.get('verification_mode') == 'grounding' and
               any(s['heading'] == '원문' for item in r['inputs'] for s in item['sections'])
               for r in model.verification_requests)


@pytest.mark.asyncio
async def test_out_of_scope_verifier_failure_retries_verifier_not_answer(tmp_path):
    class ConfusedVerifier(SummaryModel):
        confused = False
        async def complete(self, **kwargs):
            data = json.loads(kwargs['messages'][0].content)
            if data.get('verification_mode') == 'grounding' and not self.confused:
                self.confused = True
                return ModelReply(content=json.dumps({'satisfied': False,
                    'missing_requirements': ['EnumerateImageDevice 상세 설명 누락'], 'unsupported_claims': []}))
            return await super().complete(**kwargs)
    repo, model, _, o = configured(tmp_path, ConfusedVerifier(), size=100)
    result = await o.create('a', 'hd2ai-admin', '핵심 요약', ['doc_sdk'])
    await o.tasks[result['job_id']]
    task = repo.load(result['job_id'])
    assert task.status == 'completed'
    assert any('missing_requirements' in e for e in task.contract_errors)
    assert model.compositions == len(task.composition_nodes)


def test_source_citation_candidates_keep_text_and_stay_inside_allowed_evidence():
    task = SimpleNamespace(evidence={
        'doc/a': SimpleNamespace(content='Visual Studio prerequisites.'),
        'doc/b': SimpleNamespace(content='Contact Live Chat and RMA support. Revision 2019.'),
        'private/c': SimpleNamespace(content='Contact Live Chat and RMA support. Revision 2019.')})
    body = '지원은 Live Chat 및 RMA를 통해 제공하며 개정 이력은 2019년부터 시작됩니다.'
    draft = Draft(sections=[Section(heading='지원', body=body, refs=['doc/a'])])
    SourceCitationResolver().resolve(task, draft, {'doc/a', 'doc/b'})
    assert draft.sections[0].refs == ['doc/a', 'doc/b']
    assert draft.sections[0].body == body
