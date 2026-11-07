# HD2AI Beta 연동 설계 (HD2AI_INTEGRATION)

## 1. 개요
기존 HD2AI(`services/lmpilot`)의 관리자 대화 인터페이스에 Beta Agent Mode를 최소 침습 방식으로 연동합니다.

## 2. 연동 원칙
1. **최소 수정 및 비파괴성**:
   - 공개 서비스(`/HD2AI/`)와 기존 로컬 모델 대화 경로는 전혀 건드리지 않습니다.
   - 오직 관리자 화면(`/admin/hd2ai/`)에만 Beta Agent Mode 토글을 제공합니다.
2. **격리 및 폴백**:
   - Agent Mode 기본값은 `OFF`입니다.
   - `OFF`일 경우 기존 `OpenAICompatibleProvider`를 그대로 사용합니다.
   - `ON`일 경우 신규 `AGCoreAgentProvider`를 사용합니다.
   - 에이전트 서비스 장애 시 기존 채팅으로 즉각 전환 가능하도록 분리합니다.
3. **BFF 프록시를 통한 보안 통신**:
   - 브라우저는 AGCore 내부 네트워크 주소나 토큰을 직접 알지 못합니다.
   - `/admin/hd2ai/api/v1/agent/*` 경로의 Nginx 또는 BFF 프록시를 통해 세션 쿠키 및 CSRF 토큰을 검증하고 내부 AGCore로 포워딩합니다.

## 3. 프론트엔드 연동 변경점 (`services/lmpilot/src`)

### 3.1 신규 컴포넌트 및 Provider
1. **`AgentProvider` (`services/lmpilot/src/agentProvider.ts`)**:
   - `ChatProvider` 인터페이스 호환 또는 확장.
   - AGCore SSE 엔드포인트 수신 (`run.started`, `tool.started`, `tool.completed`, `answer.delta` 등).
   - Tool Execution Trace 이벤트를 콜백으로 UI에 전달 (`onTraceUpdate`).
2. **Tool Execution Trace 패널**:
   - 어시스턴트 메시지 상단에 접이식(Accordion) 형태로 실행 중인 도구명, 진행 상태, 소요 시간 표시.
   - 완료 후에도 사용자가 클릭하여 실행된 도구 이력을 투명하게 확인 가능.
3. **Agent Mode 토글 버튼 (`App.tsx`)**:
   - 탑바(Topbar)에 `Agent Mode (Beta)` 토글 스위치 배치 (`isAdmin`인 경우에만 노출).

## 4. 백엔드 연동 (`services/lmpilot/backend/server.py`)
- 관리자 라우트 핸들러에 `/v1/agent/runs` 프록시 라우트 추가 또는 Nginx reverse proxy 규칙 설정.
- 기존 `/v1/chat/completions` 핸들러는 그대로 유지하여 회귀 방지.
