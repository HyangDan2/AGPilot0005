# AGCore 테스트 및 QA 검증 계획 (QA)

## 1. 개요
본 문서는 AGCore의 핵심 기능, 예외 상황, AI Gateway 연동, HD2AI 통합에 대한 검증 항목과 절차를 기술합니다.

## 2. 테스트 매트릭스

| ID | 범주 | 테스트 항목 | 기대 결과 |
| --- | --- | --- | --- |
| TC-01 | Core | 일반 대화 요청 (Tool 미사용) | LLM이 즉시 답변 생성 (Tool 호출 0회) |
| TC-02 | Tool | DateTime 조회 요청 | `system.get_datetime` 도구 자동 호출 후 시간 답변 생성 |
| TC-03 | Tool | 사칙연산 계산 요청 | `math.calculate` 도구 호출 후 정확한 계산 결과 생성 |
| TC-04 | Tool | 다단계 도구 호출 | 시간 조회 후 계산 등 복수 도구 순차/병렬 호출 후 통합 답변 |
| TC-05 | Safety | 악의적 수식 입력 (eval 시도) | AST 파서에서 안전하게 차단되고 정형 에러 응답 반환 |
| TC-06 | Limits | Tool 호출 상한 도달 (`max_tool_calls: 6`) | 6회 도달 시 루프 중단 및 경고와 함께 최종 정리 응답 반환 |
| TC-07 | Limits | 실행 타임아웃 (`timeout: 120s`) | 런타임에서 안전하게 인터럽트 및 `run.failed` 이벤트 발행 |
| TC-08 | Cancel | 사용자 실행 중단 요청 | `POST /runs/{id}/cancel` 수신 시 비동기 태스크 즉각 취소 및 자원 해제 |
| TC-09 | SSE | 실시간 스트리밍 검증 | `run.started` -> `tool.*` -> `answer.delta` -> `run.completed` 순서 정상 수신 |
| TC-10 | Gateway | AI Gateway 호환성 검증 | 스트리밍 Tool Call Delta 및 JSON 인자 결합 정상 동작 |
| TC-11 | UI | HD2AI 관리자 모드 토글 | OFF 시 기존 채팅, ON 시 Agent Mode 및 Trace UI 노출 |
| TC-12 | Regression | 공개 HD2AI 및 기존 채팅 | 이전 기능 및 IndexedDB 저장, 로컬 모델 경로에 영향 없음 |

## 3. 자동화 테스트 수행 절차
```bash
# 가상환경 활성화 후 테스트 실행
pytest tests/ -v
```
