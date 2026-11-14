"""Task invariants. No HTTP clients, asyncio tasks, or filesystem access."""
import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


class AccessContext(BaseModel):
    model_config = ConfigDict(frozen=True)
    owner: str
    service: str


class Plan(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    objective: str = Field(min_length=1, max_length=3000)
    intent: Literal['summary', 'lookup', 'extract', 'compare', 'conversation', 'clarify']
    scope: Literal['all', 'relevant', 'none']
    entity_type: Literal['api', 'general'] = 'general'
    required_outputs: list[str] = Field(min_length=1, max_length=20)
    search_terms: list[str] = Field(default_factory=list, max_length=8)
    completion_criteria: list[str] = Field(min_length=1, max_length=20)
    steps: list[Literal['read', 'search', 'write', 'verify', 'clarify']] = Field(default_factory=lambda: ['read', 'write', 'verify'], max_length=10)
    revision: int = Field(default=1, ge=1)


class BudgetExceeded(ValueError):
    pass


class BudgetLedger(BaseModel):
    model_calls: int = 0
    document_calls: int = 0
    actual_tokens: int = 0
    estimated_input_tokens: int = 0
    max_model_calls: int = 500
    max_document_calls: int = 5000
    deadline: float

    def reserve(self, kind: Literal['model', 'document'], now: float):
        if now >= self.deadline:
            raise BudgetExceeded('task_deadline_exceeded')
        attr, limit = ('model_calls', self.max_model_calls) if kind == 'model' else ('document_calls', self.max_document_calls)
        if getattr(self, attr) >= limit:
            raise BudgetExceeded(kind + '_call_budget_exceeded')
        setattr(self, attr, getattr(self, attr) + 1)


class Evidence(BaseModel):
    key: str
    doc_id: str
    chunk_id: str
    content_hash: str
    snapshot_id: str = ''
    content: str
    source: dict = Field(default_factory=dict)


class Section(BaseModel):
    model_config = ConfigDict(extra='forbid')
    heading: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1, max_length=20000)
    refs: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)


class Draft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    sections: list[Section] = Field(min_length=1, max_length=40)

    def render(self):
        return '\n\n'.join('### ' + s.heading + '\n\n' + s.body + ('\n\n출처: ' + ', '.join('[' + r + ']' for r in s.refs) if s.refs else '') for s in self.sections)


class Verification(BaseModel):
    model_config = ConfigDict(extra='forbid')
    satisfied: bool
    missing_requirements: list[str]
    unsupported_claims: list[str]


class UserReply(BaseModel):
    model_config = ConfigDict(extra='forbid')
    content: str = Field(min_length=1, max_length=20000)
    kind: Literal['answer', 'clarification']


class CompositionNode(BaseModel):
    """A resumable synthesis checkpoint; original drafts remain untouched."""
    draft: Draft | None = None
    verification: Verification | None = None
    attempts: int = 0


class Task(BaseModel):
    schema_version: int = 2
    job_id: str
    owner_id: str
    service_id: str
    question: str
    session_id: str | None = None
    history: list[dict] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    analysis_refs: list[str] = Field(default_factory=list)
    status: Literal['queued', 'planning', 'running', 'verifying', 'completed', 'needs_input', 'partial', 'paused', 'failed', 'cancelled'] = 'queued'
    plan: Plan | None = None
    budget: BudgetLedger
    snapshots: dict[str, str] = Field(default_factory=dict)
    entries: list[dict] = Field(default_factory=list)
    evidence: dict[str, Evidence] = Field(default_factory=dict)
    selected_keys: list[str] = Field(default_factory=list)
    drafts: dict[str, Draft] = Field(default_factory=dict)
    verified_groups: list[str] = Field(default_factory=list)
    verification_reports: dict[str, Verification] = Field(default_factory=dict)
    inventory_pages: dict[str, list[str]] = Field(default_factory=dict)
    inventory: list[str] = Field(default_factory=list)
    answer: str | None = None
    error_code: str | None = None
    created_at: float
    updated_at: float
    storage_revision: int = 0
    replans: int = 0
    answer_repairs: int = 0
    planning_errors: list[str] = Field(default_factory=list)
    contract_errors: list[str] = Field(default_factory=list)
    request_parts: dict[str, dict] = Field(default_factory=dict)
    composition_nodes: dict[str, CompositionNode] = Field(default_factory=dict)
    final_draft: Draft | None = None
    final_verification: Verification | None = None
    final_signature: str | None = None
    reasoning_effort: str = 'off'
    reasoning_text: str = ''
    phase: str = 'queued'
    events: list[dict] = Field(default_factory=list)
    document_titles: dict[str, str] = Field(default_factory=dict)
    public_reply: UserReply | None = None
    reply_verification: Verification | None = None
    reply_signature: str | None = None

    def response_signature(self):
        data = {'question': self.question, 'history': self.history, 'plan': self.plan.model_dump(),
                'reply': self.public_reply.model_dump() if self.public_reply else None}
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def requires_composition(self):
        return bool(self.plan and (self.plan.intent in ('summary', 'compare') or
                    (len(self.drafts) > 1 and not (self.plan.intent == 'extract' and self.plan.entity_type == 'api'))))

    def composition_signature(self):
        data = {'plan': self.plan.model_dump() if self.plan else None,
                'drafts': {k: d.model_dump() for k, d in self.drafts.items()},
                'final': self.final_draft.model_dump() if self.final_draft else None}
        return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def owned_by(self, access):
        return self.owner_id == access.owner and self.service_id == access.service

    def apply_plan(self, plan):
        if self.status in ('completed', 'cancelled'):
            raise ValueError('invalid_task_transition')
        if plan.scope != 'none' and not self.document_ids:
            raise ValueError('plan_requires_document')
        if plan.intent in ('summary', 'extract', 'compare') and plan.scope != 'all':
            raise ValueError('incomplete_document_plan')
        self.plan = plan
        self.status = 'running'

    def complete(self):
        if self.status != 'verifying' or not self.plan:
            raise ValueError('unverified_task')
        if self.plan.scope == 'none':
            v = self.reply_verification
            if (not self.public_reply or not v or not v.satisfied or v.missing_requirements or v.unsupported_claims
                    or self.reply_signature != self.response_signature()
                    or self.public_reply.content.strip() == self.plan.objective.strip()):
                raise ValueError('unverified_user_reply')
            expected = 'clarification' if self.plan.intent == 'clarify' else 'answer'
            if self.public_reply.kind != expected:
                raise ValueError('invalid_reply_kind')
            self.answer = self.public_reply.content
            self.status = 'needs_input' if expected == 'clarification' else 'completed'
            self.error_code = None
            return
        if set(self.verified_groups) != set(self.drafts) or not self.drafts:
            raise ValueError('unverified_answer')
        if self.plan.scope != 'none':
            required = set(self.selected_keys)
            if not required or not required.issubset(self.evidence):
                raise ValueError('evidence_incomplete')
            if self.plan.scope == 'all' and required != {e['doc_id'] + '/' + e['chunk_id'] for e in self.entries}:
                raise ValueError('coverage_incomplete')
        if self.requires_composition():
            v = self.final_verification
            if (not self.final_draft or not v or not v.satisfied or v.missing_requirements or
                    v.unsupported_claims or self.final_signature != self.composition_signature()):
                raise ValueError('final_answer_unverified')
            headings = [re.sub(r'\s+', '', s.heading).casefold() for s in self.final_draft.sections]
            if len(headings) != len(set(headings)):
                raise ValueError('final_answer_repeated_headings')
            self.answer = self.final_draft.render()
        else:
            self.answer = '\n\n'.join(d.render() for d in self.drafts.values())
        if any(name not in self.answer for name in self.inventory):
            self.answer = None
            raise ValueError('extraction_inventory_incomplete')
        self.status = 'completed'
        self.error_code = None

    def record_verification(self, group_id: str, verification: Verification):
        if self.status != 'verifying' or group_id not in self.drafts or not verification.satisfied or verification.missing_requirements or verification.unsupported_claims:
            raise ValueError('verification_not_passed')
        if group_id not in self.verified_groups:
            self.verified_groups.append(group_id)

    def resume(self, now):
        if self.status not in ('paused', 'partial', 'failed', 'cancelled'):
            raise ValueError('task_not_resumable')
        if now >= self.budget.deadline:
            raise BudgetExceeded('task_deadline_exceeded')
        self.status, self.error_code = 'queued', None
