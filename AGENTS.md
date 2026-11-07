# AGPilot0005 작업 규칙 및 하네스 지침

## 1. 프로젝트 개요
- **프로젝트명**: HD2Lab Agent Core Platform (AGCore / AGPilot0005)
- **목적**: 범용 Agentic AI Runtime 개발 (FastAPI + LangGraph)
- **참조 저장소**: `Server-HQ` (`https://github.com/HyangDan2/Server-HQ`)
- **브랜딩/디자인 기준**: `AGPilot0002` (컬러 팔레트, 로고, 패비콘, 폰트 규격 준수)

## 2. 작업 원칙 및 경계
1. **단일 책임과 결합도**:
   - Single Responsibility, Loose Coupling, High Cohesion 원칙을 철저히 준수한다.
   - AGCore는 독립 백엔드 서비스이며 기존 HD2AI나 AI Gateway 내부로 병합하지 않는다.
   - Tool Gateway를 별도 서버로 분리하지 않고 AGCore 내부 모듈(Tool Registry & Dispatcher)로 구현한다.
2. **AI Gateway 연동 원칙**:
   - 기존 AI Gateway를 모델 설정과 인증 정보의 유일한 관리 주체로 유지한다.
   - AGCore는 AI Gateway를 우회하여 외부 모델 API(NVIDIA API 등)에 직접 연결하지 않는다.
   - Gateway 내부 Tool Calling 호환 계약을 점진적/비파괴적으로 설계한다.
3. **보안 및 환경 격리**:
   - 운영 서버를 개발/테스트 환경으로 직접 사용하지 않는다.
   - 기존 인증, Gateway, 사용자 데이터베이스를 임의로 초기화하거나 삭제하지 않는다.
   - API Key, 비밀 토큰, 사용자 세션 정보 및 개인정보를 Git, 일반 로그, 프론트엔드 Trace에 노출하지 않는다.
   - GitHub 푸시와 실제 서버 운영 배포를 엄격히 분리한다. 운영 배포는 사용자 명시적 승인 후 수행한다.
4. **브랜딩 및 UI 일관성**:
   - AGPilot0002의 다크 테마 기준 컬러(`--bg: #101110`, `--accent: #ff824f`, `--card-bg: #1b1d19`, `--olive-text: #b9c3a7`), 에셋(로고, 패비콘)을 유지/참고한다.
5. **실행 관리 및 오류 통제**:
   - Context Budget 관리(12,000 토큰 등) 및 호출 상한(max_tool_calls: 6, max_steps: 10) 준수.
   - 내부 Chain-of-Thought는 노출하지 않고 명확한 Tool Execution Trace 및 SSE 스트리밍 제공.
   - 사용자 취소 시 안전한 자원 해제.
