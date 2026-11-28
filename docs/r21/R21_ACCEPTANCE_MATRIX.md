# R21 수용 시나리오 A~I — 테스트 대응표

원칙: 모든 시나리오는 **실제 호환 포맷**으로 검증한다. PDF는 PyMuPDF, DOCX는 python-docx,
XLSX는 openpyxl, PPTX는 python-pptx로 생성한 파일을 사용하고, 모의 객체는 서비스 경계
(HTTP transport, Gateway 호출)에만 둔다.

| ID | 시나리오 | 기대 결과 | 테스트 |
| --- | --- | --- | --- |
| **A** | 워크스페이스 파일을 등록·색인한 뒤 질의하면, 결과가 문서·개정·근거와 함께 반환된다 | 워크스페이스 검색 결과에 `document`(namespace/revision/hash)와 `citation`이 붙고, 중복 excerpt는 `duplicates_removed`로 집계 | `AGPilot0005/tests/test_r21_knowledge_federation.py::test_search_merges_workspace_and_attachment_hits_and_deduplicates`, `::test_text_workspace_read_and_binary_read_inline` |
| **B** | 대화 첨부는 그 대화 범위에서만 검색되고, 다른 대화·워크스페이스와 섞이지 않는다 | 첨부 테넌트(`session-attachment-<hash>`) 범위 헤더로만 조회되고, 결과 namespace는 `session_attachment` | `::test_search_merges_workspace_and_attachment_hits_and_deduplicates` (attachments_only), AGTools `test_knowledge_api.py::test_upload_read_search_update_rollback_flow` |
| **C** | PDF를 페이지 단위로 읽고 인용하며, 큰 문서는 부분 읽기, 암호화·손상 파일은 계약 오류로 거부된다 | 페이지별 citation, `max_chars` 초과 시 `next_cursor`, `encrypted_document`/`corrupt_content` | `AGPilot0006/tests/test_knowledge_pdf.py` (7건) |
| **D** | DOCX를 구조 기반으로 읽고, 텍스트/문단/표/이미지를 수정해도 나머지 서식·머리말이 보존된다 | Heading/Run/Table/Image/Header 노드, 수정 후 머리말·스타일·표·이미지 유지 | `tests/test_knowledge_docx.py` (7건) |
| **E** | XLSX에서 수식과 캐시값을 구분해 읽고, 셀·범위·표·차트를 쓰며, 큰 시트는 윈도우로 읽는다 | formula/cached 분리, `formula_recalc_required`, `sheet:Data:row:N` 커서, 표·차트 생성 | `tests/test_knowledge_xlsx.py` (7건) |
| **F** | PPTX의 슬라이드/레이아웃/테마/노트를 읽고, 슬라이드 추가·삭제·순서변경 후에도 테마가 유지된다 | 레이아웃·테마 색상 동일, 슬라이드 수/순서 기대대로 | `tests/test_knowledge_pptx.py` (6건) |
| **G** | Vision은 Gateway 모델 능력이 있을 때만 켜지고, 없으면 OCR/텍스트 폴백 또는 명시적 미지원으로 끝난다(내용을 지어내지 않음) | capability 파일 없음 → `available=false`+사유, capability 있음 → 모델 id 보고, 분석 실패 시 OCR 폴백, 전부 불가 시 빈 텍스트+오류 | `tests/test_knowledge_vision.py` (6건), `AGPilot0010/tests/test_capabilities.py` (14건), `AGPilot0005/...::test_vision_delegates_to_engine_capability` |
| **H** | 아티팩트는 리비전으로 저장되고, 동시 수정은 충돌로 거부되며, 롤백·무결성 검사가 가능하다 | revision 1→2, stale hash/revision → 409 `revision_conflict`, rollback → 새 리비전, validate `ok` | `AGPilot0005/...::test_artifact_create_update_conflict_rollback_and_validate`, `AGPilot0006/tests/test_knowledge_service.py` (10건) |
| **I** | 두 서비스가 같은 계약을 말하고, 능력 매니페스트와 저하 표시가 일관된다 | 필드셋 골든 일치, 에러 코드 표 일치, manifest `formats/vision/limits`, 검색 degrade 명시 | `AGPilot0005/tests/test_r21_contract_conformance.py` (13건), `AGPilot0006/tests/test_knowledge_contracts.py` (8건), `AGPilot0011/hd2ai-admin/tests/knowledge-client.test.mjs` (6건) |

## 실행 방법

```bash
# AGTools
cd AGPilot0006 && python3 -m pytest tests/test_knowledge_*.py -q

# AGCore
cd AGPilot0005 && python3 -m pytest tests/test_r21_*.py -q

# Gateway
cd AGPilot0010 && python3 -m pytest tests/test_capabilities.py -q

# HD2AI
cd AGPilot0011/hd2ai-admin && node --test tests/knowledge-client.test.mjs
```

## 이번 선행개발에서 검증하지 않은 것

- 실제 모델에 이미지를 보내는 Gateway→Provider 호출 (capability 선택·바인딩까지만 검증)
- ARM64 Raspberry Pi / Windows 실기 실행 (wheel 존재 기준 예상)
- LibreOffice 렌더링 경로 (R21 지식 엔진은 사용하지 않음)
- HD2AI 화면 배선 (클라이언트 모듈까지만)
