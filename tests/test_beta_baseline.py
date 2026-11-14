import asyncio
import json
import time
import httpx
import pytest
from app.gateway.client import AIGatewayClient
from app.runtime.runner import AgentRunner
from app.runtime.tasks.domain import Plan
from app.schemas.agent import AgentRunRequest, AgentRunOptions
from test_task_orchestrator import setup, Model


@pytest.mark.asyncio
@pytest.mark.parametrize('intent', ['conversation', 'clarify'])
async def test_user_reply_is_not_plan_and_needs_no_document_io(tmp_path, intent):
    repo, model, docs, runner = setup(tmp_path, Model(intent=intent, scope='none'))
    job = await runner.create('owner', 'hd2ai-admin', '방금 답변이 이상한데?', ['doc_sdk'],
                              [{'role':'user','content':'문서 개요를 설명해 줘'}])
    await runner.tasks[job['job_id']]
    task = repo.load(job['job_id'])
    assert task.status == ('needs_input' if intent == 'clarify' else 'completed')
    assert task.answer != task.plan.objective and task.public_reply
    assert task.budget.document_calls == 0 and not docs.reads
    assert all('방금 답변' not in e['label'] and 'SDK의 모든 함수' not in e['label'] for e in task.events)
    assert any('question' in r['messages'][0].content for r in model.requests[1:])


@pytest.mark.asyncio
async def test_old_plan_echo_is_not_returned_by_public_api(tmp_path):
    repo, _, _, runner = setup(tmp_path)
    job = await runner.create('owner', 'hd2ai-admin', '문서 설명', ['doc_sdk'])
    await runner.tasks[job['job_id']]
    task = repo.load(job['job_id'])
    task.plan = Plan(objective='사용자 의도를 확인하여 답변한다', intent='clarify', scope='none',
                     required_outputs=['확인'], completion_criteria=['대상 지정'])
    task.status='needs_input';task.answer=task.plan.objective;repo.save(task)
    public=runner.get(task.job_id,'owner','hd2ai-admin')
    assert public['answer'] != task.plan.objective and public['answer_kind']=='invalid'
    assert runner.get(task.job_id,'other','hd2ai-admin') is None


@pytest.mark.asyncio
async def test_typed_stream_keeps_reasoning_separate_and_context_metadata():
    async def handler(request):
        frames=[{'choices':[{'delta':{'reasoning_content':'짧은 검토 요약'}}]},
                {'choices':[{'delta':{'content':'{"answer":"완료"}'},'finish_reason':'stop'}]},
                {'context_truncated':False,'usage':{'total_tokens':12}}]
        return httpx.Response(200,text=''.join('data: '+json.dumps(f,ensure_ascii=False)+'\n\n' for f in frames)+'data: [DONE]\n\n')
    client=AIGatewayClient();client._get_transport=lambda:httpx.MockTransport(handler)
    thinking=[]
    reply=await client.complete(messages=[],stream=True,reasoning_effort='low',on_reasoning_delta=thinking.append)
    assert thinking==['짧은 검토 요약'] and reply.content=='{"answer":"완료"}'
    assert reply.usage.total_tokens==12 and not reply.context_truncated


@pytest.mark.asyncio
async def test_agent_reasoning_option_and_event_flow():
    seen=[]
    class Client:
        async def execute(self, **kwargs):
            seen.append(kwargs['reasoning_effort'])
            await kwargs['on_reasoning_delta']('처리 요약')
            return '사용자에게 보내는 응답', [], 'stop'
    runner=AgentRunner()
    session=await runner.create_run(AgentRunRequest(message='설명해줘',stream=True,
        options=AgentRunOptions(reasoning_effort='low')), custom_client=Client())
    await runner.start_run(session)
    events=[]
    async for event in runner.stream_events(session.state.run_id):events.append(event)
    assert seen==['low'] and session.state.status=='completed'
    assert any('event: reasoning.delta' in e for e in events)
    assert any('event: model.completed' in e for e in events)
    assert '인지 루프' not in ''.join(events)


@pytest.mark.asyncio
async def test_document_result_has_human_citations_and_replayable_progress(tmp_path):
    repo, _, _, runner=setup(tmp_path)
    job=await runner.create('owner','hd2ai-admin','문서 함수 설명',['doc_sdk'])
    await runner.tasks[job['job_id']]
    task=repo.load(job['job_id']);public=runner.get(task.job_id,'owner','hd2ai-admin')
    assert task.status=='completed' and 'doc_sdk/chk_' not in task.answer
    assert 'p.' in task.answer and public['citations']
    assert [e['seq'] for e in public['events']]==sorted({e['seq'] for e in public['events']})
    assert any(e['kind']=='tool' and e['status']=='done' for e in public['events'])


def test_inline_evidence_key_is_presented_with_its_verified_page():
    from app.runtime.tasks.presentation import humanize_keys
    citations=[{'key':'doc_sdk/chk_0014','title':'SDK.pdf','page':14}]
    text=humanize_keys('출처는 doc_sdk/chk_0014예요. [doc_sdk/chk_0014]', citations)
    assert text == '출처는 SDK.pdf · p. 14예요. SDK.pdf · p. 14'
    assert humanize_keys('doc_sdk/chk_00140',citations)=='doc_sdk/chk_00140'


@pytest.mark.asyncio
async def test_response_kind_is_derived_from_plan_not_model_reclassification(tmp_path):
    from app.schemas.gateway import ModelReply
    class WrongKindModel(Model):
        async def complete(self, **kwargs):
            if '사용자에게 직접 답하는 응답' in kwargs['task_instructions']:
                return ModelReply(content=json.dumps({'content':'방금 답변의 문제를 확인하고 바로잡겠습니다.',
                                                     'kind':'clarification'},ensure_ascii=False))
            return await super().complete(**kwargs)
    repo, _, _, runner = setup(tmp_path, WrongKindModel(intent='conversation',scope='none'))
    job = await runner.create('owner','hd2ai-admin','방금 답변이 이상한데?',['doc_sdk'])
    await runner.tasks[job['job_id']]
    task = repo.load(job['job_id'])
    assert task.status == 'completed' and task.public_reply.kind == 'answer'


@pytest.mark.asyncio
async def test_gateway_sse_error_is_not_misreported_as_empty_answer(monkeypatch):
    from app.gateway.client import GatewayStreamError
    from app.config import settings
    monkeypatch.setattr(settings,'ALLOW_MOCK_FALLBACK',False)
    async def handler(request):
        return httpx.Response(200,text='data: {"error":"PRIVATE_PROVIDER_BODY","code":"upstream_error"}\n\n')
    client=AIGatewayClient();client._get_transport=lambda:httpx.MockTransport(handler)
    with pytest.raises(GatewayStreamError) as error:
        await client.complete(messages=[],stream=True)
    assert error.value.code=='upstream_error' and 'PRIVATE_PROVIDER_BODY' not in str(error.value)
