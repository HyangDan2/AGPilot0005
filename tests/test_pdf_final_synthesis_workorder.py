import json
import time
import httpx
import pytest

from app.gateway.client import AIGatewayClient
from app.gateway.wire import ToolNameCodec
from app.runtime.tasks.citations import SourceCitationResolver
from app.runtime.tasks.composition import (
    AnswerComposer,
    CompositionVerifier,
    serialized_json_length,
)
from app.runtime.tasks.domain import (
    BudgetLedger,
    Draft,
    Evidence,
    Plan,
    Section,
    Task,
    Verification,
)
from app.runtime.tasks.orchestrator import TaskOrchestrator
from app.runtime.document_analysis import AnalysisStore
from app.runtime.tasks.ports import JsonTaskRepository as TaskRepository
from app.runtime.tasks.services import ModelInvoker
from app.schemas.gateway import ModelReply, ModelUsage as TokenUsage


SHIPPING_DOC_ID = 'doc_642fc3bfb4fc'

# Actual 2021-08-09 Weekly Shipping News (VOL 532) chunk excerpts & structure
SHIPPING_CHUNKS = {
    f'{SHIPPING_DOC_ID}/chk_0001': (1, '[1페이지] 주간 해운 뉴스 2021.08.09 VOL 532. 목차: 1. 주간뉴스, 2. 건화물선 시장, 3. 컨테이너선 시장, 4. 탱커선 시장, 5. 중고선 시장, 6. 주요 해운지표.'),
    f'{SHIPPING_DOC_ID}/chk_0002': (2, '[2페이지] 1. 주간뉴스: 머스크, 2분기 역대 최대 실적 기록 및 연간 가이던스 상향. MSC 선복량 400만 TEU 돌파. 팬오션 2분기 영업이익 1,120억 원 달성.'),
    f'{SHIPPING_DOC_ID}/chk_0003': (3, '[3페이지] 미 연방해사위원회(FMC), 선사들의 체선료·지체료(D&D) 부과 관행 조사 강화. 중국 닝보항 및 옌톈항 방역 강화로 선박 대기 증가.'),
    f'{SHIPPING_DOC_ID}/chk_0004': (4, '[4페이지] IMO 온실가스 감축 단기조치(EEXI, CII) 채택에 따른 노후선 친환경 개조 및 감속 운항 전망.'),
    f'{SHIPPING_DOC_ID}/chk_0005': (5, '[5페이지] 2. 건화물선 시장: BDI는 전주 대비 80p 상승한 3,371p 기록. 케이프사이즈 운임 강세 지속.'),
    f'{SHIPPING_DOC_ID}/chk_0006': (6, '[6페이지] 케이프사이즈(BCI): 4,335p(전주비 +169p), 5TC 평균운임 35,954달러/일(+1,401달러). 중국 항만 체선 심화.'),
    f'{SHIPPING_DOC_ID}/chk_0007': (7, '[7페이지] 파나막스(BPI): 3,473p(전주비 -26p), 4TC 31,259달러/일. 수프라막스(BSI): 3,025p(+66p), 10TC 33,279달러/일.'),
    f'{SHIPPING_DOC_ID}/chk_0008': (8, '[8페이지] 핸디사이즈(BHSI): 1,829p(+32p), 7TC 32,918달러/일. 흑해·남미 곡물 물동량 견조.'),
    f'{SHIPPING_DOC_ID}/chk_0009': (9, '[9페이지] 3. 컨테이너선 시장: SCFI는 4,187.58p로 전주 대비 91.32p(2.2%) 상승하며 사상 최고치 경신.'),
    f'{SHIPPING_DOC_ID}/chk_0010': (10, '[10페이지] 미주 서안 노선 5,525달러/FEU(+139달러), 미주 동안 10,085달러/FEU(+235달러), 유럽 노선 7,395달러/TEU(+18달러).'),
    f'{SHIPPING_DOC_ID}/chk_0011': (11, '[11페이지] 컨테이너 용선지수(HRCI) 2,866p(+52p). 북미 내륙 철도·트럭 병목과 항만 혼잡 지속.'),
    f'{SHIPPING_DOC_ID}/chk_0012': (12, '[12페이지] 4. 탱커선 시장: VLCC 중동-중국 노선 WS 31.0(-0.5p), 일일 수익 -5,200달러로 약세 지속.'),
    f'{SHIPPING_DOC_ID}/chk_0013': (13, '[13페이지] 수에즈막스 서아프리카-유럽 WS 55.0(+1.2p), 아프라막스 중동-싱가포르 WS 89.5(+2.0p).'),
    f'{SHIPPING_DOC_ID}/chk_0014': (14, '[14페이지] 제품선(CPP): MR 탱커 동북아-호주 노선 운임 회복세, LR1/LR2 중동-일본 WS 보합.'),
    f'{SHIPPING_DOC_ID}/chk_0015': (15, '[15페이지] 5. 중고선 시장: 주간 총 34척(185만 DWT), 약 7.4억 달러 거래 성사. 건화물선과 컨테이너선 중심 매매 활발.'),
    f'{SHIPPING_DOC_ID}/chk_0016': (16, '[16페이지] 5년 선령 중고선가: 케이프 18만 DWT 4,650만 달러, 파나막스 8.2만 DWT 3,150만 달러, 아프라막스 11.5만 DWT 4,200만 달러.'),
    f'{SHIPPING_DOC_ID}/chk_0017': (17, '[17페이지] 신조선가지수 145p, 중고선가지수 153p 기록. 컨테이너선 단기 확보 수요로 중고선 프리미엄 지속.'),
    f'{SHIPPING_DOC_ID}/chk_0018': (18, '[18페이지] 6. 주요 해운지표: 싱가포르 VLSFO 538.5달러/톤(-7.0달러), MGO 596.0달러/톤, HSFO 404.0달러/톤.'),
    f'{SHIPPING_DOC_ID}/chk_0019': (19, '[19페이지] 원/달러 환율 1,144.2원, WTI 유가 68.28달러/배럴(-5.7%), 중국 철광석 수입가격 171.8달러/톤.'),
    f'{SHIPPING_DOC_ID}/chk_0020': (20, '[20페이지] 해체선 시장: 방글라데시·파키스탄 해체선가 580~600달러/LDT 수준 유지. 발행인: 한국해양수산개발원(KMI), 문의처: 해운빅데이터연구센터.'),
}


def build_shipping_task_and_inputs():
    now = time.time()
    evidence = {
        key: Evidence(
            key=key,
            doc_id=SHIPPING_DOC_ID,
            chunk_id=key.split('/')[1],
            content_hash=f'hash_{page}',
            content=text,
            source={'page': page},
        )
        for key, (page, text) in SHIPPING_CHUNKS.items()
    }
    task = Task(
        job_id='ana_' + '9' * 32,
        owner_id='user_1',
        service_id='hd2ai-admin',
        question='2021-08-09 주간 해운 뉴스 PDF 전체 내용을 요약해줘',
        document_ids=[SHIPPING_DOC_ID],
        created_at=now,
        updated_at=now,
        budget=BudgetLedger(deadline=now + 300, max_model_calls=40, max_tool_calls=40),
        reasoning_effort='medium',
        plan=Plan(
            intent='summary',
            scope='all',
            objective='2021-08-09자 주간 해운 뉴스(VOL 532) 전체 내용을 주제별로 요약',
            required_outputs=['주간 해운 뉴스 주요 이슈', '건화물선·컨테이너선·탱커선 시황', '중고선 시장 및 주요 해운지표'],
            completion_criteria=['전체 문서 범위 반영', '주제별 구분 및 정확한 수치·단위·출처 보존'],
        ),
        evidence=evidence,
    )
    # Intermediate verified batch outputs (note batch 1 contains the problematic 1~3 page claim from the bug report)
    inputs = [
        {
            'id': 'group_0',
            'sections': [
                {
                    'heading': '주간 해운 뉴스 (초반 묶음)',
                    'body': '이 문서는 1~3페이지 내용이다. 2021년 8월 9일자 VOL 532 주간 해운 뉴스로 머스크 2분기 역대 최대 실적, MSC 400만 TEU 돌파, 팬오션 영업이익 1,120억 원, 미 FMC 체선료 조사 강화, 중국 항만 방역 강화, IMO EEXI·CII 채택을 다룬다.',
                    'refs': [f'{SHIPPING_DOC_ID}/chk_0001', f'{SHIPPING_DOC_ID}/chk_0002', f'{SHIPPING_DOC_ID}/chk_0003', f'{SHIPPING_DOC_ID}/chk_0004'],
                    'labels': [],
                }
            ],
        },
        {
            'id': 'group_1',
            'sections': [
                {
                    'heading': '건화물선 시황',
                    'body': 'BDI는 3,371p(+80p)로 상승했다. BCI 4,335p(+169p, 5TC 35,954달러/일), BPI 3,473p(-26p, 4TC 31,259달러/일), BSI 3,025p(+66p, 10TC 33,279달러/일), BHSI 1,829p(+32p, 7TC 32,918달러/일)를 기록했다.',
                    'refs': [f'{SHIPPING_DOC_ID}/chk_0005', f'{SHIPPING_DOC_ID}/chk_0006', f'{SHIPPING_DOC_ID}/chk_0007', f'{SHIPPING_DOC_ID}/chk_0008'],
                    'labels': [],
                }
            ],
        },
        {
            'id': 'group_2',
            'sections': [
                {
                    'heading': '컨테이너선 및 탱커선 시황',
                    'body': 'SCFI는 4,187.58p(+91.32p, +2.2%)로 최고치를 경신했다(미주 서안 5,525달러/FEU, 미주 동안 10,085달러/FEU, 유럽 7,395달러/TEU, HRCI 2,866p). 탱커선은 VLCC 중동-중국 WS 31.0(-0.5p, 일일 -5,200달러), 수에즈막스 WS 55.0, 아프라막스 WS 89.5를 기록했다.',
                    'refs': [
                        f'{SHIPPING_DOC_ID}/chk_0009', f'{SHIPPING_DOC_ID}/chk_0010', f'{SHIPPING_DOC_ID}/chk_0011',
                        f'{SHIPPING_DOC_ID}/chk_0012', f'{SHIPPING_DOC_ID}/chk_0013', f'{SHIPPING_DOC_ID}/chk_0014',
                    ],
                    'labels': [],
                }
            ],
        },
        {
            'id': 'group_3',
            'sections': [
                {
                    'heading': '중고선 시장 및 주요 해운지표',
                    'body': '중고선은 주간 34척(185만 DWT, 7.4억 달러)이 거래되었고 신조선가지수 145p, 중고선가지수 153p를 기록했다. 싱가포르 VLSFO 538.5달러/톤, WTI 68.28달러/배럴, 원/달러 환율 1,144.2원, 철광석 171.8달러/톤, 해체선가 580~600달러/LDT이다.',
                    'refs': [
                        f'{SHIPPING_DOC_ID}/chk_0015', f'{SHIPPING_DOC_ID}/chk_0016', f'{SHIPPING_DOC_ID}/chk_0017',
                        f'{SHIPPING_DOC_ID}/chk_0018', f'{SHIPPING_DOC_ID}/chk_0019', f'{SHIPPING_DOC_ID}/chk_0020',
                    ],
                    'labels': [],
                }
            ],
        },
    ]
    task.drafts = {
        item['id']: Draft.model_validate({'sections': item['sections']})
        for item in inputs
    }
    return task, inputs


def valid_shipping_final_draft_dict():
    return {
        'sections': [
            {
                'heading': '주간 해운 뉴스 주요 이슈',
                'body': '2021년 8월 9일자(VOL 532) 주간 해운 뉴스에서는 머스크의 2분기 역대 최대 실적 및 연간 가이던스 상향, MSC 선복량 400만 TEU 돌파, 팬오션 2분기 영업이익 1,120억 원 달성, 미 FMC의 체선료·지체료(D&D) 조사 강화, 중국 주요 항만 방역 강화에 따른 체선 증가, IMO 단기 온실가스 감축 조치(EEXI·CII) 채택 영향을 다루었다.',
                'refs': [f'{SHIPPING_DOC_ID}/chk_0001', f'{SHIPPING_DOC_ID}/chk_0002', f'{SHIPPING_DOC_ID}/chk_0003', f'{SHIPPING_DOC_ID}/chk_0004'],
                'labels': [],
            },
            {
                'heading': '건화물선 시황',
                'body': 'BDI는 전주 대비 80p 상승한 3,371p를 기록했다. 케이프사이즈(BCI 4,335p, 5TC 35,954달러/일), 수프라막스(BSI 3,025p, 10TC 33,279달러/일), 핸디사이즈(BHSI 1,829p, 7TC 32,918달러/일)는 중국 항만 체선과 곡물 물동량 견조로 상승했고, 파나막스(BPI 3,473p, 4TC 31,259달러/일)는 소폭 하락했다.',
                'refs': [f'{SHIPPING_DOC_ID}/chk_0005', f'{SHIPPING_DOC_ID}/chk_0006', f'{SHIPPING_DOC_ID}/chk_0007', f'{SHIPPING_DOC_ID}/chk_0008'],
                'labels': [],
            },
            {
                'heading': '컨테이너선 시황',
                'body': 'SCFI는 4,187.58p(+91.32p, +2.2%)로 사상 최고치를 경신했다. 미주 서안 5,525달러/FEU(+139달러), 미주 동안 10,085달러/FEU(+235달러), 유럽 노선 7,395달러/TEU(+18달러)로 상승했으며, 북미 내륙·항만 병목 속에 HRCI 용선지수도 2,866p(+52p)로 올랐다.',
                'refs': [f'{SHIPPING_DOC_ID}/chk_0009', f'{SHIPPING_DOC_ID}/chk_0010', f'{SHIPPING_DOC_ID}/chk_0011'],
                'labels': [],
            },
            {
                'heading': '탱커선 시황',
                'body': 'VLCC 중동-중국 노선은 WS 31.0(-0.5p), 일일 수익 -5,200달러로 약세가 이어졌으나, 수에즈막스 서아프리카-유럽(WS 55.0, +1.2p)과 아프라막스 중동-싱가포르(WS 89.5, +2.0p), 동북아-호주 MR 제품선 노선은 회복세를 보였다.',
                'refs': [f'{SHIPPING_DOC_ID}/chk_0012', f'{SHIPPING_DOC_ID}/chk_0013', f'{SHIPPING_DOC_ID}/chk_0014'],
                'labels': [],
            },
            {
                'heading': '중고선 시장 동향',
                'body': '주간 중고선 거래는 총 34척(185만 DWT, 약 7.4억 달러)이 성사되었다. 5년 선령 기준 케이프 4,650만 달러, 파나막스 3,150만 달러, 아프라막스 4,200만 달러를 기록했으며 신조선가지수는 145p, 중고선가지수는 153p로 집계되었다.',
                'refs': [f'{SHIPPING_DOC_ID}/chk_0015', f'{SHIPPING_DOC_ID}/chk_0016', f'{SHIPPING_DOC_ID}/chk_0017'],
                'labels': [],
            },
            {
                'heading': '주요 해운지표',
                'body': '싱가포르 VLSFO는 538.5달러/톤(-7.0달러), MGO 596.0달러/톤, HSFO 404.0달러/톤을 기록했고, WTI 유가 68.28달러/배럴(-5.7%), 원/달러 환율 1,144.2원, 중국 철광석 수입가격 171.8달러/톤, 서남아 해체선가 580~600달러/LDT를 나타냈다.',
                'refs': [f'{SHIPPING_DOC_ID}/chk_0018', f'{SHIPPING_DOC_ID}/chk_0019', f'{SHIPPING_DOC_ID}/chk_0020'],
                'labels': [],
            },
        ]
    }


# ---------------------------------------------------------------------------
# Case 1: 동일 PDF 전역 합성 (2021-08-09 주간 해운 뉴스, output_character_budget=7728)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shipping_news_2021_08_09_global_final_synthesis_completes(tmp_path):
    repo = TaskRepository(AnalysisStore(tmp_path), time.time)
    task, inputs = build_shipping_task_and_inputs()
    repo.save(task)

    captured_compose_payloads = []

    class ShippingSynthesisModel:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs['messages'][0].content)
            if 'verification_mode' in payload or 'answer' in payload:
                return ModelReply(
                    content=json.dumps(
                        {'satisfied': True, 'missing_requirements': [], 'unsupported_claims': []},
                        ensure_ascii=False,
                    ),
                    finish_reason='stop',
                    usage=TokenUsage(prompt_tokens=600, completion_tokens=40, total_tokens=640),
                )
            captured_compose_payloads.append(payload)
            # First attempt returns a draft that mistakenly kept the 1~3 page scope claim;
            # Second attempt (with repair + previous_answer) returns the clean final JSON.
            if len(captured_compose_payloads) == 1:
                bad = valid_shipping_final_draft_dict()
                bad['sections'][0]['body'] = '이 문서는 1~3페이지 내용이다. ' + bad['sections'][0]['body']
                return ModelReply(
                    content=json.dumps(bad, ensure_ascii=False),
                    finish_reason='stop',
                    reasoning_length=420,
                    usage=TokenUsage(prompt_tokens=1800, completion_tokens=500, total_tokens=2300),
                )
            good = valid_shipping_final_draft_dict()
            if kwargs.get('on_reasoning_delta'):
                kwargs['on_reasoning_delta']('초안 검토 중... 1~3페이지 범위 단정 문구 제거. Let\'s finalize.')
            return ModelReply(
                content=json.dumps(good, ensure_ascii=False),
                finish_reason='stop',
                reasoning_length=310,
                usage=TokenUsage(prompt_tokens=2100, completion_tokens=480, total_tokens=2580),
            )

    invoker = ModelInvoker(ShippingSynthesisModel(), repo, time.time, sleep=self_sleep)
    composer = AnswerComposer(invoker, repo, max_repairs=2)

    # Confirm exact 7,728 character budget for this payload shape when overhead is calibrated
    composer._output_budget = lambda t, inp, fin: 7728
    await composer.compose(task)
    draft = task.final_draft

    assert isinstance(draft, Draft)
    assert len(draft.sections) == 6
    assert serialized_json_length(draft) <= 7728
    rendered = draft.render()
    assert '1~3페이지' not in rendered
    assert 'Let\'s finalize' not in rendered
    for topic in ('주간 해운 뉴스', '건화물선', '컨테이너선', '탱커선', '중고선', '주요 해운지표'):
        assert any(topic in s.heading for s in draft.sections)

    # Check that attempt 2 received repair, previous_answer, and output_character_budget=7728
    assert len(captured_compose_payloads) == 2
    second_req = captured_compose_payloads[1]
    assert second_req['global_final'] is True
    assert second_req['output_character_budget'] == 7728
    assert second_req['previous_answer'] is not None
    assert second_req['repair']['satisfied'] is False
    assert any('1~3페이지' in c for c in second_req['repair']['unsupported_claims'])
    assert second_req['repair_policy']['previous_answer_role'] == 'revision_target_only_not_evidence'


# ---------------------------------------------------------------------------
# Case 2: 추론만 수신 / 추론과 정상 최종 텍스트를 함께 수신
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reasoning_only_vs_reasoning_plus_valid_final_text(tmp_path):
    repo = TaskRepository(AnalysisStore(tmp_path), time.time)

    # 2A: Model outputs reasoning ("Let's finalize.") but final content channel is empty
    task_empty, inputs = build_shipping_task_and_inputs()
    repo.save(task_empty)

    class ReasoningOnlyModel:
        async def complete(self, **kwargs):
            if kwargs.get('on_reasoning_delta'):
                kwargs['on_reasoning_delta']('Drafting final JSON... 이 문서는 1~3페이지 내용이다... Let\'s finalize.')
            return ModelReply(
                content='',
                finish_reason='stop',
                reasoning_length=245,
                usage=TokenUsage(prompt_tokens=1500, completion_tokens=245, total_tokens=1745),
            )

    invoker_empty = ModelInvoker(ReasoningOnlyModel(), repo, time.time, sleep=self_sleep)
    composer_empty = AnswerComposer(invoker_empty, repo, max_repairs=1)
    with pytest.raises(ValueError, match='model_response_invalid'):
        await composer_empty.compose(task_empty)

    assert task_empty.last_failure_reason == 'empty_final_text'
    assert any(
        d.get('stage') == 'model_response'
        and d.get('failure_reason') == 'empty_final_text'
        and d.get('content_length') == 0
        and d.get('reasoning_length') == 245
        for d in task_empty.diagnostics
    )
    assert task_empty.answer is None

    # 2B: Model outputs reasoning AND valid final JSON in content channel
    task_ok, inputs_ok = build_shipping_task_and_inputs()
    task_ok.job_id = 'ana_' + '8' * 32
    repo.save(task_ok)

    class ReasoningPlusFinalModel:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs['messages'][0].content)
            if 'verification_mode' in payload or 'answer' in payload:
                return ModelReply(
                    content=json.dumps({'satisfied': True, 'missing_requirements': [], 'unsupported_claims': []}),
                    finish_reason='stop',
                )
            if kwargs.get('on_reasoning_delta'):
                kwargs['on_reasoning_delta']('내부 추론 초안: 이 문서는 1~3페이지 내용이다. Let\'s finalize.')
            return ModelReply(
                content=json.dumps(valid_shipping_final_draft_dict(), ensure_ascii=False),
                finish_reason='stop',
                reasoning_length=180,
                usage=TokenUsage(prompt_tokens=1500, completion_tokens=520, total_tokens=2020),
            )

    invoker_ok = ModelInvoker(ReasoningPlusFinalModel(), repo, time.time, sleep=self_sleep)
    composer_ok = AnswerComposer(invoker_ok, repo, max_repairs=1)
    await composer_ok.compose(task_ok)
    draft_ok = task_ok.final_draft

    assert isinstance(draft_ok, Draft)
    assert 'Let\'s finalize' not in draft_ok.render()
    assert '내부 추론 초안' not in draft_ok.render()
    assert '1~3페이지' not in draft_ok.render()
    assert 'Let\'s finalize' in task_ok.reasoning_text


async def self_sleep(_):
    return None


# ---------------------------------------------------------------------------
# Case 3: 스트리밍 분할 / 마지막 조각 후 정상 종료 / 중도 종료
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_streaming_split_chunks_normal_done_and_midstream_disconnect(tmp_path):
    final_json_str = json.dumps(valid_shipping_final_draft_dict(), ensure_ascii=False)
    # Split multi-byte Korean JSON across multiple chunks
    part1 = final_json_str[:180]
    part2 = final_json_str[180:520]
    part3 = final_json_str[520:]

    sse_lines_complete = [
        'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': '추론 시작... '}, 'finish_reason': None}]}, ensure_ascii=False),
        'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': "Let's finalize."}, 'finish_reason': None}]}, ensure_ascii=False),
        'data: ' + json.dumps({'choices': [{'delta': {'content': part1}, 'finish_reason': None}]}, ensure_ascii=False),
        'data: ' + json.dumps({'choices': [{'delta': {'content': part2}, 'finish_reason': None}]}, ensure_ascii=False),
        'data: ' + json.dumps({'choices': [{'delta': {'content': part3}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}}, ensure_ascii=False),
        'data: [DONE]',
    ]
    sse_body_complete = '\n\n'.join(sse_lines_complete) + '\n\n'

    unary_body = json.dumps({
        'choices': [{
            'message': {
                'content': final_json_str,
                'reasoning_content': "추론 시작... Let's finalize.",
            },
            'finish_reason': 'stop',
        }],
        'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150},
    }, ensure_ascii=False)

    # Mid-stream disconnect: stops after part2 with no [DONE]
    sse_lines_interrupted = [
        'data: ' + json.dumps({'choices': [{'delta': {'reasoning_content': '추론 중...'}, 'finish_reason': None}]}, ensure_ascii=False),
        'data: ' + json.dumps({'choices': [{'delta': {'content': part1 + part2}, 'finish_reason': None}]}, ensure_ascii=False),
    ]
    sse_body_interrupted = '\n\n'.join(sse_lines_interrupted) + '\n\n'

    mode = {'current': 'stream_ok'}

    def handler(request: httpx.Request) -> httpx.Response:
        if mode['current'] == 'unary_ok':
            return httpx.Response(200, content=unary_body.encode('utf-8'), headers={'content-type': 'application/json'})
        if mode['current'] == 'stream_ok':
            return httpx.Response(200, content=sse_body_complete.encode('utf-8'), headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, content=sse_body_interrupted.encode('utf-8'), headers={'content-type': 'text/event-stream'})

    transport = httpx.MockTransport(handler)
    client = AIGatewayClient(base_url='http://test-gateway', token='tok', route='hd2ai-admin')
    codec = ToolNameCodec({})

    async with httpx.AsyncClient(transport=transport) as http_client:
        stream_reasoning = []
        reply_stream = await client._stream_request(
            http_client,
            'http://test-gateway/v1/chat/completions',
            {'messages': []},
            None,
            codec,
            on_reasoning_delta=stream_reasoning.append,
            structured=True,
        )
        mode['current'] = 'unary_ok'
        unary_reasoning = []
        reply_unary = await client._unary_request(
            http_client,
            'http://test-gateway/v1/chat/completions',
            {'messages': []},
            codec,
            structured=True,
            on_reasoning_delta=unary_reasoning.append,
        )
        mode['current'] = 'stream_interrupted'
        reply_interrupted = await client._stream_request(
            http_client,
            'http://test-gateway/v1/chat/completions',
            {'messages': []},
            None,
            codec,
            structured=True,
        )

    assert reply_stream.stream_incomplete is False
    assert reply_stream.finish_reason == 'stop'
    assert reply_stream.content == reply_unary.content == final_json_str
    assert ''.join(stream_reasoning) == ''.join(unary_reasoning) == "추론 시작... Let's finalize."
    assert reply_stream.reasoning_length == len("추론 시작... Let's finalize.")

    assert reply_interrupted.stream_incomplete is True
    assert reply_interrupted.finish_reason == 'stream_interrupted'

    # Verify ModelInvoker classifies mid-stream disconnect as 'stream_interrupted' (not json_parse_error)
    repo = TaskRepository(AnalysisStore(tmp_path), time.time)
    task, _ = build_shipping_task_and_inputs()
    task.job_id = 'ana_' + '7' * 32
    repo.save(task)

    class InterruptedStreamModel:
        async def complete(self, **kwargs):
            return reply_interrupted

    invoker = ModelInvoker(InterruptedStreamModel(), repo, time.time, sleep=self_sleep)
    with pytest.raises(ValueError, match='model_response_invalid'):
        await invoker.typed(task, 'compose', {'x': 1}, Draft, strict_json=True)

    assert task.last_failure_reason == 'stream_interrupted'
    assert all(d['failure_reason'] == 'stream_interrupted' for d in task.diagnostics)


# ---------------------------------------------------------------------------
# Case 4: 토큰 한도로 잘린 JSON / 정상 종료했지만 잘못된 JSON
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_token_limit_truncated_json_vs_normal_stop_malformed_json(tmp_path):
    repo = TaskRepository(AnalysisStore(tmp_path), time.time)

    # 4A: Token limit truncated JSON (finish_reason='length')
    task_trunc, _ = build_shipping_task_and_inputs()
    task_trunc.job_id = 'ana_' + '6' * 32
    repo.save(task_trunc)

    truncated_json = json.dumps(valid_shipping_final_draft_dict(), ensure_ascii=False)[:350]

    class TokenLimitTruncatedModel:
        def __init__(self):
            self.requested_max_tokens = []

        async def complete(self, **kwargs):
            self.requested_max_tokens.append(kwargs['max_tokens'])
            return ModelReply(
                content=truncated_json,
                finish_reason='length',
                usage=TokenUsage(prompt_tokens=1200, completion_tokens=kwargs['max_tokens'], total_tokens=1200 + kwargs['max_tokens']),
            )

    trunc_model = TokenLimitTruncatedModel()
    invoker_trunc = ModelInvoker(trunc_model, repo, time.time, sleep=self_sleep)
    with pytest.raises(ValueError, match='model_response_invalid'):
        await invoker_trunc.typed(task_trunc, 'compose', {'x': 1}, Draft, max_tokens=1024, strict_json=True)

    assert task_trunc.last_failure_reason == 'output_token_limit'
    assert any(d['failure_reason'] == 'output_token_limit' and d['finish_reason'] == 'length' for d in task_trunc.diagnostics)
    # Confirm max_tokens was scaled up before exhausting attempts
    assert trunc_model.requested_max_tokens[1] > trunc_model.requested_max_tokens[0]

    # 4B: Normal stop (finish_reason='stop') but invalid JSON / markdown fence / broken syntax
    task_parse, _ = build_shipping_task_and_inputs()
    task_parse.job_id = 'ana_' + '5' * 32
    repo.save(task_parse)

    class MalformedJsonModel:
        async def complete(self, **kwargs):
            return ModelReply(
                content='{"sections":[{"heading":"주간 뉴스","body":"잘못된 JSON 본문"',
                finish_reason='stop',
                usage=TokenUsage(prompt_tokens=1000, completion_tokens=80, total_tokens=1080),
            )

    invoker_parse = ModelInvoker(MalformedJsonModel(), repo, time.time, sleep=self_sleep)
    with pytest.raises(ValueError, match='model_response_invalid'):
        await invoker_parse.typed(task_parse, 'compose', {'x': 1}, Draft, max_tokens=1024, strict_json=True)

    assert task_parse.last_failure_reason == 'json_parse_error'
    assert all(d['failure_reason'] == 'json_parse_error' and d['finish_reason'] == 'stop' for d in task_parse.diagnostics)


# ---------------------------------------------------------------------------
# Case 5: 유효 JSON의 7,728자 경계 / 7,729자 / 잘못된 스키마 / 미허용 ref
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_json_budget_boundary_7728_vs_7729_schema_and_unauthorized_ref(tmp_path):
    repo = TaskRepository(AnalysisStore(tmp_path), time.time)
    task, inputs = build_shipping_task_and_inputs()
    repo.save(task)

    class AlwaysPassLLMVerifierModel:
        async def complete(self, **kwargs):
            return ModelReply(
                content=json.dumps({'satisfied': True, 'missing_requirements': [], 'unsupported_claims': []}),
                finish_reason='stop',
            )

    invoker = ModelInvoker(AlwaysPassLLMVerifierModel(), repo, time.time, sleep=self_sleep)
    verifier = CompositionVerifier(invoker)

    def make_draft_of_exact_serialized_length(target_len: int) -> Draft:
        base = Draft(
            sections=[
                Section(
                    heading='해운 시황 종합',
                    body='가',
                    refs=[f'{SHIPPING_DOC_ID}/chk_0001'],
                    labels=[],
                )
            ]
        )
        overhead = serialized_json_length(base) - 1
        pad_len = target_len - overhead
        assert pad_len > 0
        draft = Draft(
            sections=[
                Section(
                    heading='해운 시황 종합',
                    body='가' * pad_len,
                    refs=[f'{SHIPPING_DOC_ID}/chk_0001'],
                    labels=[],
                )
            ]
        )
        assert serialized_json_length(draft) == target_len
        return draft

    # 5A: Exact 7,728 chars boundary -> ALLOWED
    draft_7728 = make_draft_of_exact_serialized_length(7728)
    v_7728 = await verifier.verify(task, draft_7728, inputs, True, 7728)
    assert v_7728.satisfied is True
    assert v_7728.failure_reasons == []

    # 5B: Exact 7,729 chars -> REJECTED with 'character_budget_exceeded'
    draft_7729 = make_draft_of_exact_serialized_length(7729)
    v_7729 = await verifier.verify(task, draft_7729, inputs, True, 7728)
    assert v_7729.satisfied is False
    assert 'character_budget_exceeded' in v_7729.failure_reasons
    assert any('7728자' in m for m in v_7729.missing_requirements)

    # 5C: Automatic re-synthesis into compressed JSON when attempt 1 is 7,729 chars (no string truncation)
    calls = {'compose': 0}

    class CompressOnRetryModel:
        async def complete(self, **kwargs):
            payload = json.loads(kwargs['messages'][0].content)
            if 'verification_mode' in payload or 'answer' in payload:
                return ModelReply(
                    content=json.dumps({'satisfied': True, 'missing_requirements': [], 'unsupported_claims': []}),
                    finish_reason='stop',
                )
            calls['compose'] += 1
            if calls['compose'] == 1:
                return ModelReply(content=json.dumps(draft_7729.model_dump(), ensure_ascii=False), finish_reason='stop')
            return ModelReply(content=json.dumps(draft_7728.model_dump(), ensure_ascii=False), finish_reason='stop')

    invoker_compress = ModelInvoker(CompressOnRetryModel(), repo, time.time, sleep=self_sleep)
    composer_compress = AnswerComposer(invoker_compress, repo, max_repairs=2)
    composer_compress._output_budget = lambda t, inp, fin: 7728
    task_compress, _ = build_shipping_task_and_inputs()
    task_compress.job_id = 'ana_' + '4' * 32
    repo.save(task_compress)

    await composer_compress.compose(task_compress)
    final_compressed = task_compress.final_draft
    assert serialized_json_length(final_compressed) == 7728

    # 5D: Unauthorized ref -> REJECTED with 'unauthorized_ref'
    draft_bad_ref = Draft(
        sections=[
            Section(
                heading='해운 시황 종합',
                body='정상 본문',
                refs=['doc_unauthorized/chk_9999'],
                labels=[],
            )
        ]
    )
    v_bad_ref = await verifier.verify(task, draft_bad_ref, inputs, True, 7728)
    assert v_bad_ref.satisfied is False
    assert v_bad_ref.failure_reasons == ['unauthorized_ref']

    # 5E: Schema mismatch -> REJECTED with 'schema_mismatch'
    task_schema, _ = build_shipping_task_and_inputs()
    task_schema.job_id = 'ana_' + '3' * 32
    repo.save(task_schema)

    class BadSchemaModel:
        async def complete(self, **kwargs):
            return ModelReply(
                content=json.dumps({'sections': [{'heading': '제목만 있고 body/refs 누락'}]}, ensure_ascii=False),
                finish_reason='stop',
            )

    invoker_schema = ModelInvoker(BadSchemaModel(), repo, time.time, sleep=self_sleep)
    with pytest.raises(ValueError, match='model_contract_invalid'):
        await invoker_schema.typed(task_schema, 'compose', {'x': 1}, Draft, strict_json=True)
    assert task_schema.last_failure_reason == 'schema_mismatch'
    assert any(d['failure_reason'] == 'schema_mismatch' for d in task_schema.diagnostics)


# ---------------------------------------------------------------------------
# Case 6: repair 지적 주장 재등장 / 제거한 수정본 / 발행인·문의처 조건부 제거
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repair_flagged_claim_reappearance_vs_removal_and_publisher_contact(tmp_path):
    repo = TaskRepository(AnalysisStore(tmp_path), time.time)
    task, inputs = build_shipping_task_and_inputs()
    repo.save(task)

    class PassLLMModel:
        async def complete(self, **kwargs):
            return ModelReply(
                content=json.dumps({'satisfied': True, 'missing_requirements': [], 'unsupported_claims': []}),
                finish_reason='stop',
            )

    invoker = ModelInvoker(PassLLMModel(), repo, time.time, sleep=self_sleep)
    verifier = CompositionVerifier(invoker)

    repair_scope = {
        'satisfied': False,
        'missing_requirements': [],
        'unsupported_claims': ['이 문서는 1~3페이지 내용이다'],
    }

    # 6A: Paraphrased reappearance of '1~3페이지' scope claim -> FAILS
    paraphrased_scope_draft = Draft.model_validate(valid_shipping_final_draft_dict())
    paraphrased_scope_draft.sections[0].body = (
        '본 문서는 1쪽부터 3쪽까지의 내용을 정리한 자료이다. ' + paraphrased_scope_draft.sections[0].body
    )
    v_paraphrased = await verifier.verify(task, paraphrased_scope_draft, inputs, True, 7728, repair=repair_scope)
    assert v_paraphrased.satisfied is False
    assert 'unsupported_scope_claim' in v_paraphrased.failure_reasons
    assert 'repair_claim_reappeared' in v_paraphrased.failure_reasons

    # 6B: Clean draft with '1~3페이지' claim removed -> PASSES
    clean_draft = Draft.model_validate(valid_shipping_final_draft_dict())
    v_clean = await verifier.verify(task, clean_draft, inputs, True, 7728, repair=repair_scope)
    assert v_clean.satisfied is True

    # 6C: Publisher / contact ('발행인', '문의처')
    draft_with_publisher = Draft.model_validate(valid_shipping_final_draft_dict())
    draft_with_publisher.sections[-1].body += ' 발행인: 한국해양수산개발원(KMI), 문의처: 해운빅데이터연구센터.'

    # When NOT in repair.unsupported_claims -> PASSES deterministic check
    v_pub_allowed = await verifier.verify(task, draft_with_publisher, inputs, True, 7728, repair=repair_scope)
    assert v_pub_allowed.satisfied is True

    # When explicitly flagged in repair.unsupported_claims -> FAILS with 'repair_claim_reappeared'
    repair_with_publisher = {
        'satisfied': False,
        'missing_requirements': [],
        'unsupported_claims': ['발행인 및 문의처(한국해양수산개발원, 해운빅데이터연구센터) 관련 주장'],
    }
    v_pub_flagged = await verifier.verify(task, draft_with_publisher, inputs, True, 7728, repair=repair_with_publisher)
    assert v_pub_flagged.satisfied is False
    assert 'repair_claim_reappeared' in v_pub_flagged.failure_reasons

    # Once publisher/contact is removed in revised draft -> PASSES
    v_pub_removed = await verifier.verify(task, clean_draft, inputs, True, 7728, repair=repair_with_publisher)
    assert v_pub_removed.satisfied is True
