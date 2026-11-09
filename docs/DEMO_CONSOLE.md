# AGPilot0005 — Standalone Agent Demo Console (v1.1)

## 1. 개요
AGPilot0005의 LangGraph Agent Runtime을 독립적으로 테스트 및 검증할 수 있는 단일 페이지 Web Demo Console입니다.

기존 AGCore LangGraph 백엔드를 100% 재사용하며, 사용자가 OpenAI 호환 모델(예: NVIDIA Nemotron)의 Base URL, Model명, API Key를 직접 입력하여 일반 Chat 및 자율 Tool Calling(Agent Mode)을 실시간으로 관찰할 수 있습니다.

---

## 2. 화면 및 시스템 구성

### 2.1 Model Connection Panel
- **Base URL**: OpenAI 호환 엔드포인트 입력 (예: `https://integrate.api.nvidia.com/v1`)
- **Model**: 모델명 자유 입력 (예: `nvidia/nemotron-3.5-super-vl-120b-a12b` 또는 `mock`)
- **API Key**: 비밀번호 마스킹(`type="password"`) 입력
- **Connection Mode**:
  - **Direct Test Mode**: 사용자가 입력한 Base URL / Model / API Key로 직접 연결
  - **Gateway Test Mode**: 기존 AI Gateway 서버 연결 (Gateway Tool Calling 미지원 시 명확한 비활성 안내 제공, 몰래 우회 금지)
- **[ Test Connection ] 버튼**: 1 토큰의 최소 핑으로 연결 및 응답 속도(ms) 측정

### 2.2 Execution Console
- **Mode 선택**:
  - **Chat Mode**: 일반 LLM 단일 호출 (도구 미사용)
  - **Agent Mode**: LangGraph StateGraph 실행 (`tool_choice="auto"`, 자율 도구 선택 및 다단계 루프)
- **추천 테스트 프롬프트 버튼**:
  - 기본 대화 (인사)
  - 시간 조회 (`system.get_datetime`)
  - 다중 도구 (`system.get_datetime` + `math.calculate`)
  - 문서 목차 (`document.get_outline`)
- **Run / Stop 버튼**: 실행 중단(`AbortController` / 비동기 취소) 지원

### 2.3 Agent Execution Viewer
- **실시간 실행 진행 표시**:
  - `✓ LLM Request`
  - `✓ Tool Selected: <도구명>`
  - `✓ Tool Executed: <도구명> (<ms>)`
  - `● Generating Final Answer`
- **[ View Details ] 상세 로그**:
  - Tool Arguments (JSON 접기/펼치기)
  - Tool Execution Result 및 소요 시간
  - LLM의 내부 Chain-of-Thought는 완전히 차단하고 실제 실행 이벤트만 투명하게 제공

### 2.4 Response Panel
- 최종 응답 Markdown 렌더링
- 코드 블록 서식 및 One-click 클립보드 복사 버튼
- 오류 발생 시 안전한 Callout 표시

---

## 3. 보안 정책 준수
1. **API Key 취급 규칙**:
   - 브라우저 React 메모리 상태(`useState`)에만 보관
   - `localStorage`, `sessionStorage`, `IndexedDB`, Git, 쿠키, 설정 파일 일체 미저장
   - SSE 이벤트 스트림 및 백엔드 로그에 API Key 포함 절대 금지
   - 페이지 새로고침 시 즉시 메모리 소멸
2. **SSRF 방지**:
   - 백엔드 `validate_base_url`을 통해 내부망 IP(`169.254.169.254`, `10.0.0.0/8` 등) 및 클라우드 메타데이터 엔드포인트 차단
3. **독립성 유지**:
   - 운영 `HD2AI` 프런트엔드(`services/lmpilot`)의 코드를 복사하거나 수정하지 않고 완전히 독립된 Vite 클라이언트로 구현

---

## 4. 로컬 실행 방법

### 백엔드 및 통합 데모 콘솔 실행
```bash
# 가상환경 활성화
source .venv/bin/activate

# FastAPI 서버 구동 (포트 8200)
uvicorn app.main:app --host 127.0.0.1 --port 8200
```
브라우저에서 `http://127.0.0.1:8200/demo/`로 접속하면 번들링된 Demo Console이 즉시 실행됩니다.

### 프런트엔드 독립 개발 서버 (Vite HMR)
```bash
cd demo
npm install
npm run dev
```
브라우저에서 `http://localhost:5173/` 접속 (백엔드 `8200` 포트로 API 자동 프록시).
