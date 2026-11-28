# R2 Session Attachment 계약

이 기능은 배포자가 `AGCORE_SESSION_ATTACHMENT_SERVICES`에 지정한 서비스에서 활성화한다. 최초 후속 배치에서는 Beta만 선택한다. 제품 UI와 브라우저 인증은 downstream이 구성한다. 파서·인덱싱은 설정된 AGTools API를 재사용한다.

| API | 요청과 동작 |
| --- | --- |
| `PUT /api/v1/conversations/{conversation_id}/workspace` | 기존 세션 바인딩. Workspace ID 또는 일반 Chat의 `null`을 영속화한다. 기존 값과 다른 변경은 `409`다. |
| `POST /api/v1/conversations/{conversation_id}/attachments` | 기존 바인딩이 있는 세션에 multipart `file`을 업로드한다. 20 MiB 이하. 운영 파서에서 확인한 형식은 PDF, DOCX, PPTX다. AGTools가 받은 뒤 `202`와 불투명한 `att-` 참조, 처리 상태, 파일명을 반환한다. |
| `GET /api/v1/conversations/{conversation_id}/attachments/{attachment_id}/{view}` | 지원 보기 `status`, `outline`, `text`. 다른 세션·사용자·테넌트의 참조는 `404`로 거부한다. 텍스트 미리보기에는 내부 저장 경로가 포함되지 않는다. |
| `POST /api/v1/agent/runs` | Workspace 바인딩과 현재 세션의 `att-` 참조를 같이 보낸다. Workspace 모드에서 기존 Workspace·Canvas 도구와 허용된 세션의 문서 도구를 같은 실행에서 사용한다. 일반 Chat은 기존 Durable 분석 경로로 첨부를 사용할 수 있다. |
| 기존 run / analysis 조회 및 제어 | 새 첨부가 포함된 작업에는 해당 작업의 `conversation_id`를 쿼리에 제공한다. 인증된 서비스·Tenant·Owner 검증도 적용한다. 일반 작업의 API 형상은 유지한다. |

AGCore는 인증으로 검증된 Tenant, Owner, 서비스, 영속 대화 ID로 AGTools 문서 범위를 계산한다. 이 범위는 업로드, 상태 확인, 읽기, 검색 및 작업 재개에서 동일하다. 브라우저에서 제공한 Tenant/Owner 헤더는 범위 계산에 사용하지 않는다. 이전 분석 근거의 재사용은 새 첨부가 관여하는 경우 같은 대화에 한정한다. Dispatcher는 요청에 포함된 문서 참조에만 문서 도구를 허용하고, Workspace 도구는 기존 바인딩을 사용한다.

관리 세션이 첨부 하나를 참조하는 경우, 문서 도구는 모델에게 해당 ID의 입력을 요구하지 않는다. 모델이 ID를 생략하면 런타임이 검증된 실행 참조로 채운 뒤 기존 정책 검증을 수행한다. 모델이 ID를 명시하면 기존 허용 목록을 그대로 검사한다. 여러 참조가 있으면 모델 도구 스키마의 후보에 승인된 참조만 포함한다. 실행 전 등록된 도구 스키마로 최상위 인자를 정규화한다. 스키마에 정의되지 않은 인자는 제거하며, 명시된 문서 ID와 등록된 인자 값은 기존 검증에 그대로 전달한다. 업로드와 AGTools 인증 범위는 동일하다.

기존 공용 Library 및 기본 AGTools 문서는 보존한다. 정확한 기존 대화 귀속을 확인할 수 없는 파일은 현재 대화에 자동 연결하지 않는다. 확인된 원본을 사용자가 명시적으로 새 대화에 업로드하면 새 세션 범위에 등록된다. 브라우저에서 첨부 연결을 제거해도 파서 저장소는 그대로 유지한다. 롤백 시에는 기존 Workspace·Chat·Canvas와 새 세션 첨부용 파서 상태를 함께 보존한다.
