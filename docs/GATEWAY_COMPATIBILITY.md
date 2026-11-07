# AI Gateway Tool Calling 호환성 분석 및 어댑터 명세 (GATEWAY_COMPATIBILITY)

## 1. 개요
본 문서는 HD2Lab의 `services/ai-gateway`와 AGCore(`AGPilot0005`) 간의 Tool Calling 호환성을 검증하고, 기존 채팅 경로와 정책 스키마를 파괴하지 않는 비파괴적(Non-breaking) 내부 계약을 정의합니다.

## 2. 기존 AI Gateway 분석 및 확인된 제약 사항

### 2.1 기존 구현 구조
- **경로**: `services/ai-gateway/gateway.py`, `services/ai-gateway/engine.py`
- **라우트**: `hd2ai-admin`, `hd2ai-public`, `hd2lingo-admin`, `hd2lingo-public`, `universe`
- **인증**: `GATEWAY_CALLERS` JSON 매핑을 통한 Bearer 토큰 인증 (HMAC 검증)
- **엔진**: Ollama (로컬) 및 NVIDIA API (Nemotron 등)

### 2.2 핵심 제약 사항 (호환 불가 원인)
1. **요청 파라미터 필터링 엄격성 (`engine.py:47`)**:
   ```python
   if not isinstance(data, dict) or set(data) - {'model', 'messages', 'temperature', 'max_tokens', 'stream', 'response_format', 'chat_template_kwargs'}:
       raise EngineError('invalid_request', '요청 형식을 확인하세요.', 422)
   ```
   - OpenAI 표준 Tool Calling 필드인 `tools`, `tool_choice`가 포함되면 즉시 `422 EngineError`를 발생시킴.
2. **메시지 스키마 제한 (`engine.py:49`)**:
   ```python
   if any(not isinstance(m, dict) or set(m) != {'role', 'content'} or m['role'] not in ('system', 'user', 'assistant') or not isinstance(m['content'], str) for m in ms):
       raise EngineError('invalid_messages', '대화 형식을 확인하세요.', 422)
   ```
   - `tool` role 미지원 (role은 오직 `system`, `user`, `assistant`만 허용)
   - 메시지 내 `tool_calls` 필드 미지원 (`set(m) != {'role', 'content'}`로 인해 422 발생)
   - `tool_call_id` 필드 미지원
3. **NVIDIA 페이로드 전달 누락 (`engine.py:62`)**:
   - `tools`, `tool_choice` 파라미터가 NVIDIA 업스트림 요청 body에 포함되지 않음.
4. **스트리밍 파싱 및 빈 응답 처리 제약 (`engine.py:139, 155`)**:
   - 스트리밍 수신 시 `delta.content`만 추출하며, `delta.tool_calls` 청크를 무시함.
   - 단일 완료 응답(`stream=False`) 시 `text.strip()`이 비어 있으면 `empty_response (502)`를 발생시킴. 도구 호출만 반환하는 모델의 경우 `content`가 `null`이거나 빈 문자열이므로 정상 도구 호출임에도 502 에러 처리됨.

## 3. AGCore 호환성 해결 전략

### 전략 A: AI Gateway 비파괴 패치 (Server-HQ 내부 호환 패치)
기존 Gateway의 `/internal/{route}/chat/completions` 엔드포인트를 유지하면서, 요청에 `tools`가 포함된 경우 Tool Calling 모드로 안전하게 라우팅되도록 허용 필드와 메시지 검증을 점진적으로 확장합니다.

1. **허용 필드 확장**:
   - 요청 body에 `tools`, `tool_choice` 허용
2. **메시지 스키마 확장**:
   - role에 `tool` 추가
   - assistant 메시지에 `tool_calls` 허용 (리스트 형태: `id`, `type`, `function`: `{name, arguments}`)
   - tool 메시지에 `tool_call_id`, `name`, `content` 허용
3. **NVIDIA 업스트림 전달**:
   - `tools`, `tool_choice`가 존재하면 그대로 upstream에 전달
4. **스트리밍/완료 응답 처리**:
   - `delta.tool_calls` 청크 누적 및 emit
   - `finish_reason == 'tool_calls'` 지원
   - `content`가 비어 있어도 `tool_calls`가 존재하면 `empty_response` 예외를 건너뜀

### 전략 B: AGCore 내부 AI Gateway Client Adapter
AGCore의 `app/gateway/client.py`는 AI Gateway의 동작 규격을 완벽히 캡슐화하여:
1. AI Gateway 토큰 및 Unix Domain Socket / HTTP 연결 관리
2. Streaming 응답에서 분할 수신되는 `tool_calls` arguments(JSON 파편)의 안전한 재조립 (Stream Chunk Reassembler)
3. Gateway 버전 확인 및 정책 준수 (Persona, Budget)
4. Gateway 미지원 환경(또는 Mock 테스트 환경)에서의 호환 레이어 지원
