# HD2Lab Agent Core Platform (AGPilot0005)

> **독립 범용 Agentic AI Runtime 및 Tool Execution 플랫폼**  
> *Version 1.1 — Phase 1 ~ Phase 3 완료*  
> *Python FastAPI • LangGraph Runtime • AI Gateway Adapter • Tool Dispatcher • SSE Tracing • Document Intelligence(AGPilot0006) 연동 • NVIDIA NIM/OpenAI Wire Translation*

---

## 1. 개요 (Overview)

**HD2Lab Agent Core Platform (AGCore / AGPilot0005)**는 HD2Lab의 다양한 생성형 AI 서비스(HD2AI, Universe, 사내 AI 도구 등)에서 공통으로 재사용할 수 있는 **독립 범용 Agentic AI Runtime 및 Tool Dispatcher 백엔드 서비스**입니다.

LangGraph 기반의 자율 에이전트 루프를 내장하여 모델의 추론과 도구 호출(Tool Calling), 다단계 실행 계획 및 상태 관리를 효율적으로 수행합니다.

### 핵심 아키텍처 원칙
- **독립 런타임 원칙**: 특정 웹 UI나 단일 AI Gateway에 종속되지 않고, 표준 REST API와 SSE(Server-Sent Events)를 통해 완전히 분리되어 동작합니다.
- **도구 실행 및 컨텍스트 예산 제어 (Context Budget Control)**: 최대 스텝 수(`max_steps`), 최대 도구 호출 수(`max_tool_calls`), 요청 페이로드(최대 2MB)를 강제하여 무한 루프 및 과도한 토큰 소모를 원천 차단합니다.
- **마이크로서비스 분리**: 비정형 문서 파싱/계층화/검색은 **AGPilot0006 (Document Intelligence)**에 위임하며, 100% 비동기 REST API로만 연동합니다.
- **Wire 레벨 프로토콜 호환성**: OpenAI 및 NVIDIA NIM의 함수명 정규식 제약(`^[a-zA-Z0-9_-]+$`)을 투명하게 흡수하여 `system.echo` $\leftrightarrow$ `system_echo` 양방향 변환을 자동으로 처리합니다.

---

## 2. HD2Lab 브랜드 및 비주얼 규격

본 프로젝트는 [AGPilot0002](https://github.com/HyangDan2/AGPilot0002) 및 [HD2Lab HQ](https://hd2lab.duckdns.org)의 시각 디자인 및 브랜드 규격을 준수합니다.

- **Brand Colors**:
  - Dark BG: `#101110`, Light BG: `#f4f5f0`
  - Accent Primary: `#ff824f` (Hover: `#ff9b73`)
  - Muted: `#a2a59c`, Lines & Borders: `#30322d`, Card BG: `#1b1d19`
- **Brand Mark**: HD2Lab Geometric Skew Mark & Favicon 세트 내장 (`favicon.svg`, `favicon.ico`, `LOGO.png` 등)
- **Header Eyebrow**: `05 / SERVE · AGENT CORE PLATFORM`
- **관리자 Swagger UI**: HD2Lab Dark Palette 기반 커스텀 테마 적용 (`/docs`)

---

## 3. 핵심 규칙 및 문서

- [API 명세 및 계약서 (docs/API_CONTRACT.md)](docs/API_CONTRACT.md): 전체 REST 엔드포인트, 요청/응답 스키마, SSE 프로토콜 및 Wire 변환 규약
- [시스템 아키텍처 (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md): LangGraph 런타임 상태 머신, 도구 레지스트리 및 Gateway 통합 설계
- [AGPilot0006 연동 계약 (docs/TOOL_ADAPTER_CONTRACT_AGPILOT0006.md)](docs/TOOL_ADAPTER_CONTRACT_AGPILOT0006.md): Document Intelligence 서비스와의 REST 연동 스키마
- [데모 콘솔 가이드 (docs/DEMO_CONSOLE.md)](docs/DEMO_CONSOLE.md): React 기반 Standalone Demo UI (`/demo/`) 운영 및 기능 설명
- [OpenAPI 스펙 JSON (docs/openapi.json)](docs/openapi.json): Swagger 및 자동화 클라이언트 연동용 전체 OpenAPI 3.1 스펙

---

## 4. 인증 및 접근 통제 (Authentication & Access Control)

운영 환경(Nginx 역방향 프록시)과 독립 API 호출 환경을 모두 지원하는 2단계 인증 정책을 적용합니다.

- **관리자 SSO 프록시 인증 (운영 환경 `/admin/agcore/`)**:
  - Nginx SSO 인증 레이어(`admin-sso-page.conf` / `admin-sso-api.conf`) 경유
  - 상태 변경 요청(`POST`, `DELETE` 등) 시 `X-CSRF-Token` 헤더 필수 검증 (CSRF 방어)
  - 관리자 식별 헤더 주입: `X-HD2Lab-User`, `X-HD2Lab-Role`, `X-HD2Lab-Service`
- **서비스 토큰 인증 (Direct API)**:
  - `Authorization: Bearer <AGCORE_SERVICE_TOKEN>`
- **입력 검증 및 리소스 제한**:
  - 요청 본문 최대 크기 2MB 초과 시 `413 Payload Too Large` 반환
  - 로컬/사설 IP 직접 연결 차단 (SSRF 방어)

---

## 5. API 엔드포인트 명세 (API Contract)

| 메서드 | 엔드포인트 | 태그 | 설명 |
| :--- | :--- | :--- | :--- |
| `GET`  | `/api/v1/health` | Health | 서비스 헬스 체크 및 AI Gateway / AGTools 연결 상태 확인 |
| `POST` | `/api/v1/agent/runs` | Agent | 에이전트 실행 생성 (비동기 202 대기열 또는 `stream: true` SSE 스트리밍) |
| `GET`  | `/api/v1/agent/runs/{run_id}` | Agent | 실행 상태, 최종 답변, 스텝별 실행 트레이스 및 리소스 사용량 조회 |
| `GET`  | `/api/v1/agent/runs/{run_id}/events` | Agent | 에이전트 실행 이벤트 전용 SSE 실시간 스트리밍 |
| `POST` | `/api/v1/agent/runs/{run_id}/cancel` | Agent | 진행 중인 에이전트 실행 즉시 중단 |
| `GET`  | `/api/v1/agents` | Agent | 사용 가능한 에이전트 프로필 및 허용된 도구 목록 조회 |
| `POST` | `/api/v1/demo/connect` | Demo Console | 직접 LLM(NVIDIA NIM/OpenAI) 또는 AI Gateway 연결성 사전 검증 |
| `POST` | `/api/v1/demo/run` | Demo Console | 데모 콘솔 전용 다중 모드(Chat/Agent) 실시간 스트리밍 실행 |
| `GET`  | `/api/v1/demo/tools` | Demo Console | 현재 런타임에 등록된 전체 도구 스키마(OpenAPI Spec) 조회 |

> 엔드포인트별 상세 Request/Response JSON 페이로드 및 SSE 10종 이벤트 명세는 **[API_CONTRACT.md](docs/API_CONTRACT.md)**를 참조하십시오.

---

## 6. 실행 및 테스트

### 1. 로컬 개발 서버 실행
```bash
# 가상환경 활성화 및 의존성 설치
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 서버 실행 (기본 포트 8200)
uvicorn app.main:app --host 127.0.0.1 --port 8200
```
- **Web Demo Console**: [http://127.0.0.1:8200/demo/](http://127.0.0.1:8200/demo/)
- **Swagger UI (OpenAPI Docs)**: [http://127.0.0.1:8200/docs](http://127.0.0.1:8200/docs)
- **OpenAPI Schema**: [http://127.0.0.1:8200/openapi.json](http://127.0.0.1:8200/openapi.json)

### 2. 자동화 테스트 실행 (총 30개 단위/통합 테스트)
```bash
pytest tests/ -v
```

### 3. 운영 환경 (라즈베리파이) 서비스 관리
```bash
# 서비스 상태 확인
sudo systemctl status hd2lab-agcore.service

# 서비스 재시작
sudo systemctl restart hd2lab-agcore.service
```
