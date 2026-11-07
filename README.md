# AGPilot0005 — HD2Lab Agent Core Platform (AGCore)

HD2Lab의 다양한 AI 서비스에서 재사용할 수 있는 독립 범용 Agent Runtime 플랫폼입니다.
FastAPI와 LangGraph 기반으로 동작하며, 기존 AI Gateway와 결합하여 자율적인 Tool Calling 및 Context 관리를 수행합니다.

## 핵심 기능
- **LangGraph 기반 Agent Runtime**: 자율적 Tool 선택, 다단계 실행 루프, 동시 병렬 처리 지원
- **AI Gateway Adapter**: 기존 AI Gateway와의 Tool Calling 프로토콜 호환 계약
- **내장 Tool Registry & Dispatcher**: 시스템 시간, 에코, 안전한 수식 연산(No eval), 문서 Mock 등
- **Context Budget 관리**: 최대 호출 수, 토큰 예산 관리, 오래된 결과 압축
- **SSE & Execution Tracing**: 단계별 실행 이벤트 스트리밍, 취소 및 상세 트레이스 제공
- **HD2AI Beta 연동**: 관리자 화면 전용 Beta Agent Mode 지원

## 브랜딩 및 디자인
깃허브 `AGPilot0002`의 브랜딩(로고, 패비콘, 테마 컬러 팔레트)을 준수합니다.
