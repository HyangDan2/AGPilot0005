# AGCore REST API 및 SSE 프로토콜 계약 (API_CONTRACT)

## 1. 개요
AGCore는 클라이언트(HD2AI, AF AI 등)에 일관된 REST API와 Server-Sent Events(SSE) 스트리밍 인터페이스를 제공합니다.

## 2. 기본 규약
- Base Path: `/api/v1`
- Content-Type: `application/json` (스트리밍의 경우 `text/event-stream`)
- 인증: `Authorization: Bearer <service_token>` 및 서비스 식별자 헤더

---

## 3. REST API 명세

### 3.1 Agent 실행 요청
- **엔드포인트**: `POST /api/v1/agent/runs`
- **설명**: 새로운 Agent 실행 세션을 생성하고 즉시 실행을 시작합니다.
- **Request Body**:
```json
{
  "agent_id": "general-agent",
  "message": "오늘 서울 날짜와 현재 시간을 알려주고, 25 * 40을 계산해줘.",
  "session_id": "session-12345",
  "context_refs": [],
  "stream": true,
  "options": {
    "max_steps": 10,
    "max_tool_calls": 6,
    "temperature": 0.2
  }
}
```
- **Response (200 OK or 202 Accepted)**:
```json
{
  "run_id": "run-0192a3b4-5678-789a-bcde-f0123456789a",
  "agent_id": "general-agent",
  "session_id": "session-12345",
  "status": "queued",
  "created_at": 1794132000.123
}
```

### 3.2 Agent 실행 상태 조회
- **엔드포인트**: `GET /api/v1/agent/runs/{run_id}`
- **Response (200 OK)**:
```json
{
  "run_id": "run-0192a3b4-5678-789a-bcde-f0123456789a",
  "status": "completed",
  "answer": "오늘 날짜는 2026년 11월 8일이며, 25 * 40의 계산 결과는 1,000입니다.",
  "traces": [
    {
      "step": 1,
      "type": "tool",
      "tool_name": "system.get_datetime",
      "status": "completed",
      "duration_ms": 12,
      "result_size_bytes": 48
    },
    {
      "step": 2,
      "type": "tool",
      "tool_name": "math.calculate",
      "status": "completed",
      "duration_ms": 5,
      "result_size_bytes": 10
    }
  ],
  "usage": {
    "steps": 2,
    "tool_calls": 2,
    "total_duration_ms": 850
  },
  "created_at": 1794132000.123,
  "finished_at": 1794132000.973
}
```

### 3.3 Agent 실행 취소
- **엔드포인트**: `POST /api/v1/agent/runs/{run_id}/cancel`
- **Response (200 OK)**:
```json
{
  "run_id": "run-0192a3b4-5678-789a-bcde-f0123456789a",
  "status": "cancelled",
  "message": "Agent execution cancelled by user."
}
```

### 3.4 등록된 도구 및 Agent 목록 조회
- **엔드포인트**: `GET /api/v1/agents`
- **Response (200 OK)**:
```json
{
  "agents": [
    {
      "id": "general-agent",
      "name": "General Assistant Agent",
      "description": "다양한 도구를 활용할 수 있는 범용 에이전트",
      "allowed_tools": [
        "system.echo",
        "system.get_datetime",
        "math.calculate",
        "document.get_outline",
        "document.read_section"
      ]
    }
  ]
}
```

### 3.5 헬스 체크
- **엔드포인트**: `GET /api/v1/health`
- **Response (200 OK)**:
```json
{
  "status": "healthy",
  "service": "AGCore",
  "version": "1.0.0-mvp",
  "gateway_connected": true
}
```

---

## 4. SSE (Server-Sent Events) 스트리밍 계약

- **엔드포인트**: `GET /api/v1/agent/runs/{run_id}/events` (또는 `POST /api/v1/agent/runs` 시 `stream: true`로 직접 SSE 응답 수신 가능)
- **미디어 타입**: `text/event-stream`

### 4.1 이벤트 구조
각 SSE 프레임은 아래와 같은 공통 스키마를 가집니다:
```text
event: <event_name>
data: {"run_id":"...","event_id":"...","timestamp":1794132000.123,"data":{...}}
```

### 4.2 이벤트 종류
1. `run.started`: 에이전트 실행 시작
2. `step.started`: 에이전트 루프의 단계(`step`) 시작 (step 번호 포함)
3. `model.started`: AI Gateway 모델 호출 시작
4. `tool.started`: 도구 실행 시작 (도구명, 인자 요약, 실행 ID)
5. `tool.completed`: 도구 실행 완료 (소요시간, 결과 크기, 성공 여부)
6. `step.completed`: 단계 완료
7. `answer.delta`: 최종 사용자 답변 텍스트 델타 청크
8. `run.completed`: 에이전트 전체 실행 완료
9. `run.failed`: 오류로 인한 실행 실패 (오류 코드, 안전한 메시지)
10. `run.cancelled`: 취소 완료

### 4.3 보안 및 프라이버시 주의사항
- 내부 Chain-of-Thought (생각 과정) 토큰은 외부 SSE로 절대 스트리밍하지 않음.
- API Key, 비밀 토큰, 문서 원본 전체, 개인정보는 SSE 이벤트 페이로드에 포함하지 않음.
