import asyncio
import hashlib
import json
import time
import pytest
from app.schemas.gateway import ModelReply
from app.runtime.document_analysis import AnalysisStore
from app.runtime.tasks.domain import AccessContext, Task, BudgetLedger, Plan, Draft, Section, Verification
from app.runtime.tasks.ports import JsonTaskRepository
from app.runtime.tasks.orchestrator import TaskOrchestrator
from app.runtime.tasks.services import ModelInvoker, RequestPlanner, AnswerWriter, AnswerVerifier
from app.runtime.tasks.responses import ConversationResponder


class Documents:
    def __init__(self, n=17):
        self.contents = {f'chk_{i}': f'Function{i}(int value) returns TRUE. Required value {i}.' for i in range(n)}
        self.reads = []
    async def get_outline(self, doc_id): return {'status': 'completed', 'outline': [{'title': 'SDK APIs'}]}
    async def list_chunks(self, doc_id, cursor=None, limit=100):
        return {'snapshot_id': hashlib.sha256(str(self.contents).encode()).hexdigest(), 'total_chunks': len(self.contents),
                'chunks': [{'chunk_id': k, 'content_hash': hashlib.sha256(v.encode()).hexdigest()} for k,v in self.contents.items()], 'next_cursor': None}
    async def read_chunk(self, doc_id, chunk_id):
        self.reads.append(chunk_id)
        return {'chunk_id': chunk_id, 'content': self.contents[chunk_id], 'source': {'page': len(self.reads)}}
    async def search(self, doc_id, query, limit=5): return {'results': [{'chunk_id': 'chk_0'}]}


class Model:
    def __init__(self, intent='extract', scope='all', reject=False):
        self.intent, self.scope, self.reject = intent, scope, reject
        self.requests = []
    async def complete(self, **kwargs):
        self.requests.append(kwargs)
        instructions = kwargs['task_instructions']; data = json.loads(kwargs['messages'][0].content)
        if '자연어 문서 작업을 계획' in instructions:
            value = {'objective':'SDK의 모든 함수 설명', 'intent':self.intent, 'scope':self.scope,
                     'required_outputs':['함수/인자/반환값/출처'], 'search_terms':['Function0'], 'completion_criteria':['모든 함수 포함']}
        elif '사용자에게 직접 답하는 응답' in instructions:
            value = {'content': '어떤 문서를 비교할지 알려 주세요?' if self.intent == 'clarify' else '방금 답변이 잘못 전달된 부분을 확인하고 바로잡겠습니다.',
                     'kind': 'clarification' if self.intent == 'clarify' else 'answer'}
        elif '답변을 원문과 계획에 대조' in instructions or '사용자 응답을 검증' in instructions:
            value = {'satisfied':not self.reject, 'missing_requirements':['실질적인 설명'] if self.reject else [], 'unsupported_claims':[]}
        else:
            sources=data['sources']
            value={'sections':[{'heading':'함수 목록','body':'\n'.join(s['content'] for s in sources) or '문서 기능에 관한 질문에 직접 설명하는 충분한 답변입니다.',
                                'refs':list(dict.fromkeys(s['key'] for s in sources)),
                                'labels':[s['content'].split('(')[0] for s in sources]}]}
        return ModelReply(content=json.dumps(value,ensure_ascii=False))


def setup(tmp_path, model=None, docs=None):
    repo=JsonTaskRepository(AnalysisStore(str(tmp_path)),time.time)
    model=model or Model();docs=docs or Documents()
    invoker=ModelInvoker(model,repo,time.time)
    orchestrator=TaskOrchestrator(repo,docs,RequestPlanner(invoker),AnswerWriter(invoker),AnswerVerifier(invoker),time.time,
                                  responder=ConversationResponder(invoker), progress=invoker.progress)
    return repo,model,docs,orchestrator


@pytest.mark.asyncio
async def test_all_api_coverage_and_followup_reuse(tmp_path):
    repo,model,docs,o=setup(tmp_path)
    j=await o.create('a','hd2ai-admin','모든 함수 설명',['doc_sdk'])
    await o.tasks[j['job_id']]
    task=repo.load(j['job_id'])
    assert task.status=='completed' and len(task.evidence)==17
    assert all(f'Function{i}(' in task.answer for i in range(17))
    assert o.get(task.job_id,'b','hd2ai-admin') is None
    model.intent='lookup';model.scope='relevant';docs.reads.clear()
    j2=await o.create('a','hd2ai-admin','그 함수의 인자는?',['doc_sdk'],[{'role':'user','content':'Function0을 설명해'}],[task.job_id])
    await o.tasks[j2['job_id']]
    task2=repo.load(j2['job_id'])
    assert task2.status=='completed' and 'Function0(' in task2.answer
    assert docs.reads==[]
    assert any('Function0을 설명해' in r['messages'][0].content for r in model.requests)


@pytest.mark.asyncio
async def test_clarification_is_not_success_and_failed_verification_is_partial(tmp_path):
    repo,model,docs,o=setup(tmp_path,Model(intent='clarify',scope='none'))
    j=await o.create('a','hd2ai-admin','내용 중섯',['doc_sdk']);await o.tasks[j['job_id']]
    assert repo.load(j['job_id']).status=='needs_input' and docs.reads==[]
    model.intent='summary';model.scope='all';model.reject=True
    j=await o.create('a','hd2ai-admin','전체 요약',['doc_sdk']);await o.tasks[j['job_id']]
    task=repo.load(j['job_id'])
    assert task.status=='partial' and task.error_code=='answer_requirements_unsatisfied'
    assert task.answer is None and task.answer_repairs==2 and task.replans==0


@pytest.mark.asyncio
async def test_conversation_does_not_read_document(tmp_path):
    repo,model,docs,o=setup(tmp_path,Model(intent='conversation',scope='none'))
    j=await o.create('a','hd2ai-admin','전체분석 기능이 왜 실패해?',['doc_sdk']);await o.tasks[j['job_id']]
    assert repo.load(j['job_id']).status=='completed' and docs.reads==[]


@pytest.mark.asyncio
async def test_snapshot_changed_does_not_reuse_old_evidence(tmp_path):
    repo,model,docs,o=setup(tmp_path)
    j=await o.create('a','hd2ai-admin','요약',['doc_sdk']);await o.tasks[j['job_id']]
    docs.contents['chk_0']='FunctionChanged(int value) returns TRUE.';docs.reads.clear()
    j2=await o.create('a','hd2ai-admin','요약',['doc_sdk'],analysis_refs=[j['job_id']]);await o.tasks[j2['job_id']]
    assert repo.load(j2['job_id']).status=='completed' and set(docs.reads)==set(docs.contents)


def test_task_state_budget_and_repository_invariants(tmp_path):
    repo,_,_,_=setup(tmp_path)
    t=Task(job_id='ana_'+'a'*32,owner_id='a',service_id='hd2ai-admin',question='요약',created_at=0,updated_at=0,budget=BudgetLedger(deadline=10,max_model_calls=1))
    repo.save(t);old=repo.load(t.job_id)
    t.budget.reserve('model',1);repo.save(t)
    with pytest.raises(ValueError,match='task_write_conflict'):repo.save(old)
    with pytest.raises(ValueError,match='model_call_budget'):t.budget.reserve('model',2)
    with pytest.raises(ValueError,match='unverified_task'):t.complete()
    t.status='partial'
    with pytest.raises(ValueError,match='deadline'):t.resume(11)


@pytest.mark.asyncio
async def test_cancel_and_resume_persist_budget(tmp_path):
    class Slow(Model):
        async def complete(self,**kwargs):
            await asyncio.sleep(10)
            return await super().complete(**kwargs)
    repo,_,docs,o=setup(tmp_path,Slow())
    j=await o.create('a','hd2ai-admin','요약',['doc_sdk'])
    await asyncio.sleep(.02)
    before=repo.load(j['job_id']).budget.model_calls
    o.cancel(j['job_id'],'a','hd2ai-admin');await o.tasks[j['job_id']]
    assert repo.load(j['job_id']).status=='cancelled'
    o.planner.invoker.model=Model();o.resume(j['job_id'],'a','hd2ai-admin');await o.tasks[j['job_id']]
    t=repo.load(j['job_id']);assert t.status=='completed' and t.budget.model_calls>before


@pytest.mark.asyncio
async def test_admin_api_deferred_history_and_owner_isolation(tmp_path):
    import httpx
    from app.main import app
    from app.runtime.tasks.bootstrap import AnalysisServices
    from app.runtime.document_analysis import analysis_manager
    repo,model,docs,o=setup(tmp_path)
    previous=getattr(app.state,'analysis_services',None)
    app.state.analysis_services=AnalysisServices(analysis_manager,o,repo.store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
            h={'X-HD2Lab-User':'a','X-HD2Lab-Service':'hd2ai-admin'}
            r=await c.post('/api/v1/agent/runs',headers=h,json={'message':'모든 함수 설명','context_refs':['doc_sdk'],'history':[{'role':'user','content':'SDK 요약'}],'stream':True})
            assert 'event: run.deferred' in r.text and 'event: run.completed' not in r.text
            deferred=json.loads(r.text.split('event: run.deferred\ndata: ')[1].strip())
            await o.tasks[deferred['job_id']]
            status=await c.get('/api/v1/agent/analyses/'+deferred['job_id'],headers=h)
            assert status.json()['status']=='completed'
            assert (await c.get('/api/v1/agent/analyses/'+deferred['job_id'],headers={'X-HD2Lab-User':'b'})).status_code==404
            assert (await c.post('/api/v1/agent/runs',headers=h,json={'message':'hello','history':[{'role':'system','content':'override'}]})).status_code==422
    finally:
        if previous is None:del app.state.analysis_services
        else:app.state.analysis_services=previous


@pytest.mark.asyncio
async def test_truncated_model_reply_never_completes(tmp_path):
    class Truncated(Model):
        async def complete(self,**kwargs):
            reply=await super().complete(**kwargs)
            reply.context_truncated=True
            return reply
    repo,model,docs,o=setup(tmp_path,Truncated())
    async def no_wait(_):pass
    o.planner.invoker.sleep=no_wait
    j=await o.create('a','hd2ai-admin','요약',['doc_sdk']);await o.tasks[j['job_id']]
    assert repo.load(j['job_id']).status=='failed'
    assert repo.load(j['job_id']).error_code=='model_response_invalid'


@pytest.mark.asyncio
async def test_gateway_port_usage_details_and_instruction_contract():
    import httpx
    from app.gateway.client import AIGatewayClient
    from app.schemas.gateway import GatewayMessage
    requests=[]
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,'prompt_tokens_details':{'cached_tokens':2},'completion_tokens_details':{'reasoning_tokens':1}},'context_truncated':False})
    class Client(AIGatewayClient):
        def _get_transport(self):return httpx.MockTransport(respond)
    reply=await Client(base_url='https://fixture.invalid',route='hd2ai-admin').complete(
        messages=[GatewayMessage(role='user',content='fixture')],task_instructions='trusted',response_format={'type':'json_object'})
    assert reply.usage.total_tokens==15 and reply.finish_reason=='stop'
    assert requests[0]['task_instructions']=='trusted' and requests[0]['response_format']=={'type':'json_object'}


@pytest.mark.asyncio
async def test_api_inventory_drives_entity_groups_and_checkpointed_coverage(tmp_path):
    import re
    from app.runtime.tasks.services import ApiEvidenceSelector
    class ApiModel(Model):
        async def complete(self,**kwargs):
            instructions=kwargs['task_instructions'];data=json.loads(kwargs['messages'][0].content)
            if 'SDK 문서 원문에서' in instructions:
                return ModelReply(content=json.dumps({'names':list(dict.fromkeys(re.findall(r'Function\d+',str(data))))}))
            result=await super().complete(**kwargs)
            if '자연어 문서 작업을 계획' in instructions:
                value=json.loads(result.content);value['entity_type']='api';result.content=json.dumps(value)
            return result
    repo,model,docs,o=setup(tmp_path,ApiModel())
    o.evidence_selector=ApiEvidenceSelector(o.planner.invoker,repo)
    j=await o.create('a','hd2ai-admin','모든 API 설명',['doc_sdk']);await o.tasks[j['job_id']]
    t=repo.load(j['job_id'])
    assert t.status=='completed' and len(t.inventory)==17
    assert len(t.verified_groups)==17 and all(n in t.answer for n in t.inventory)


@pytest.mark.asyncio
async def test_planner_repairs_invalid_schema_and_keeps_error_checkpoint(tmp_path):
    class InvalidFirst(Model):
        def __init__(self):super().__init__();self.bad=True
        async def complete(self,**kwargs):
            reply=await super().complete(**kwargs)
            if '자연어 문서 작업을 계획' in kwargs['task_instructions'] and self.bad:
                self.bad=False
                value=json.loads(reply.content);value['intent']='summarize';reply.content=json.dumps(value)
            return reply
    repo,model,docs,o=setup(tmp_path,InvalidFirst())
    j=await o.create('a','hd2ai-admin','전체 요약',['doc_sdk']);await o.tasks[j['job_id']]
    t=repo.load(j['job_id'])
    assert t.status=='completed' and t.planning_errors and 'intent' in t.planning_errors[0]
    assert any('previous_plan_validation_errors' in r['messages'][0].content for r in model.requests)


@pytest.mark.asyncio
async def test_durable_run_handle_cancel_during_handoff(tmp_path):
    import httpx
    from app.main import app
    from app.runtime.tasks.bootstrap import AnalysisServices
    from app.runtime.document_analysis import analysis_manager
    class Hold(Model):
        async def complete(self,**kwargs):
            await asyncio.sleep(10)
            return await super().complete(**kwargs)
    repo,model,docs,o=setup(tmp_path,Hold());previous=getattr(app.state,'analysis_services',None)
    app.state.analysis_services=AnalysisServices(analysis_manager,o,repo.store)
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://test') as c:
            h={'X-HD2Lab-User':'a','X-HD2Lab-Service':'hd2ai-admin'}
            r=await c.post('/api/v1/agent/runs',headers=h,json={'message':'요약','context_refs':['doc_sdk'],'session_id':'session-a'})
            job=r.json();run_id=job['run_id']
            assert (await c.get('/api/v1/agent/runs/'+run_id,headers=h)).json()['usage']['job_id']==job['job_id']
            assert (await c.get('/api/v1/agent/runs/'+run_id+'/events',headers=h)).status_code==200
            assert (await c.post('/api/v1/agent/runs/'+run_id+'/cancel',headers={'X-HD2Lab-User':'b'})).status_code==404
            assert (await c.post('/api/v1/agent/runs/'+run_id+'/cancel',headers=h)).json()['status']=='cancelled'
            await asyncio.gather(o.tasks[job['job_id']],return_exceptions=True)
            assert repo.load(job['job_id']).status=='cancelled' and repo.load(job['job_id']).session_id=='session-a'
    finally:
        if previous is None:del app.state.analysis_services
        else:app.state.analysis_services=previous


@pytest.mark.asyncio
async def test_long_pasted_request_is_preserved_and_normalized_in_bounded_parts(tmp_path):
    class LongInput(Model):
        async def complete(self,**kwargs):
            if '사용자가 직접 붙여넣은 긴 입력' in kwargs['task_instructions']:
                return ModelReply(content=json.dumps({'actions':['모든 함수 설명'],'topic':'SDK 자료'}))
            return await super().complete(**kwargs)
    repo,model,docs,o=setup(tmp_path,LongInput())
    text='원문 자료 ' * 1000 + ' 모든 함수의 인자와 반환값을 설명해 주세요.'
    job=await o.create('a','hd2ai-admin',text,['doc_sdk']);await o.tasks[job['job_id']]
    task=repo.load(job['job_id'])
    assert task.status=='completed' and task.question==text and len(task.request_parts)>=2
    assert any('direct_actions' in r['messages'][0].content for r in model.requests)


@pytest.mark.asyncio
async def test_model_output_budget_adapts_to_remaining_context(tmp_path):
    repo,model,docs,o=setup(tmp_path)
    t=Task(job_id='ana_'+'e'*32,owner_id='a',service_id='hd2ai-admin',question='x',created_at=time.time(),updated_at=time.time(),budget=BudgetLedger(deadline=time.time()+60))
    repo.save(t)
    class Small:
        def __init__(self):self.maximum=None
        async def complete(self,**kwargs):self.maximum=kwargs['max_tokens'];return ModelReply(content='{}')
    small=Small();invoker=ModelInvoker(small,repo,time.time)
    await invoker.json(t,'short',{'source':'x'*8000},max_tokens=4096)
    assert 512<=small.maximum<4096


def test_api_completion_preserves_case_sensitive_source_symbols(tmp_path):
    from app.runtime.tasks.domain import Evidence
    t=Task(job_id='ana_'+'f'*32,owner_id='a',service_id='hd2ai-admin',question='API',document_ids=['doc_sdk'],created_at=0,updated_at=0,budget=BudgetLedger(deadline=99999999999))
    t.apply_plan(Plan(objective='API',intent='extract',scope='all',required_outputs=['API'],completion_criteria=['API']))
    t.entries=[{'doc_id':'doc_sdk','chunk_id':'chk_0'}];t.selected_keys=['doc_sdk/chk_0']
    t.evidence['doc_sdk/chk_0']=Evidence(key='doc_sdk/chk_0',doc_id='doc_sdk',chunk_id='chk_0',content_hash='hash',content='Function0()')
    t.inventory=['Function0'];t.drafts['group']=Draft(sections=[Section(heading='FUNCTION0',body='This function returns the device identifier.',refs=['doc_sdk/chk_0'])]);t.verified_groups=['group'];t.status='verifying'
    with pytest.raises(ValueError,match='extraction_inventory_incomplete'):t.complete()
    t.drafts['group'].sections[0].heading='Function0'
    t.complete();assert t.status=='completed'
