# AGCore 서버 운영 및 복원 가이드 (OPERATIONS)

## 1. 개요
본 문서는 AGCore의 배포, 환경 설정, 서비스 실행, 백업 및 복원 절차를 안내합니다.

## 2. 환경 설정
- **Python**: 3.10+ (권장 3.11 또는 3.12)
- **필수 환경 변수**:
  - `AGCORE_PORT`: 서버 리스닝 포트 (기본값: `8200`)
  - `AGCORE_HOST`: 바인딩 호스트 (기본값: `127.0.0.1`)
  - `AGCORE_GATEWAY_URL`: AI Gateway 엔드포인트 (`http://127.0.0.1:8080` 또는 UDS)
  - `AGCORE_GATEWAY_TOKEN`: AI Gateway Caller 인증 토큰
  - `AGCORE_MAX_STEPS`: 에이전트 최대 실행 단계 (기본값: 10)
  - `AGCORE_MAX_TOOL_CALLS`: 에이전트 최대 도구 호출 수 (기본값: 6)
  - `AGCORE_CONTEXT_BUDGET`: 컨텍스트 토큰 예산 (기본값: 12000)

## 3. 서비스 실행 및 프로세스 관리
```bash
# 의존성 설치
pip install -r requirements.txt

# 개발 서버 실행
uvicorn app.main:app --host 127.0.0.1 --port 8200 --reload

# 프로덕션 실행 (systemd 또는 Supervisor)
python3 -m app.main
```

## 4. 백업 및 롤백 절차
1. 배포 전 변경 사항을 Git 태그 및 별도 백업 디렉토리에 보관합니다.
2. 롤백 시:
   ```bash
   git checkout <PREVIOUS_STABLE_COMMIT>
   systemctl restart agcore
   ```
3. 기존 AI Gateway 및 HD2AI 서비스의 설정 파일은 불변(Immutable) 상태를 유지합니다.
