# 통합 문서 참조·근거 추적 계약 — `unified-document-reference` (WO-R21-ADD-01)

상태: R2.1 브랜치(`feat/r21-document-knowledge-engine`) 구현 완료 · 운영 배포 없음
계약 버전: `dk/v1` (추가 확장 id `unified-document-reference`, `evidence-coverage`,
`bounded-enrichment`, `synthesis-budget-reservation`)
기준 커밋: AGCore `59676b2` + HOTFIX-08A 후보 `2a8fbd1` 통합

이 문서는 `docs/r21/DOCUMENT_KNOWLEDGE_CONTRACT.md`의 추가 장이다. 기존 `dk/v1` 필드는
그대로이고, 아래 구조만 새로 export 된다. 구현은 `app/knowledge/references.py`(순수 계약,
R3 `feat/r3-workflow-extensibility`에서 그대로 import)와 런타임 배선
(`app/runtime/runner.py`, `app/runtime/graph.py`, `app/runtime/langgraph_r1.py`, `app/api/agent.py`)으로 나뉜다.

---

## 1. 왜 필요한가 (R2-BETA-HOTFIX-08A 이관 범위)

08A까지의 실패는 "모델이 첨부만 읽고 정상 `completed`로 끝났다"는 것이다. 원인은 세 가지였다.

1. Chat 요청에는 세션 첨부 id(`context_refs`)만 실리고, **워크스페이스 파일을 지목하는 입력 경로가 없었다.**
2. 실행 전에는 "특정 파일을 지정한 분석"과 "워크스페이스에서 이어가는 단순 대화"를 구분할 구조적 근거가 없었다.
3. 도구 결과는 남았지만 **요청한 자료별 근거 확보 여부**를 추적하지 않아, 누락을 답변 상태로 표현할 수 없었다.

ADD-01은 이 세 가지를 하나의 계약으로 닫는다. 단순 Chat은 기존 경로를 그대로 쓴다.

---

## 2. 공통 `DocumentReference`

```json
{
  "source_type": "workspace_file" | "session_attachment",
  "document_id": "wf_..." | "att-...",
  "file_id": "wf_... | att-...",
  "name": "R2-BETA-WORKSPACE-731.pdf",
  "mime_type": "application/pdf",
  "scope": {"workspace_id": "ws_...", "conversation_id": "conv-...", "session_id": "conv-..."},
  "revision": 1,
  "analysis_target": "full | outline | pages | section | search",
  "citation_ref": "workspace_file:wf_...@1",
  "origin": "explicit | natural_language",
  "availability": "available | missing | unavailable",
  "matched_by": ["name" | "explicit_id" | "<토큰>"],
  "metadata": {"path": "docs/R2-BETA-WORKSPACE-731.pdf", "size_bytes": 1234}
}
```

* `citation_ref`는 결정적이다: `<source>:<document_id>@<revision>[#<locator>]`.
  같은 자료·개정·위치면 REST 응답, SSE 프레임, 클라이언트 상태에서 문자열이 동일하다.
* `scope`는 인증으로 검증된 값만 들어간다. 브라우저가 보낸 테넌트/소유자 헤더는 쓰지 않는다.
* `analysis_target`은 요청 문장에서 추론한다(`목차`→outline, `N페이지`→pages, `검색`→search 등).

## 3. `available_refs` vs `requested_refs`

`ReferenceResolution`이 둘을 구조적으로 분리한다.

| 필드 | 의미 | 결정 방법 |
| --- | --- | --- |
| `available_refs` | 이 대화가 읽을 수 있는 자료 | 대화에 바인딩된 워크스페이스 파일 인벤토리(메타데이터만) + 이번 턴에 바인딩된 세션 첨부 |
| `requested_refs` | 사용자가 실제로 분석을 요청한 자료 | ① `context_refs`에 담긴 선택 id(`att-...`, `wf_...`) ② 요청 문장의 파일명·제목·식별자 매칭 |
| `unresolved` | 요청됐지만 확인되지 않은 참조 | `attachment_not_bound_to_conversation`, `workspace_file_not_found` |

* `requested_refs`가 비면 `route="chat"` — 문서 검색도, 추가 모델 호출도 없다(기존 경로).
* 워크스페이스 인벤토리는 "읽을 수 있는 후보"일 뿐 요청이 아니다. 이름이 불리지 않으면 `requested`가 되지 않는다.
* 워크스페이스 파일 id(`wf_...`)를 `context_refs`로 보내려면 실행에 워크스페이스 바인딩이 있어야 한다
  (`workspace_reference_requires_binding`, 422).

## 4. `EvidenceCoverage`

요청된 `requested_refs` 각각에 대해 실제 읽기·검색 근거가 확보됐는지 기록한다.

```json
{
  "status": "not_requested | covered | partial | missing",
  "requested": 2, "covered": 1, "missing": 1, "failed": 0,
  "entries": [{
    "reference": {"...": "DocumentReference"},
    "status": "covered | missing | failed",
    "tool_calls": 2, "reads": 1, "search_calls": 1,
    "chunks": ["chk_9"], "pages": [1],
    "citation_refs": ["workspace_file:wf_...@1#chk_9:p1", "cit_9"],
    "missing_reason": "no_matching_evidence",
    "last_error_code": "no_matches"
  }],
  "enrichment": {"attempts": 1, "limit": 2, "attempted_refs": ["workspace_file:wf_..."]},
  "budget": {"reserved_tokens": 2400, "evidence_tokens": 9600, "total_input_budget_tokens": 12000}
}
```

규칙:

* **목차(outline)는 근거가 아니다.** 구조만 확인한 자료는 `missing`으로 남는다.
* 검색 결과 0건은 `no_matching_evidence`, 읽기 실패는 `failure_reason()` 매핑
  (`document_not_referenced`→`reference_not_authorized`, 5xx→`provider_unavailable` 등)으로 사유를 남긴다.
* 워크스페이스 검색 결과가 다른 파일만 인용하면 그 파일의 근거로 치지 않는다.
* 한 번 확보한 근거는 이후 실패로 되돌리지 않는다(다운그레이드 금지).
* `status`는 `covered`(전부 확보) / `partial`(일부) / `missing`(전부 미확보) / `not_requested`.

## 5. 제한적 추가 검색 (Bounded Enrichment)

* 트리거: 모델이 도구 호출 없이 답변을 내려는 시점에 `requested_refs` 중 근거가 없는 자료가 남아 있을 때.
* 범위: 이미 인증된 대화 범위 안의 기존 조회 도구만 쓴다.
  * 세션 첨부 → `document.search(doc_id, query=<사용자 요청>, limit=4)`
  * 워크스페이스 파일 → `workspace_retrieve(query=<사용자 요청>, file_ids=[wf_...], top_k=4)`
* 상한: 실행당 `AGCORE_EVIDENCE_ENRICHMENT_LIMIT`(기본 2)회, 자료별 1회. 같은 자료를 반복 조회하지 않는다.
* 이때 모델이 스트리밍하던 초안 답변은 클라이언트로 내보내지 않는다(중복 답변 방지). 근거 확보 후의
  최종 합성만 `answer.delta`로 전달한다.
* 새로운 검색 엔진은 만들지 않았다 — R2.1 RAG(`r2-w/v1+a02`, `dk/v1`) 경로 재사용.

## 6. 합성 토큰 예산 예약

* 문서 근거가 요청된 실행에서는 입력 예산의 `AGCORE_SYNTHESIS_RESERVE_RATIO`(기본 0.2, 최소 256토큰)를
  **최종 합성용으로 먼저 예약**한다.
* 도구 결과는 `evidence_budget = input_budget - reserve` 안에서만 배분되므로, 중간 도구 출력이 커져도
  마지막 합성 호출의 자리가 사라지지 않는다. 실제 값은 응답의 `synthesis_budget`으로 노출된다.
* 예약량은 실행 상태(`AgentState.synthesis_reserve_tokens`)에 남아 재현·감사 가능하다.

## 7. 종료 상태

* 요청 자료의 근거가 모두 확보 → `completed`
* 일부/전부 미확보 → **`partial`** (정상 완료로 처리하지 않는다)
  * 답변 본문은 그대로 반환하고, 확보한 근거 + 누락 자료 + 누락 사유를 함께 구조화해 반환한다.
  * `run.failed`로 끝내지 않는다 — 사용자는 부분 답변과 이유를 함께 받는다.
* 단순 Chat(요청 자료 없음) → 기존과 동일하게 `completed`

## 8. REST · SSE · 클라이언트 전달

### REST `GET /api/v1/agent/runs/{run_id}`

`AgentRunResponse` 추가 필드(모두 optional/기본값 있음):

| 필드 | 내용 |
| --- | --- |
| `requested_refs` | 요청된 자료의 `citation_ref` 목록 |
| `available_refs` | 대화가 읽을 수 있는 자료의 `citation_ref` 목록 |
| `unresolved_refs` | 확인되지 않은 참조 + 사유 |
| `missing_references` | 근거를 확보하지 못한 `citation_ref` |
| `partial_reasons` | `citation_ref → 사유` |
| `evidence_coverage` | 위 `EvidenceCoverage` 전체 |
| `synthesis_budget` | 예약/근거 토큰 |

### SSE

* `evidence.coverage` — 도구 배치가 끝날 때마다 `status/covered/missing/missing_references/partial_reasons`를 보낸다(진행 표시용).
* `run.completed` — 종료 프레임은 그대로 `run.completed`이며, `status`(=payload `status`)가 `partial`일 수 있다.
  payload에는 `partial`, `references`, `evidence_coverage`, `missing_references`, `partial_reasons`, `budget`이 실린다.
  클라이언트는 `run.completed`를 계속 종료 신호로 쓰면 되고, `partial`이면 부분 완료로 표시한다.

### HD2AI 클라이언트

* `hd2ai-shared/documentReference.ts` — 계약 타입(`DocumentReference`, `ReferenceResolution`,
  `EvidenceCoverage`, `PartialRunReport`)과 표시 도우미(`referenceLabel`, `coverageNote`).
* `hd2ai-shared/agentClient.ts` — `run.completed`의 `partial`을 `onStatus('partial')`으로 전달하고,
  `onMetadata`로 `references/evidence_coverage/missing_references/partial_reasons`를 넘긴다.
  누락 자료가 있으면 도구 스텝에 `근거 부족` 항목과 사유를 남긴다.
* `hd2ai-admin` — `services/knowledge.ts`가 같은 타입을 재-export해 검색·참조 화면과 런타임 상태가
  같은 문구·같은 `citation_ref`를 쓴다.

## 9. R3 재사용

`app/knowledge/references.py`는 HTTP·워크스페이스 런타임·에이전트 상태에 의존하지 않는 순수 모듈이다.
`feat/r3-workflow-extensibility`의 워크플로 노드는 다음만 import 하면 된다.

```python
from app.knowledge.references import (
    DocumentReference, ReferenceResolution, EvidenceCoverage, EvidenceTracker,
    resolve_references, plan_enrichment,
)
```

TS 쪽은 `hd2ai-shared/documentReference.ts`를 그대로 import 한다. 필드 추가는 허용, 삭제·개명은 `dk/v2`.

## 10. 검증

| 항목 | 테스트 |
| --- | --- |
| Workspace PDF(`R2-BETA-WORKSPACE-731`) + 세션 첨부 PDF(`R2-BETA-ATTACHMENT-428`)를 한 응답에서 인용 | `tests/test_r21_unified_references.py::test_workspace_and_attachment_pdfs_are_cited_in_one_answer` |
| 자료 일부 미확보 → `partial` + 누락 사유 (REST·SSE) | `::test_unreadable_reference_ends_partial_with_reasons` |
| 단순 Chat: 문서 검색 0건, 모델 호출 1회 | `::test_simple_chat_does_not_search_or_extra_call` |
| 계약·근거 추적·보강 상한 단위 검증 | `::test_contract_is_exported_for_runtime_and_r3_reuse` 외 5건 |
| 권한 격리 회귀(타 대화 첨부 404, 타 대화 참조 실행 422, Admin 세션 첨부 403) | `::test_workspace_and_attachment_pdfs_are_cited_in_one_answer`, `tests/test_r2_integration_cp05.py::test_session_attachment_workspace_and_parser_scope` |
| 기존 R2.1/R2 회귀 | `python3 -m pytest tests -q` (기준선 실패 1건 제외 전부 통과, 아래 §11) |

## 11. 기준선 실패 1건

`tests/test_r1_2_final_addendum.py::test_add_02_durable_analysis_real_orchestrator_create_resume_and_no_legacy_fallback`
는 이번 변경 이전 커밋 `59676b2`에서도 동일하게 실패한다(`'_DummyNoop' object has no attribute 'create'`,
R1.2 durable 분석 부트스트랩 테스트). ADD-01과 무관하며 손대지 않았다.
