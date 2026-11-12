# HD2Lab Agent Core Platform (AGCore / AGPilot0005) — API Contract

> **버전**: v1.1.0  
> **기준 일자**: 2026-11-13  
> **프로토콜**: HTTP/1.1 REST & Server-Sent Events (SSE)  
> **접근 기본 경로**: `/api/v1` (Nginx 관리자 프록시: `/admin/agcore/api/v1`)  
> **연동 서비스**: HD2AI Frontend, AI Gateway v3, Document Intelligence (AGPilot0006)

---

## 1. 개요 및 아키텍처 철학 (Overview & Principles)

**HD2Lab Agent Core Platform (AGCore)**는 HD2Lab 생태계 내 다양한 프런트엔드(HD2AI, 관리자 도구 등)에서 활용할 수 있는 **독립 범용 Agentic AI Runtime 및 Tool Execution 서비스**입니다.

### 핵심 설계 원칙
1. **단일 책임 원칙 (Single Responsibility Principle)**:
   - AGCore는 자율적 다단계 에이전트 추론 루프(LangGraph 기반), 도구 실행 제어(Tool Dispatcher), 그리고 실행 추적(Execution Tracing)을 전담합니다.
   - 비정형 문서의 심층 파싱, 계층 구조화, 시각 에셋 렌더링은 **AGPilot0006 (Document Intelligence Service)**에 전적으로 위임하며, 100% REST API로만 통신합니다.
2. **프로토콜 격리 및 안전한 모델 인터페이스**:
   - 내부 AI Gateway 및 외부 LLM Provider(NVIDIA NIM, OpenAI 등)와의 통신은 엄격한 프로토콜 어댑터를 거치며, 모델 공급자별 Wire 스키마 차이(예: 함수명 점 표기 제한)를 투명하게 흡수합니다.
3. **토큰 예산 및 리소스 한도 보호 (Budget & Limits)**:
   - 최대 스텝 수(`max_steps`), 최대 도구 호출 수(`max_tool_calls`), 요청 페이로드 크기(최대 2MB)를 런타임 단계에서 엄격히 제한하여 무한 루프나 DoS를 원천 차단합니다.

---

## 2. 인증 및 보안 계약 (Authentication & Security Contract)

AGCore는 운영 환경(라즈베리파이 Nginx SSO 프록시)과 로컬 독립 개발 환경을 모두 지원하는 다계층 보안 정책을 적용합니다.

### 2.1 관리자 SSO 프록시 및 CSRF 방어
운영 환경(`/admin/agcore/`)에서 Nginx 인증 레이어(`admin-sso-page.conf` / `admin-sso-api.conf`)가 선행 적용됩니다.

- **식별 헤더 주입**:
  - `X-HD2Lab-User`: 인증된 관리자 사용자 식별자
  - `X-HD2Lab-Role`: 사용자 권한 (`admin`, `operator`)
  - `X-HD2Lab-Service`: 서비스 라우트 태그 (예: `hd2ai-admin`)
- **CSRF 토큰 강제 (`X-CSRF-Token`)**:
  - 브라우저 기반 관리자 환경에서 상태를 변경하는 모든 비-GET 요청(`POST`, `DELETE` 등)은 Nginx 세션 검증 쿠키 및 `X-CSRF-Token` 헤더를 포함해야 합니다.
  - 토큰 누락 또는 세션 미인증 시 Nginx가 `403 Forbidden` 또는 `401 Unauthorized`를 즉시 반환합니다.

### 2.2 서비스 토큰 인증 (Direct API)
마이크로서비스 간 직접 통신 또는 백엔드 간 호출 시 Bearer 토큰을 사용합니다.
- `Authorization: Bearer <AGCORE_SERVICE_TOKEN>`

### 2.3 SSRF 방어 및 리소스 제한
- **Direct LLM 엔드포인트 검증**: `DirectLLMClient`는 로컬 루프백(`127.0.0.1`, `localhost`) 및 AWS 메타데이터 서비스(`169.254.169.254`), 비공개 사설 IP 대역 연결을 차단하여 내부 인프라 탐색(SSRF)을 방지합니다.
- **Payload 크기 제한**: 미들웨어를 통해 2MB 이상의 요청 본문은 즉시 `413 Payload Too Large`로 거절됩니다.

---

## 3. API 엔드포인트 명세 (API Contract Matrix)

모든 엔드포인트는 기본 `/api/v1` 하위에 위치합니다. (Nginx 프록시 시 `/admin/agcore/api/v1/...`)

| 메서드 | 엔드포인트 | 태그 | 설명 |
| :--- | :--- | :--- | :--- |
| `GET`  | `/api/v1/health` | Health | 서비스 상태 및 Gateway 소켓 연결 여부 확인 |
| `POST` | `/api/v1/agent/runs` | Agent | 에이전트 실행 생성 (비동기 202 또는 즉시 SSE 스트리밍) |
| `GET`  | `/api/v1/agent/runs/{run_id}` | Agent | 실행 상태, 최종 답변, 스텝별 상세 실행 트레이스 조회 |
| `GET`  | `/api/v1/agent/runs/{run_id}/events` | Agent | 에이전트 실행 이벤트 실시간 SSE 스트리밍 |
| `POST` | `/api/v1/agent/runs/{run_id}/cancel` | Agent | 진행 중인 에이전트 실행 즉시 취소 |
| `GET`  | `/api/v1/agents` | Agent | 사용 가능한 에이전트 프로필 및 허용된 도구 목록 조회 |
| `POST` | `/api/v1/demo/connect` | Demo Console | 모델 공급자(Direct / Gateway) 연결 및 상태 사전 검증 |
| `POST` | `/api/v1/demo/run` | Demo Console | 독립 데모 콘솔용 실시간 실행 스트림 (Chat / Agent 모드) |
| `GET`  | `/api/v1/demo/tools` | Demo Console | 런타임에 등록된 전체 도구의 OpenAPI JSON Schema 조회 |

---

## 4. 엔드포인트별 상세 규격

### 4.1 Agent 실행 요청
- **엔드포인트**: `POST /api/v1/agent/runs`
- **Content-Type**: `application/json`

#### Request Body
```json
{
  "agent_id": "general-agent",
  "message": "오늘 서울 날짜와 시간을 확인하고, 125 * 8의 계산 결과와 사내 문서 hd2lab-guide 목차를 알려줘.",
  "session_id": "sess-20261113-001",
  "context_refs": [],
  "stream": true,
  "options": {
    "max_steps": 10,
    "max_tool_calls": 6,
    "temperature": 0.2
  }
}
```

#### Response (비스트림 모드: `stream: false`, Status: `202 Accepted`)
```json
{
  "run_id": "run-019318b2-385a-7b3c-8a21-9988aabbccdd",
  "agent_id": "general-agent",
  "session_id": "sess-20261113-001",
  "status": "queued",
  "created_at": 1794500000.123
}
```
> `stream: true` 지정 시 즉시 `text/event-stream` 형태의 SSE 연결이 반환됩니다.

---

### 4.2 Agent 실행 상태 및 트레이스 조회
- **엔드포인트**: `GET /api/v1/agent/runs/{run_id}`
- **Response (Status: `200 OK`)**:
```json
{
  "run_id": "run-019318b2-385a-7b3c-8a21-9988aabbccdd",
  "agent_id": "general-agent",
  "session_id": "sess-20261113-001",
  "status": "completed",
  "answer": "오늘 날짜는 2026년 11월 13일이며, 125 * 8의 계산 결과는 1,000입니다. 사내 문서 목차는 다음과 같습니다: 1. Overview & Vision, 2. AI Gateway Architecture...",
  "traces": [
    {
      "step": 1,
      "type": "tool",
      "tool_name": "system.get_datetime",
      "status": "completed",
      "duration_ms": 8.5,
      "result_size_bytes": 54
    },
    {
      "step": 2,
      "type": "tool",
      "tool_name": "math.calculate",
      "status": "completed",
      "duration_ms": 3.2,
      "result_size_bytes": 12
    },
    {
      "step": 3,
      "type": "tool",
      "tool_name": "document.get_outline",
      "status": "completed",
      "duration_ms": 15.4,
      "result_size_bytes": 420
    }
  ],
  "usage": {
    "steps": 3,
    "tool_calls": 3,
    "total_duration_ms": 1240.5
  },
  "created_at": 1794500000.123,
  "finished_at": 1794500001.364
}
```

---

### 4.3 Agent 실행 취소
- **엔드포인트**: `POST /api/v1/agent/runs/{run_id}/cancel`
- **Response (Status: `200 OK`)**:
```json
{
  "run_id": "run-019318b2-385a-7b3c-8a21-9988aabbccdd",
  "status": "cancelled",
  "message": "Agent execution cancelled by user."
}
```

---

### 4.4 등록된 도구 및 에이전트 목록 조회
- **엔드포인트**: `GET /api/v1/agents`
- **Response (Status: `200 OK`)**:
```json
{
  "agents": [
    {
      "id": "general-agent",
      "name": "General Assistant Agent",
      "description": "시스템 제어, 계산, 문서 검색 도구를 활용할 수 있는 HD2Lab 범용 에이전트",
      "allowed_tools": [
        "system.echo",
        "system.get_datetime",
        "math.calculate",
        "document.get_outline",
        "document.read_section",
        "document.search",
        "document.read_chunk",
        "document.get_figure_info"
      ]
    }
  ]
}
```

---

### 4.5 Demo Console 연결 테스트
- **엔드포인트**: `POST /api/v1/demo/connect`
- **Request Body**:
```json
{
  "mode": "direct",
  "base_url": "https://integrate.api.nvidia.com/v1",
  "model": "meta/llama-3.1-70b-instruct",
  "api_key": "nvapi-secret-key"
}
```
- **Response (Status: `200 OK`)**:
```json
{
  "success": true,
  "ready": true,
  "mode": "direct",
  "model": "meta/llama-3.1-70b-instruct",
  "latency_ms": 182.4,
  "message": "NVIDIA NIM 공급자 연결 성공"
}
```

---

### 4.6 Demo Console 실시간 실행 스트림
- **엔드포인트**: `POST /api/v1/demo/run`
- **Content-Type**: `application/json` (반환: `text/event-stream`)
- **Request Body**:
```json
{
  "mode": "agent",
  "model_mode": "direct",
  "base_url": "https://integrate.api.nvidia.com/v1",
  "model": "meta/llama-3.1-70b-instruct",
  "api_key": "nvapi-secret-key",
  "message": "내일 날짜를 계산해줘",
  "stream": true,
  "options": {
    "max_steps": 10,
    "max_tool_calls": 6,
    "temperature": 0.2
  }
}
```

---

### 4.7 헬스 체크
- **엔드포인트**: `GET /api/v1/health`
- **Response (Status: `200 OK`)**:
```json
{
  "status": "healthy",
  "service": "AGCore",
  "version": "1.1.0",
  "gateway_connected": true,
  "agtools_url": "http://127.0.0.1:8006/api/v1"
}
```

---

## 5. SSE (Server-Sent Events) 스트리밍 프로토콜 상세

- **Content-Type**: `text/event-stream; charset=utf-8`
- **헤더**: `Cache-Control: no-store`, `X-Accel-Buffering: no`

### 5.1 프레임 구조
```text
event: <이벤트명>
data: {"run_id":"...","event_id":"...","timestamp":1794500000.123,"data":{...}}

```

### 5.2 이벤트 종류 및 페이로드 규격

| 이벤트명 | 발생 시점 | 주요 데이터 필드 |
| :--- | :--- | :--- |
| `run.started` | 에이전트 실행 시작 | `agent_id`, `session_id`, `created_at` |
| `step.started` | 에이전트 루프의 신규 Step 시작 | `step` (현재 스텝 번호: 1, 2, ...) |
| `model.started`| LLM 추론 호출 시작 | `step`, `provider`, `model` |
| `tool.started` | 특정 Tool 실행 시작 | `step`, `tool_name`, `arguments`, `call_id` |
| `tool.completed` | Tool 실행 완료 및 결과 수신 | `step`, `tool_name`, `duration_ms`, `result_summary`, `error` |
| `step.completed` | 해당 Step 완료 | `step`, `status` |
| `answer.delta` | 최종 사용자 답변 스트리밍 청크 | `delta` (생성된 텍스트 청크) |
| `run.completed` | 전체 실행 정상 완료 | `answer`, `usage` (총 소요시간, 스텝 수, 도구 호출 수) |
| `run.failed` | 실행 실패 | `error_code`, `message` (안전하게 정제된 오류 메시지) |
| `run.cancelled`| 사용자 요청으로 실행 취소 | `cancelled_at`, `step` |

> **보안 보장**: 모델의 내부 `thinking`(Chain-of-Thought) 토큰, API Key, 원본 대용량 문서 바이너리는 SSE 이벤트에 노출되지 않습니다.

---

## 6. Wire 레벨 프로토콜 호환성 계약 (LLM Provider Tool Name)

OpenAI, NVIDIA NIM 등 주요 LLM Provider는 Tool Calling 시 함수 이름에 점(`.`)을 허용하지 않고 `^[a-zA-Z0-9_-]+$` 정규식을 엄격히 강제합니다.

AGCore는 클라이언트와 도구 레지스트리의 직관적인 네임스페이스(`system.echo`, `document.search`)를 보존하면서 Provider Wire 통신 시 아래 규칙에 따라 **양방향 투명 변환**을 수행합니다:

```text
[AGCore 내부 도구 레지스트리]                [LLM Wire 인터페이스 (OpenAI/NIM)]
   system.echo             <===========>            system_echo
   system.get_datetime     <===========>            system_get_datetime
   math.calculate          <===========>            math_calculate
   document.get_outline    <===========>            document_get_outline
   document.read_section   <===========>            document_read_section
   document.search         <===========>            document_search
   document.read_chunk     <===========>            document_read_chunk
   document.get_figure_info<===========>            document_get_figure_info
```

- **Outbound (LLM 요청 시)**: Wire 함수 목록 전송 시 `name.replace('.', '_')` 적용
- **Inbound (LLM 응답 처리 시)**: LLM이 전달한 `tool_call.function.name`의 prefix(`system_`, `math_`, `document_`)를 검출하여 정규 점(`.`) 표기법으로 복원 후 내부 Dispatcher로 전달

---

## 7. 다운스트림 AGPilot0006 (Document Intelligence) 연동 계약

AGCore는 사내 문서 열람 및 질의를 위해 **AGPilot0006 (AGTools, 기본 포트: 8006)**을 100% REST API로 온디맨드 질의합니다:

| AGCore Tool | AGPilot0006 REST API | 비고 |
| :--- | :--- | :--- |
| `document.get_outline` | `GET /api/v1/documents/{doc_id}/outline` | 문서 전체 목차 트리 |
| `document.read_section` | `GET /api/v1/documents/{doc_id}/sections/{section_id}` | 특정 절 본문 및 그림 ID 목록 |
| `document.search` | `POST /api/v1/documents/{doc_id}/search` | FTS 키워드 기반 관련 단락 검색 |
| `document.read_chunk` | `GET /api/v1/documents/{doc_id}/chunks/{chunk_id}` | 최대 1,500토큰 단위 원본 청크 |
| `document.get_figure_info`| `GET /api/v1/documents/{doc_id}/figures/{figure_id}` | 그림 메타데이터 및 이미지 다운로드 URL |

- **요청 헤더**: `Authorization: Bearer <AGTOOLS_TOKEN>`
- **Fail-Closed 정책**: AGTools 서비스 다운 또는 연결 실패 시, `ALLOW_MOCK_FALLBACK=False` 환경에서는 임의 데이터를 날조하지 않고 `connection_failed` 오류를 반환하여 데이터 정확성을 보장합니다.

---

## 8. HTTP 상태 코드 및 오류 규격

| HTTP 상태 코드 | 상황 | 오류 응답 포맷 |
| :--- | :--- | :--- |
| `200 OK` | 성공적 조회 또는 처리 완료 | JSON 객체 또는 이벤트 스트림 |
| `202 Accepted` | 비동기 실행 세션 생성 완료 | `{"run_id": "...", "status": "queued"}` |
| `400 Bad Request` | 필수 파라미터 누락, 유효하지 않은 도구 호출 인자 | `{"detail": "..."}` |
| `401 Unauthorized` | 유효하지 않거나 누락된 서비스 토큰 | `{"detail": "Unauthorized"}` |
| `403 Forbidden` | CSRF 토큰 누락 또는 테넌트 권한 부족 | `{"detail": "Forbidden / Missing CSRF"}` |
| `404 Not Found` | 존재하지 않는 `run_id`, `doc_id`, `section_id` | `{"detail": "Not found"}` |
| `413 Payload Too Large`| 2MB를 초과하는 요청 본문 전송 | `{"detail": "Request payload too large (max 2MB)."}` |
| `502 Bad Gateway` | 다운스트림 모델 게이트웨이 또는 AGTools 통신 장애 | `{"detail": "Gateway communication failure"}` |
