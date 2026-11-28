# R3 Workflow Execution Contract — WO-R3-PREDEV

> **대상 저장소**: AGPilot0005 (AGCore) · 브랜치 `feat/r3-workflow-extensibility`
> **선행 커밋**: `552df4b` (WP-03~WP-08, WP-10 엔진) · **본 작업**: WP-01/02/09 + Runtime/API/Acceptance/Docs
> **버전**: R3-PREDEV v1.0 (2026-11-28)
> **범위**: Planner, Execution Plan, Skill, Rules, Hook, MCP, Unified Tool Registry, Checkpoint/Resume,
> Trajectory, REST/SSE API, Acceptance A~H, R2.1 및 Codex 통합 요구사항.
> **Lean Engineering v1.1 준수**: `app/runtime/langgraph_r1.py`, `app/workspace/*`, `app/canvas/*`는 무수정.
> `app/schemas/events.py`와 `app/main.py`에는 additive 확장만 넣었다.

---

## 0. 구성 파일

| 파일 | 역할 | 성격 |
|---|---|---|
| `app/workflow/safety.py` | WP-10 실행 스코프, 승인 토큰, 프롬프트 인젝션 방어, 멱등 원장 | 선행(552df4b) |
| `app/workflow/registry.py` | WP-07 통합 도구 카탈로그 | 선행 + 정책 1건 확장 |
| `app/workflow/rules.py` | WP-04 4계층 규칙 해석 | 선행 |
| `app/workflow/skills.py` | WP-03 SKILL.md 발견·지연 로딩·capability 위임 | 선행 |
| `app/workflow/hooks.py` | WP-05 11개 훅 이벤트, fail-open/closed | 선행 |
| `app/workflow/mcp.py` | WP-06 MCP 수명주기·정규화·자격증명 격리 | 선행 |
| `app/workflow/budget.py` | WP-08 컨텍스트 배분·압축·실행 한도 | 선행 |
| `app/workflow/office_adapter.py` | R2.1/AGTools 5-capability 어댑터(Mock/Runtime) | 선행 |
| **`app/workflow/planner.py`** | **WP-01 라우팅·플랜 DAG·조건부 Replanning** | 신규 |
| **`app/workflow/checkpoint.py`** | **WP-02 체크포인트 저장/재개 + WP-10 재검증** | 신규 |
| **`app/workflow/trajectory.py`** | **WP-09 구조화 Trajectory, 시크릿 마스킹** | 신규 |
| **`app/workflow/runtime.py`** | **WP-02 LangGraph 실행 그래프, 승인 게이트, SSE** | 신규 |
| **`app/api/workflow.py`** | **`/agent/workflows/*` REST + SSE** | 신규 |
| **`app/schemas/events.py`** | `EventType`에 `workflow.*` 9종 additive 추가 | 10줄 추가 |
| **`app/main.py`** | `workflow_router` 등록 | 2줄 추가 |
| **`tests/test_r3_workflow_acceptance.py`** | **Acceptance A~H + API 스모크 (39 tests)** | 신규 |

---

## 1. Workflow Execution Contract (실행 계약)

### 1.1 두 개의 라우트 — 섞이지 않는다

`Planner.plan()`은 요청을 둘 중 하나로만 보낸다.

| route | 조건 | 보장 |
|---|---|---|
| `direct_chat` | `execution_mode="direct"` / capability 없음(auto) / 작업 신호 없음(auto) | **추가 모델 호출 0건, Skill 로딩 0건, Workflow 그래프 실행 0건** (구조적 보장 — planner가 해당 컴포넌트를 호출하기 전에 반환) |
| `workflow` | `execution_mode="workflow"` 또는(auto) capability + 작업 신호 존재 | `ExecutionPlan` DAG 실행, 단계별 체크포인트·Trajectory·이벤트 |

작업 신호(`Planner._looks_like_work`): `context.force_workflow=true`, `context.requires_tools=true`,
`context.params` 존재, 또는 질의에 `작성/만들/생성/분석/비교/정리/보고서/슬라이드/workflow/report/anal/compar/generate/build/draft/summar` 중 하나 포함.

> 해석 주의: `auto` 모드에서 `available_capabilities`만으로는 workflow로 가지 않는다. 잡담("ㅇㅇ")에
> 5개 capability가 붙어 있어도 `direct_chat`이다 (Acceptance H, `test_auto_mode_ignores_capabilities_without_a_work_signal`).

### 1.2 실행 단계 (LangGraph 노드)

```
START ─(entry)─> context_resolve ─> rules_resolve ─> capability_discover ─> plan ─> execute_dag
                        │(실패)          │(실패)                              │(실패)
                        └──────────────> report <─────────────────────────────┘
execute_dag ─(completed)─> validate_hooks ─> store_artifacts ─> report ─> END
            └(그 외)──────────────────────────────────────────> report
```

| 노드 | 책임 | 실패 시 |
|---|---|---|
| `context_resolve` | `workflow.started` 발행, `session_start` 훅, 컨텍스트 토큰 계측, 체크포인트 생성 | 예외는 실행기 최상위에서 `failed` |
| `rules_resolve` | `RulesEngine` 구성·해석, 적용 규칙 Trajectory 기록 | `rule_resolution_failed` → `report` |
| `capability_discover` | 도구/office capability 열거, Skill **요약만** 발견(본문 로딩 없음) | - |
| `plan` | `before/after_planning` 훅, Planner 호출, Skill 권한 검증·로딩, `workflow.plan.created` | `planner` 단계 실패 → `report` |
| `execute_dag` | DAG 스케줄러(직렬/병렬 fan-out·fan-in/조건부 skip/재시도/타임아웃/취소/승인/Replan) | `failed`/`awaiting_approval`/`cancelled` |
| `validate_hooks` | `workflow_completed` 훅 (fail-closed 시 런 실패) | `failed` |
| `store_artifacts` | 산출물(파일/pptx 등) Trajectory·결과에 수집 | - |
| `report` | `session_end` 훅, Trajectory 종료, 최종 체크포인트, 종료 이벤트 | - |

그래프는 프로세스당 1회 컴파일(`get_compiled_workflow_graph()`), 실행별 상태는
`config["configurable"]["run_ctx"]`로 주입한다 (R1.3 `langgraph_r1.py` 관례 동일).

### 1.3 상태 전이

| 상태 | 의미 | 다음 가능 상태 |
|---|---|---|
| `pending` | 체크포인트 생성 직후 | `running` |
| `running` | 실행 중 (단계 완료마다 재저장) | `awaiting_approval` · `completed` · `failed` · `cancelled` |
| `awaiting_approval` | 휴먼 승인 대기 (`pending_approval_id` 존재) | `running`(승인 후 resume) · `cancelled` |
| `completed` | 모든 단계 완료(또는 skip) | 종료 |
| `failed` | 복구 불가 실패 (원인 보존) | 종료 (`resume`은 재시도 용도로만 허용) |
| `cancelled` | 호출자 취소 | 종료 (`resume` 거부: `workflow_run_cancelled`) |

REST 결과에는 `direct_chat`(워크플로 미실행)이 추가로 나타나며, 이 경우 체크포인트 상태는 `completed`로 남는다.

### 1.4 보장 / 비보장

**보장**
1. 완료 단계는 재개 시 **재실행되지 않는다** (체크포인트 `completed_steps` + 멱등 원장 2중).
2. 승인 없이는 `approval_required` 단계가 실행되지 않으며, **모델이 만든 승인 토큰은 구조적으로 거부**된다.
3. `before_file_write` fail-closed 훅이 실패하면 **원본 파일은 변경되지 않는다** (어댑터 호출 자체가 없다).
4. 단계 실행은 `timeout_seconds`로 상한이 있고, 재시도는 `max_retries`로 상한이 있다.
5. 다른 tenant/owner/service/session/workspace의 런은 읽기·재개가 불가하다.
6. 실행 예산(`max_steps`, `max_tool_calls`, 컨텍스트 토큰)을 넘으면 실행이 중단되고 코드가 남는다.
7. 실패 시 **실제 원인 코드/메시지**가 `error`와 `trajectory.failures`에 남는다.

**비보장 (명시적 한계)**
- `AGPolicy` RL 학습은 미구현. `WorkflowTrajectory.learning_payload()`로 특징만 제공한다.
- 취소는 **협조적**이다: 실행 중인 단일 도구 호출은 끝까지 진행되고, 다음 단계 경계에서 취소가 반영된다.
- 진행 중인 런에 대한 `cancel`과 런 자신의 최종 저장이 경합하면 마지막 저장이 이긴다(운영 시 cancel은 승인 대기/유휴 런에 사용 권장).
- LLM 기반 자동 플래닝은 미구현(모델 훅만 존재). 현재 플랜은 결정적 레시피/명시 단계에서 만든다.
- 실시간 메트릭(Prometheus 등) 미연동. 관측은 Trajectory + SSE 이벤트까지다.

---

## 2. Planner Schema (WP-01)

### 2.1 `PlannerRequest` (입력)

| 필드 | 타입 | 기본 | 설명 |
|---|---|---|---|
| `query` | str | `""` | 사용자 요청 |
| `context` | dict | `{}` | `params`, `rules`, `workspace_root`, `workspace_id`, `session_id`, `inputs` 등 |
| `budget` | dict | `{}` | `max_steps`, `max_retries`, `max_estimated_tokens`, `replan_on_failure` |
| `execution_mode` | `auto`\|`direct`\|`workflow` | `auto` | 라우팅 강제 |
| `available_capabilities` | tuple[str] | `()` | 호출자가 위임받은 capability 목록 |

### 2.2 `PlannerDecision` (출력)

```json
{ "route": "workflow", "reason": "workflow_planned",
  "plan": { "plan_id": "plan_3f1c...", "version": 1, "goal": "...", "step_count": 5,
            "steps": ["s1_research", "..."], "capabilities": ["workspace.rag.search", "..."],
            "parallel_groups": {"analysis": ["s2_table", "s3_diff"]},
            "max_steps": 10, "max_retries": 2, "estimated_tokens": 812,
            "replan_on_failure": true } }
```

`reason` 코드: `explicit_direct_mode`, `fast_path_no_capability_required`, `fast_path_simple_query`,
`workflow_planned`, `workflow_capability_signal`, `explicit_plan`.

### 2.3 `PlanStep`

| 필드 | 타입 | 기본 | 비고 |
|---|---|---|---|
| `step_id` | str | 필수 | `^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$`, 플랜 내 유일 |
| `name` | str | `""` | 표시용 |
| `capability` | str | 필수 | 실행 권한 키. **호출자 grant에 없으면 실행 거부** |
| `tool_name` | str? | null | Registry 정규화 이름(없으면 capability로 조회) |
| `skill_name` | str? | null | 지정 시 실행 전 권한 위임 검증 후 본문 로딩 |
| `depends_on` | tuple[str] | `()` | 선행 step id |
| `parallel_group` | str? | null | 같은 그룹은 동시 실행(fan-out), 그룹 내 상호 의존 금지 |
| `input_bindings` | dict | `{}` | §3 바인딩 언어 |
| `output_key` | str? | `step_id` | `$outputs.<key>`로 참조되는 저장 키 |
| `approval_required` | bool | false | true면 승인 게이트에서 정지 |
| `max_retries` | int | 0 | 0~10, 총 시도 = max_retries+1 |
| `timeout_seconds` | float | 30 | 0< t ≤600 |
| `condition` | str? | null | **additive 확장**: 조건부 분기/스킵 (§2.6) |

### 2.4 `ExecutionPlan`

`plan_id`(`^plan_[A-Za-z0-9_\-]{4,80}$`), `goal`, `steps`, `max_steps`, `max_retries`,
`estimated_tokens`, `replan_on_failure`, `version`, `parent_plan_id`, `notes`.

**생성 시 검증**(`validate_plan_dag`) — 위반은 `PlannerError.code`로 노출:

| 코드 | 의미 |
|---|---|
| `max_steps_exceeded` | 단계 수 > 예산 |
| `duplicate_step_id` | step_id 중복 |
| `dangling_dependency` | 존재하지 않는 선행 참조 |
| `cyclic_dependency` | DAG 순환 |
| `parallel_group_has_dependency` | 병렬 그룹 내부 의존 |
| `token_budget_exceeded` | 추정 토큰 > 예산 |
| `no_available_capability` | workflow 모드인데 capability 0개 |

### 2.5 결정적 레시피

1. **문서 파이프라인**: capability 집합이 `DOCUMENT_PIPELINE` 5종을 모두 포함하면 고정 레시피.
   `s1_research` → (`s2_table` ∥ `s3_diff`, `parallel_group="analysis"`) → `s4_deck` → `s5_report`.
   `s5_report`는 `$template`으로 research/table/diff/deck 산출을 보고서 본문으로 합성한다.
2. **단일 체인**: 그 외에는 `CAPABILITY_ORDER` 순서대로 1 capability = 1 step, 순차 체인.
   `workspace.*` capability는 `workspace_id` 기본 바인딩이 자동 주입된다.
3. **명시 단계**: `WorkflowRunRequest.plan_steps`가 있으면 그대로 사용(예산·DAG 검증은 동일).

### 2.6 조건부 분기 · Replanning

- `condition`: `"$outputs.diff.added_count > 0"` 형태의 비교식 또는 단일 참조(truthy). 평가 불가 시
  **스텝을 실행하지 않고 skip**(`condition_false`, `trajectory.metadata.condition_errors` 기록). 코드 실행 없음.
- 재시도: `attempts = max_retries + 1`. `NON_RETRYABLE_CODES`(예: `file_not_found`, `path_escape`,
  `invalid_parameters`, `permission_denied`, `scope_mismatch`, `prompt_injection_detected`)는 즉시 실패 처리.
- Replanning: `plan.replan_on_failure` + `replans < max_replans`(기본 1) + 원인이 `adapter`/`runtime`일 때만.
  - `REPLAN_FALLBACKS`(예: `office.pptx.generate` → `workspace.file.write`)가 있으면 **실패 스텝만 대체**하고
    후속 스텝의 `depends_on`을 대체 스텝으로 재지정, `output_key` 유지.
  - 폴백이 없으면 실패 스텝 **및 그 출력에 의존하는 모든 스텝을 제거**. 남는 스텝이 없으면
    `nothing_left_to_replan` → 런은 실패로 종료하고 **원래 오류 코드를 그대로 보고**한다.
  - 결과: `plan.version` 증가, `parent_plan_id` 연결, `trajectory.replans` 기록, `workflow.plan.created(replan=true)` 발행.

---

## 3. Binding 언어 (`input_bindings`)

| 표기 | 의미 |
|---|---|
| `$request.query` | 요청 질의 |
| `$context.inputs.table_ref` | `request.context` 경로 |
| `$scope.workspace_id` | 실행 스코프(tenant/owner/service/session/conversation/workspace) |
| `$outputs.research.hit_count` | 앞 단계 산출(`output_key`) |
| `$steps.s1_research.hit_count` | step_id 기준 산출 |
| `$rules.report.output_path` | 해석된 유효 규칙(플랫 키 우선, 없으면 중첩 탐색) |
| `{"$ref": "$outputs.research.hits"}` | 객체/리스트 그대로 참조 |
| `{"$template": "...{x}...", "vars": {"x": "$outputs.diff.added_count"}}` | 문자열 포맷 합성 |

리스트 인덱스는 `hits[0]` 형태만 허용한다. 표현식/함수 호출/eval은 **지원하지 않는다**(플랜을 저장·재생·감사 가능하게 유지).
미해결 참조는 `binding_unresolved`로 실패하며, 재시도하지 않는다.

---

## 4. Skill Manifest Schema (WP-03)

```yaml
---
name: report-generation          # ^[a-z0-9][a-z0-9-_]{1,63}$
version: 1.0.0                   # x.y[.z[.w]]
description: Assemble a sourced report from workspace research.
required_capabilities: [workspace.rag.search, workspace.file.write]
references: [report-outline.md]
scope: global                    # global | product | workspace | session
tags: [report, generation]
---
# 본문(instructions) …
```

| 계약 | 내용 |
|---|---|
| 발견(`discover_skills`) | `SkillSummary`만 반환(이름/버전/설명/필요 capability/참조 개수). **본문·참조 미로딩** |
| 로딩(`load_skill`) | 본문 + 참조 materialise, 캐시. 실행 단계에서만 호출 |
| 권한 위임(`authorize_execution`) | `required_capabilities ⊆ caller_capabilities`일 때만 `SkillExecutionGrant` 발급. 부족 시 `SkillPrivilegeError` |
| 실행 강제(`enforce_tool_call`) | grant에 없는 capability 호출은 `capability_not_in_skill_grant` |
| 실패 매핑 | 플랜 단계의 skill 위임 실패는 런 실패 `skill_privilege_denied` (플랜 자체가 잘못됨) |
| 내장 스킬 | `document-summary`, `document-comparison`, `spreadsheet-analysis`, `presentation-generation`, `report-generation`, `workspace-research` |

> **중요**: 스킬은 호출자 권한을 **넓힐 수 없다**. 문서 파이프라인은 각 스텝에 내장 스킬을 지정하므로,
> capability를 위임하지 않은 호출자는 그 스텝에서 실패한다(Acceptance C-2).

---

## 5. Rules Priority Contract (WP-04)

### 5.1 4계층과 권위

`RuleLayer`: `system` > `product` > `workspace` > `session` (권위 순, `LAYER_AUTHORITY`).
동일 키 충돌 시:

1. 상위 권위 레이어가 이긴다(`decision="applied"`, `reason="highest_authority"`).
2. **보호 범주**(`auth`, `security`, `approval`, `data_isolation`)는 하위 레이어가 **완화할 수 없다**.
   완화 시도는 `RuleConflict(blocked=True, reason="protected_rule_weakening_blocked")`로 기록되고 무시된다.
   더 엄격한 값(`is_stricter`: max/limit/budget 계열은 작은 값, min/required 계열은 큰 값)만 `override`로 승격된다.
3. 보호 범주가 아니면 더 구체적인 레이어가 상위 기본값을 정교화(`specific_layer_override`).
4. 값이 동일하면 `unchanged`, 열등하면 `shadowed`.

### 5.2 적용 경로

`RulesEngine.resolve()` → `effective` dict. 런타임은 `$rules.<key>` 바인딩으로 **실행 파라미터에 직접 반영**한다
(예: `report.output_path` 규칙 → `workspace.file.write`의 `path`). 적용 내역은
`trajectory.applied_rules[{key, rule_id, layer, decision, reason}]`에 남고, 차단된 충돌은
`trajectory.metadata.rule_conflicts_blocked`에 남는다.

### 5.3 워크스페이스 규칙 파일

`<root>/.agcore/rules/*.{md,json}`, `<root>/.agcore/rules.{md,json}`, `<root>/rules.{md,json}`.
md 형식은 `- key: value`, `# [priority=10]` 메타를 지원한다. 런타임은 `request.context.workspace_root`
(없으면 `WorkflowRuntime.workspace_root`)를 사용한다.

---

## 6. Hook Event Contract (WP-05)

| 이벤트 | 시점 | 차단 가능 | 기본 정책(등록 필요) |
|---|---|---|---|
| `session_start` | `context_resolve` 시작 | 예(관측용 fail-open 권장) | fail_closed |
| `before_planning` | Planner 호출 전 | 예 | fail_closed |
| `after_planning` | 플랜 확정 후 | 예 | fail_closed |
| `before_tool_call` | 모든 capability 호출 직전 | **예** | fail_closed |
| `after_tool_call` | 호출 성공 직후 | 예(부작용 후이므로 출력은 보존하고 런 실패) | fail_closed |
| `before_file_write` | `workspace.file.write` 직전 | **예 — 실패 시 원본 보호** | fail_closed |
| `after_file_write` | 파일 쓰기 성공 직후 | 예 | fail_closed |
| `on_approval_required` | 승인 게이트 진입 | 아니오(관측) | fail_open |
| `on_error` | 단계/런 실패 | 아니오(관측) | fail_open |
| `workflow_completed` | 모든 단계 완료 후 | **예** (검증 실패 → 런 실패) | fail_closed |
| `session_end` | `report` 단계 | 아니오(관측) | fail_open |

### 6.1 디스패치 규칙

- 런타임은 **플랫폼 주체**이므로 `granted_permission="admin"`으로 디스패치한다(훅은 운영자가 등록한 코드).
- 훅 핸들러는 `dict` payload를 받고, `{"blocked": true}`로 이벤트를 중단하거나 dict 패치를 반환할 수 있다.
- 실패 정책: `FAIL_CLOSED`(기본)는 `HookValidationError`를 올려 **가드 대상 동작을 중단**하고,
  `FAIL_OPEN`은 기록만 남기고 통과한다.
- 셸/커맨드 훅은 등록 자체가 거부된다(`allow_shell_forbidden`, `command_hook_forbidden`).
- 재귀 상한 `max_depth`(기본 4), 이벤트 중복 제거 `event_id` 창(기본 4096).
- fail-closed 실패도 `trajectory.hook_executions`에 `status="failed"`, `failure_policy="fail_closed"`로 남는다
  (dispatch가 raise하는 경로도 레지스트리 히스토리에서 복사).

### 6.2 원본 파일 보호 시퀀스 (Acceptance D)

```
before_tool_call ─> before_file_write ─(fail_closed 실패)─> [어댑터 미호출] ─> on_error ─> failed
```
어댑터 `workspace_file_write`는 호출되지 않으므로 파일 내용/`side_effects` 모두 불변이다.

---

## 7. MCP Integration Contract (WP-06)

### 7.1 등록

`MCPServerConfig(server_id, transport, command_or_url, allowed_tools, timeout_seconds, max_retries, credential_ref, description)`

- `server_id`: `^[a-z0-9][a-z0-9_\-]{0,47}$`, 프로세스 내 유일(중복 시 `MCPRegistrationError`).
- `transport`: `stdio` | `streamable_http`. `streamable_http`는 `http(s)://`만, `stdio`는 URL 금지.
- **자격증명은 참조만**: `credential_ref`가 `CredentialStore`에 없으면 `MCPConfigError("unknown_credential_ref")`.
  `Bearer …`, `sk-…`, `ghp_…` 같은 인라인 시크릿은 `inline_credential_rejected`로 거부.
- `allowed_tools`는 **allowlist**다. 목록에 없는 도구는 발견 단계에서 `not_in_allowlist`로 차단.

### 7.2 발견 → 등록

`discover_tools(server_id)`는 ① 이름 형식 검증 ② allowlist ③ **도구 설명 인젝션 스캔**을 통과한 도구만
`UnifiedToolSpec(namespace="mcp.<server>", capability="mcp.<server>.<tool>", source="mcp")`로 등록한다.
`destructiveHint=true` → `side_effect="destructive"` + `approval_required=true`(런타임 승인 게이트 강제).
`readOnlyHint=true` → `side_effect="read"`, `permission="read"`.

거부 사유 코드: `invalid_tool_name`, `not_in_allowlist`, `suspicious_tool_description`.

### 7.3 호출

- 런타임은 `capability = "mcp.<server>.<tool>"` 스텝을 만나면 미등록 서버에 대해 1회 `connect` + `discover_tools`를 수행한다.
- `allowed_tools`/`registry`/승인 3중 검사를 통과해야 호출된다(`mcp_tool_not_allowlisted`,
  `mcp_tool_not_discovered`, `mcp_user_approval_required`).
- 결과 텍스트는 `PromptInjectionGuard`로 감싸 `<<<UNTRUSTED_DATA …>>>` 블록으로 전달하고,
  Trajectory에는 `mcp_executions[{server_id, tool, status, suspicious_output}]`로 남긴다.
- 자격증명 값은 Trajectory/텔레메트리 어디에도 남지 않는다(`CredentialStore.redact` + Trajectory 마스킹).

---

## 8. Unified Tool Registry 명세 (WP-07)

`UnifiedToolSpec`: `name`, `namespace`, `version`, `description`, `input_schema`, `output_schema`,
`capability`, `permission(read|write|admin)`, `side_effect(none|read|write|external|destructive)`,
`timeout_seconds`, `error_contract`, `approval_required`, `source(builtin|agtools|mcp|skill|downstream)`, `tags`.

| 규칙 | 내용 |
|---|---|
| 정규화 이름 | `namespace.name` (충돌 시 `ToolCollisionError`) |
| capability 소유권 | capability당 구현 1개. 다른 source가 같은 capability를 등록하려면 `supersede=True` 필요(`DuplicateCapabilityError`) |
| 권한 바닥 | `execute(granted_permission=...)`가 spec.permission보다 낮으면 `PermissionError` |
| **정책 (본 작업에서 1건 확장)** | `write`/`destructive` + `permission="read"`는 **금지 유지**. `external` + `read`는 **허용**(읽기 전용 외부 호출: 웹 검색 등). `destructive`는 `approval_required=True` 필수 |
| `is_read_only` | `side_effect ∈ {none, read}` **그리고** `permission="read"`. `external`은 경계를 넘으므로 read-only 아님 |
| 지연 스키마 노출 | `select()/schemas_for()`로 선택 도구만 프롬프트에 노출 |

> **확장 근거**: AGPilot0006 `app/r3_capabilities.py`가 `agtools.web.search`(읽기 전용 웹 검색)를
> `permission="read"` + `side_effect="external"`로 표현해야 했다. 기존 검증은 이 조합을 거부했으므로
> `registry.py`의 `_consistent_policy`만 완화했고, `tests/test_r3_engines.py`의
> `test_write_tool_cannot_declare_read_permission`(write+read 거부)은 그대로 통과한다.
> 대안(권장하지 않음): 웹 검색을 `permission="write"`로 표기 — read 워크플로가 외부 호출 권한을 갖게 되어 의미가 뒤집힌다.

---

## 9. Checkpoint / Resume 계약 (WP-02 + WP-10)

### 9.1 저장 스키마 (`WorkflowCheckpoint`)

| 필드 | 설명 |
|---|---|
| `run_id` | `wfrun_<20hex>` 또는 호출자 지정(중복 불가) |
| `scope` | `tenant_id, owner_id, service_id, session_id, conversation_id, workspace_id` |
| `plan` | `ExecutionPlan.model_dump()` |
| `completed_steps` | 완료 step_id 목록(중복 없음) |
| `step_outputs` | `output_key → 산출` |
| `pending_approval_id` | 승인 대기 시 `apreq_<run_id>_<step_id>` |
| `status` | §1.3 |
| `updated_at` / `created_at` | UTC |
| *(additive)* `plan_version`, `replans`, `failures`, `metadata.request` | 재개·감사용 |

### 9.2 백엔드

| backend | 위치 | 용도 |
|---|---|---|
| `sqlite`(기본) | `AGCORE_R3_CHECKPOINT_DB` 또는 `data/workflow_checkpoints.db` (WAL) | 운영/서비스 |
| `file` | 디렉터리 내 `<run_id>.json` | 파일 기반 배포 |
| `memory` | 프로세스 내부 | 테스트/단명 런 |

Trajectory 스냅샷은 체크포인트 행에 함께 저장된다(가시성 캐시). 재개 시 이 스냅샷에서 복원해
`trajectory.step_records`가 이어지고, `s1_search`처럼 한 번 완료된 단계는 **기록도 1건**으로 유지된다.

### 9.3 접근 재검증 (WP-10)

```
API:  caller(credential) ──authorize_run──▶ checkpoint(소유 tenant/owner/service 대조)  [불일치 → 404]
      │
      └─ run_context(): tenant/owner/service = credential, session/conversation/workspace = 저장값
                        └─ store.load(run_id, context=ctx) ─▶ assert_scope_access  [불일치 → ScopeViolation → 404]
runtime: resume(run_id, ctx) ─▶ store.load(run_id, context=ctx) ─▶ assert_scope_access (동일)
```

- `assert_scope_access(context, checkpoint.scope, require=("tenant_id","owner_id"))`는 저장된 스코프의 **모든 차원**을
  대조한다(deny by default). 즉 같은 tenant라도 owner/session/workspace가 다르면 거부된다.
- 존재 여부를 숨기기 위해 권한 오류는 **404 `workflow_run_not_found`**로 응답한다(존재 추측 차단).
- `is_admin` + `can_access_tenant`인 호출자는 저장 tenant로 컨텍스트를 재구성해 열람할 수 있다(감사 목적, 문서화된 예외).

### 9.4 재개 규칙

| 상황 | 동작 |
|---|---|
| `awaiting_approval` + 승인 토큰 없음 | `ApprovalError("approval_required")` → 403 |
| `awaiting_approval` + 모델 발급 토큰 | `approval_source != "user"` → `model_generated_approval` → 403 |
| `awaiting_approval` + 사용자 토큰 | 서명·스코프·만료 검증 → `approval_id == pending_approval_id` 확인 → 해당 단계만 승인 표시 후 재개 |
| `cancelled` | `workflow_run_cancelled` → 409 |
| `completed` | 저장된 결과 반환(`reason="already_completed"`), 실행 없음 |
| `failed`/`running` | 남은 단계만 재개(완료 단계 skip) |

### 9.5 멱등성

`IdempotencyLedger` 키 = `(scope_hash, step_id, f"{run_id}:{step_id}")`.
- 스텝 완료 시 `begin → complete(결과)`.
- 재개/재시도 시 `lookup().status == "completed"`이면 **외부 부작용을 다시 일으키지 않고** 캐시 결과를 사용한다.
- 따라서 체크포인트(`completed_steps`)와 원장이 2중 방어선을 이룬다(Acceptance B: `side_effects` 길이 1).
- 같은 `run_id` 재사용은 거부(`run_id_conflict`)되므로 원장 키 충돌로 다른 런이 오염되지 않는다.

### 9.6 취소

`cancel(run_id, ctx)` → `cancelled` 체크포인트 + `pending_approval_id` 해제 + `workflow.cancelled` 발행.
진행 중 런은 단계 경계에서 `asyncio.Event`를 확인해 스스로 중단한다(협조적 취소, §1.4).

---

## 10. Trajectory Schema (WP-09)

| 필드 | 타입 | 내용 |
|---|---|---|
| `run_id` | str | 실행 식별자 |
| `plan_summary` | dict | `plan_id, version, parent_plan_id, goal, step_count, steps, capabilities, parallel_groups, max_steps, max_retries, estimated_tokens, replan_on_failure` |
| `step_records` | list[StepRecord] | `step_id, name, capability, status(completed/failed/skipped/replayed/pending), attempt, started_at, ended_at, duration_ms, output_key, error_code, error, replanned_from` |
| `tool_calls` | list[ToolCallRecord] | `step_id, capability, transport(office/registry/mcp), tool_name, attempt, arguments(마스킹), result_summary(요약), status(ok/error/timeout/denied), duration_ms, error_code, error` |
| `skill_selections` | list | `skill, version, reason, loaded, allowed_capabilities` |
| `applied_rules` | list[dict] | `key, rule_id, layer, decision, reason` |
| `hook_executions` | list | `event, hook_id, status, failure_policy, duration_ms, error, reason` |
| `mcp_executions` | list | `server_id, tool, status, duration_ms, error, suspicious_output` |
| `retries` | list | `step_id, capability, attempt, error_code, error, timeout` |
| `replans` | list | `from_plan_id, to_plan_id, version, failed_step_id, error_code, drop_completed, note` |
| `failures` | list | `step_id, capability, code, message, attempts, replanned, source(adapter/hook/planner/scope/approval/runtime/budget), cause_type` |
| `token_usage` | TokenUsage | `prompt/completion/total/model_calls/context_tokens` |
| `execution_latency_ms` | float | 벽시계 실행 시간 |
| `artifacts` | list | `artifact_id, kind(file/pptx/…), path, sha256, bytes, produced_by_step` |
| `status` | str | 최종 상태 |
| `created_at`, `completed_at`, `metadata` | | 부가 정보(route, reason, pending_approval, condition_errors, replan_error, skill_candidates, rule_conflicts_blocked, status_reason) |

### 10.1 직렬화 3형태

| 메서드 | 용도 | 보안 |
|---|---|---|
| `to_dict()` / `to_json()` | 내부 저장·API 응답 | 기록 시점 마스킹: `password/passwd/secret/token/api_key/apikey/authorization/auth_header/credential/private_key/bearer/cookie/session_key/access_key` 키와 `app/tracing/sanitizer.SECRET_VALUE_PATTERNS` 값 → `[redacted]`. 문자열 2 000자, 리스트 5개 상한 |
| `to_telemetry()` | 외부 텔레메트리 반출 | `sanitize_mapping()`(R1-FIX-13 allowlist)로 전면 재살균 |
| `learning_payload()` | 향후 `AGPolicy` 학습 특징 | 자유 텍스트 없이 구조화 특징만(plan/step_outcomes/skills/rules/hook_failures/retry·replan 수/failure_codes/artifacts/token/latency) |

> **RL 학습 자체는 미구현**이다(브리프 명시). `learning_payload()`는 스키마만 제공한다.

---

## 11. REST / SSE API

Base: `/api/v1/agent/workflows` (Nginx 관리자 프록시: `/admin/agcore/api/v1/agent/workflows`).
모든 엔드포인트는 `require_authenticated_caller`를 통과해야 하며 **본문으로 신원을 받지 않는다**.

| 메서드 | 경로 | 설명 | 주요 응답 |
|---|---|---|---|
| POST | `/plan` | 라우팅 결정 + 플랜(실행 없음) | 200 `{route, reason, plan, fast_path}` · 422 `PlannerError` 코드 |
| POST | `/runs` | 런 시작. `stream=true` → SSE | 200 완료/실패, 202 `awaiting_approval` |
| GET | `/runs/{run_id}` | 체크포인트 + Trajectory + 스텝 상태 | 200 · 404 |
| POST | `/runs/{run_id}/resume` | `{approval: <ApprovalToken>}`로 재개 | 200 · 403 · 404 · 409 |
| POST | `/runs/{run_id}/approve` | 사용자 승인 토큰 발급 | 200 `{approval, next}` · 409 |
| POST | `/runs/{run_id}/cancel` | 취소 | 200 `{checkpoint}` · 409 |
| GET | `/tools` | Registry/office/MCP 현황 | 200 |
| GET | `/skills` | Skill 요약(본문 미포함) + 로딩 수 | 200 |
| POST | `/mcp/servers` | MCP 서버 등록(+연결/발견) | 201 · 202(연결 실패) · 400 · 409 · 503 |

### 11.1 오류 봉투

```json
{"detail": {"code": "workflow_run_not_found", "message": "...", "retryable": false, "context": {}}}
```

| HTTP | 코드 |
|---|---|
| 400 | `invalid_scope`, `invalid_run_id`, `invalid_rules`, `invalid_rule_layer`, `mcp_config_invalid` |
| 403 | `approval_required`, `model_generated_approval`, `approval_mismatch`, `invalid_signature`, `expired_approval`, `scope_mismatch` (승인 스코프) |
| 404 | `workflow_run_not_found` (존재/권한 모두 동일 응답) |
| 409 | `run_id_conflict`, `workflow_run_cancelled`, `workflow_run_completed`, `workflow_run_terminal`, `approval_not_required`, `mcp_server_exists` |
| 422 | `max_steps_exceeded`, `token_budget_exceeded`, `cyclic_dependency`, `dangling_dependency`, `duplicate_step_id`, `parallel_group_has_dependency`, `no_available_capability` |
| 503 | `mcp_unavailable` |

### 11.2 `workflow.*` SSE 9종과 payload

`SSEEvent.to_sse()` 형식(`event: <type>` + `data: {run_id, event_id, timestamp, status, ...}`).
`app/schemas/events.py`의 `EventType`에 additive로 등록되어 스키마 검증을 통과한다.

| 이벤트 | data 필드 |
|---|---|
| `workflow.started` | `query, execution_mode, entry` |
| `workflow.plan.created` | `plan{plan_id,version,parent_plan_id,goal,step_count,steps,capabilities,parallel_groups,max_steps,max_retries,estimated_tokens,replan_on_failure}, replan(bool)` |
| `workflow.step.started` | `step_id, capability, name` |
| `workflow.step.completed` | `step_id, capability, output_key, duration_ms, attempt` |
| `workflow.approval.required` | `step_id, capability, approval_id, reason` |
| `workflow.checkpoint.saved` | `checkpoint_status, step_id, completed_steps(개수)` |
| `workflow.completed` | `steps(개수), artifacts(개수), duration_ms` |
| `workflow.failed` | `error{code, message, step_id?, capability?, attempts?, source?}` |
| `workflow.cancelled` | `reason` |

### 11.3 승인 게이트 흐름 (HTTP)

```
1) POST /runs            → 202 {status:"awaiting_approval", pending_approval:{approval_id, step_id, capability}}
2) POST /runs/{id}/approve {"approved_by":"user-1"}
   → 200 {approval:{approval_id, scope_hash, approved_by_user:true, approval_source:"user", signature, expires_at}, next:"…"}
3) POST /runs/{id}/resume {"approval": <위 토큰>}
   → 200 {status:"completed"}
```

`approve`는 **사용자 행위**를 대표하는 엔드포인트이며, `ApprovalAuthority.issue_user_approval`로만 토큰을 만든다.
모델이 만든 토큰(`approved_by_user=false`, `approval_source="model"`)은 서명 검증 이전에 구조적으로 거부된다.

---

## 12. 필수 테스트 Fixture 및 실행 결과

### 12.1 Fixture (`tests/test_r3_workflow_acceptance.py`)

| Fixture/더블 | 목적 |
|---|---|
| `MockOfficeAdapter(documents=...)` + `DOCUMENT_FILES` | 결정적 RAG/xlsx/docx/pptx/write (테이블 JSON, 정책 md, base/target md) |
| `SlowOfficeAdapter` | xlsx/docx 각 0.2s 지연 → 병렬 fan-out 증명 |
| `FlakyOfficeAdapter` | 1회 `transient_backend` 실패 후 성공 → 재시도 |
| `BrokenDeckAdapter` | `template_not_found` → Replanning(폴백) |
| `HangingAdapter` | 1s 지연 + `timeout_seconds=0.2` → 타임아웃 |
| `FakeMCPClient` | MCP 발견/호출/파괴적 도구/인젝션 설명 도구 |
| `RecordingHooks` | 11개 훅 디스패치 순서·횟수 기록 |
| `ctx(**overrides)` | 6차원 `ExecutionContext` |

### 12.2 시나리오 매트릭스

| ID | 시나리오 | 핵심 검증 (테스트 수) |
|---|---|---|
| **A** | 다단계 문서 작성 E2E | 5 capability 순서, 병렬 그룹 실행, 보고서 본문 합성, 산출물 2종, Trajectory 5 tool_calls, SSE 순서 (3) |
| **B** | 중단 Checkpoint 재개 | 완료 단계/부작용 **0회 중복**, 모델 토큰 거부, SQLite 재오픈, 멱등 원장, 스텝 1회 기록 (4) |
| **C** | Workspace Skill + Rules 보고서 | `.agcore/rules/report.md` → `reports/q4-review.md` 경로 반영, 스킬 로딩·권한 위임, 권한 확장 시도 실패 (2) |
| **D** | 파일 쓰기 전후 Validation Hook | fail_closed 시 원본 불변·어댑터 미호출, fail_open 통과, 11개 훅 디스패치 (3) |
| **E** | MCP 등록→발견→통합 실행 | allowlist 차단, `mcp_executions`, 파괴적 도구 승인 대기, 자격증명 참조 검증, 설명 인젝션 격리 (4) |
| **F** | 재시도/Replanning/실패 원인 | 1회 재시도 성공, 폴백 재계획, 복구 불가 시 `file_not_found` 원인 보고, 타임아웃 상한 (4) |
| **G** | 격리 | 타 tenant/owner 읽기·재개 차단, 타 workspace 스텝 차단, 미위임 capability 거부, 시크릿 마스킹, run_id 재사용 거부 (6) |
| **H** | 일반 Chat Fast-path | 모델/Skill/그래프 **0건**, `direct` 강제, `auto` 신호 없음, 런 실행 시 도구 0건 (4) |
| **API** | REST + SSE 스모크 | `/plan`, `/runs`(+stream), `/runs/{id}`, approve→resume, cancel, `/tools`, `/skills`, `/mcp/servers`, 9종 이벤트 스키마 (10) |

### 12.3 실행 결과 (2026-11-28, `/workspace/.venv-r2canvas/bin/python -m pytest`)

| 명령 | 결과 |
|---|---|
| `pytest tests/test_r3_engines.py -q` | **116 passed** |
| `pytest tests/test_r3_workflow_acceptance.py -q` | **40 passed** |
| `pytest tests/test_r3_engines.py tests/test_r3_workflow_acceptance.py -v` | **156 passed** |
| `pytest tests -q` (전체) | **580 passed, 1 failed** |
| R2 Canvas 주요 스위트 7종 | **110 passed** |

- 전체 1건 실패는 **선행 커밋 `552df4b`에서 이미 실패하던**
  `tests/test_r1_2_final_addendum.py::test_add_02_durable_analysis_real_orchestrator_create_resume_and_no_legacy_fallback`
  (`app/runtime/tasks/bootstrap.py:41` `_DummyNoop` has no attribute `create`)이다.
  `git archive HEAD`로 만든 클린 트리에서도 동일하게 실패함을 확인해 **본 작업과 무관**함을 검증했다.

---

## 13. R2.1 통합 요구사항

### 13.1 어댑터 교체

| 항목 | Mock (기본) | R2.1 Runtime |
|---|---|---|
| 선택 | `AGCORE_R3_OFFICE_ADAPTER=mock`(기본) | `AGCORE_R3_OFFICE_ADAPTER=runtime` + `AGCORE_AGTOOLS_BASE_URL` |
| 클래스 | `MockOfficeAdapter` | `RuntimeOfficeAdapter` |
| 운영 환경 | `assert_adapter_allowed`가 **production에서 Mock 금지** | 허용 |
| 실패 시 | - | 어댑터 미구성 시 `None` → capability 스텝이 `unknown_capability`로 명확히 실패(가짜 데이터 금지) |

`WorkflowService`는 `AGCORE_R3_OFFICE_ADAPTER`로 어댑터를 만들고, `r3` 서비스는
`build_workflow_stack(...)`의 registry/skills/hooks/mcp/approvals/ledger를 그대로 재사용한다.

### 13.2 capability ↔ AGTools 라우트 매핑 (AGPilot0006 `98113bc`, `app/r3_capabilities.py`)

- 19개 실제 라우트 매핑이 `AGPilot0006`에 확정되어 있다. 대표: `workspace.rag.search` → `agtools.document.search`.
- `office.*` 4종(`office.xlsx.analyze`, `office.docx.compare`, `office.pptx.generate`, `workspace.file.write`)은
  **Mock 담당**으로 정렬되어 있다(R2.1 실 어댑터 연결 전까지는 Mock으로 계약 검증).
- 본 런타임은 `OfficeCapabilityAdapter.invoke(capability, params)` 한 지점에서만 어댑터를 호출하므로,
  라우트 매핑이 확정되면 어댑터 내부 경로만 교체하면 된다(플랜/스텝 스키마 무변경).

### 13.3 `read + external` 표기 제약 (해소)

- AGPilot0006에서 `UnifiedToolSpec(permission="read", side_effect="external")` 조합이 거부되는 제약이 확인되었다
  (읽기 전용 웹 검색 `agtools.web.search`). 본 저장소는 §8대로 **해당 조합만 허용**하도록 검증을 완화했다.
- 남은 정렬 항목: AGPilot0006이 동일 규칙(또는 `permission="write"` 표기 유지)을 채택하는지 확인 필요.
  본 저장소 기준으로는 `external+read` 채택을 권장한다(웹 검색을 write 권한 없이 호출 가능).

### 13.4 GATEWAY 계약 (AGPilot0010 `e317130`, `docs/r3/GATEWAY_R3_CONTRACT.md`)

- 게이트웨이는 `workflow.*` 이벤트를 **가공 없이 통과**시키고 `Cache-Control: no-store`,
  `X-Accel-Buffering: no`를 유지해야 한다(SSE 버퍼링 방지).
- `POST /runs` 응답의 `202 + awaiting_approval`을 성공으로 취급하고, `pending_approval.approval_id`를
  프런트 승인 UI로 전달해야 한다.
- 게이트웨이 단계에서 요청 본문의 `capabilities`를 임의로 확장하지 말 것(권한 위임은 AGCore 자격증명이 결정).

---

## 14. Codex 통합 및 예상 충돌 목록

### 14.1 신규 파일 — 충돌 없음

`app/workflow/planner.py`, `checkpoint.py`, `trajectory.py`, `runtime.py`, `app/api/workflow.py`,
`tests/test_r3_workflow_acceptance.py`, `docs/r3/WORKFLOW_EXECUTION_CONTRACT.md`.

### 14.2 수정 파일 — 충돌 가능 지점과 해소

| 파일 | 변경 | 충돌 위험 | 해소 |
|---|---|---|---|
| `app/schemas/events.py` | `EventType` Literal에 9줄 추가 | Codex가 같은 Literal을 편집하면 hunk 충돌 | Literal 끝에 블록 단위로 추가되어 있어 **append-only 병합**으로 해결 |
| `app/main.py` | import 1줄 + `include_router` 1줄 | 라우터 등록 순서 변경과 충돌 | `canvas_router` 바로 뒤에 삽입. 순서 무관(경로 prefix 상이) |
| `app/workflow/__init__.py` | export 4블록 추가(총 56줄) | 선행 엔진 export와 같은 파일 | 기존 `__all__` 항목 무변경, 뒤에 블록 추가 |
| `app/workflow/registry.py` | `_consistent_policy` 조건 1건, `is_read_only` 주석 | 정책 변경은 의미 충돌 가능 | §8에 근거·대안·테스트 명시. `write+read` 거부는 유지 |
| `data/runtime_config.json` | 테스트 실행 중 생성된 `compaction_policy` | 런타임 설정 파일 | **커밋에서 제외**(작업트리 정리 시 HEAD로 복원) |

### 14.3 인터페이스 고정점 (Codex/게이트웨이가 의존해도 되는 것)

- `WORKFLOW_EVENT_TYPES` 9종 문자열과 §11.2 payload 필드명
- `PlannerDecision.route/reason/plan`, `ExecutionPlan.summary()` 필드
- `WorkflowRunResult` 필드(`run_id, status, route, reason, plan_id, plan_version, steps, answer, artifacts, failures, pending_approval, error, token_usage, duration_ms, trajectory`)
- 오류 봉투 `{detail:{code,message,retryable,context}}`
- `pending_approval.approval_id` ↔ `ApprovalToken.approval_id` 일치 규칙(`apreq_<run_id>_<step_id>`)

### 14.4 크로스 레포 정렬 완료 항목 (전달받은 선행 결과)

| 저장소 | 브랜치 / 커밋 | 정렬 내용 | 본 저장소 반영 |
|---|---|---|---|
| **AGPilot0006** | `feat/r3-workflow-extensibility` @ `98113bc` | `app/r3_capabilities.py` 라우트 19종 매핑(`workspace.rag.search` → `agtools.document.search`), `office.*` 4종은 Mock 담당, `read+external` 표기 제약 확인 | §13.2 / §13.3, `registry.py` 정책 완화 + `test_read_only_external_tool_spec_is_allowed` |
| **AGPilot0010** | `feat/r3-workflow-extensibility` @ `e317130` | `docs/r3/GATEWAY_R3_CONTRACT.md` 추가 | §13.4 (게이트웨이 통과·버퍼링·202 처리 요구) |
| **AGPilot0011** | `feat/r3-workflow-extensibility` @ `3d63150` | `hd2ai-shared/workflow-contract.ts`에 `workflow.*` SSE 9종 + `HumanApprovalGatePayload` 타입 추가 | §11.2 payload 표를 정본으로 대조 |

> **확인 필요(정직한 미확인 항목)**: `hd2ai-shared/workflow-contract.ts`의 실제 필드명(snake_case/camelCase)과
> `HumanApprovalGatePayload` 구성은 본 저장소에서 직접 확인하지 않았다. §11.2 표의 snake_case 이름을 기준으로
> 게이트웨이에서 camelCase 매핑해야 하며, 타입 정의와 1:1 대조가 남아 있다.
> `GATEWAY_R3_CONTRACT.md`의 실제 조항 번호도 대조가 필요하다(요구사항 수준만 반영).

---

## 15. 운영 파라미터 기본값

| 파라미터 | 기본 | 위치 |
|---|---|---|
| `max_steps` | 10 (요청/예산으로 하향 가능) | `Planner`, `WorkflowRuntime` |
| `max_retries` | 2 (스텝별 `max_retries`가 우선) | 동일 |
| `max_replans` | 1 | 동일 |
| `max_tool_calls` | 24 / 런 | `ContextBudgetManager` |
| `max_context_tokens` | 12 000 | 동일 |
| 승인 토큰 TTL | 300s | `ApprovalAuthority(default_ttl_seconds=300)` |
| 훅 타임아웃 | 5s (훅별 0~120s) | `HookDefinition` |
| MCP 타임아웃/재시도 | 15s / 2 | `MCPServerConfig` |

---

## 16. 남은 작업 (R3 후속)

1. **LLM 자동 플래닝**: `Planner(model=...)` 훅만 존재. 모델 호출 시 `model_calls` 카운터로 fast-path 보장 유지 필요.
2. **AGPolicy 학습 파이프라인**: `learning_payload()` 소비자(배치 러너/보상 정의) 미구현.
3. **R2.1 실 어댑터**: `RuntimeOfficeAdapter` 엔드포인트 확정(AGPilot0006 19종 매핑 대조) + production 전환.
4. **admin cross-tenant 열람 정책**: 현재 `is_admin + can_access_tenant` 예외만 존재. 감사 로그 요구사항 확정 필요.
5. **취소 강화**: 도구 호출 중 즉시 중단(현재는 협조적), 취소와 최종 저장 간 경합 정리.
6. **텔레메트리 연동**: Trajectory → 기존 tracing store/Prometheus 브리지.
7. **`hd2ai-shared` 타입 1:1 대조**: §11.2 ↔ `workflow-contract.ts`.
8. **선행 회귀 1건**: `test_add_02_durable_analysis_real_orchestrator_create_resume_and_no_legacy_fallback`
   (`552df4b`부터 실패, `bootstrap.py:41`) — R3 범위 밖이지만 별도 수정 필요.
