"""Ground public model answers with verified AGTools search results.

The public local model does not reliably implement required tool selection. AGCore
therefore schedules the read-only search itself and passes the result as plain
context to that model, while the dispatcher retains document/owner checks.
"""
import json
from typing import List

from app.schemas.gateway import FunctionCall, GatewayMessage, ToolCall
from app.runtime.state import AgentState


def public_outline_calls(state: AgentState) -> List[ToolCall]:
    return [
        ToolCall(
            id=f"public-outline-{index}",
            function=FunctionCall(
                name="document.get_outline",
                arguments=json.dumps({"doc_id": doc_id}),
            ),
        )
        for index, doc_id in enumerate(state.context_refs)
    ]


def public_section_calls(state: AgentState) -> List[ToolCall]:
    per_document = min(3, max(1, (state.max_tool_calls - 2 * len(state.context_refs)) // len(state.context_refs)))
    question = next((message.content or "" for message in reversed(state.messages) if message.role == "user"), "")
    calls: List[ToolCall] = []
    for message in state.messages:
        if message.role != "tool" or message.name != "document.get_outline":
            continue
        try:
            outline = json.loads(message.content or "{}")
        except ValueError:
            continue
        doc_id = outline.get("doc_id")
        if doc_id not in state.context_refs:
            continue
        for section in outline.get("outline", [])[:per_document]:
            section_id = section.get("section_id")
            if not isinstance(section_id, str):
                continue
            calls.append(ToolCall(
                id=f"public-section-{len(calls)}",
                function=FunctionCall(
                    name="document.read_section",
                    arguments=json.dumps({"doc_id": doc_id, "section_id": section_id}),
                ),
            ))
        calls.append(ToolCall(
            id=f"public-search-{len(calls)}",
            function=FunctionCall(
                name="document.search",
                arguments=json.dumps({"doc_id": doc_id, "query": question, "limit": 5}),
            ),
        ))
    return calls


def public_chunk_calls(state: AgentState) -> List[ToolCall]:
    """Read ranked search hits before answering; snippets only locate evidence."""
    remaining = state.max_tool_calls - state.tool_call_count
    calls, seen = [], set()
    searches = []
    for message in state.messages:
        if message.role != 'tool' or message.name != 'document.search':
            continue
        try:
            searches.append(json.loads(message.content or '{}'))
        except ValueError:
            continue
    # Round-robin ranked hits avoids spending the remaining budget on one document.
    for rank in range(5):
        for search in searches:
            doc = search.get('doc_id')
            hits = search.get('results', [])
            if doc not in state.context_refs or rank >= len(hits):
                continue
            chunk = hits[rank].get('chunk_id')
            if not isinstance(chunk, str) or (doc, chunk) in seen:
                continue
            if len(calls) >= remaining:
                return calls
            seen.add((doc, chunk))
            calls.append(ToolCall(id=f'public-chunk-{len(calls)}',function=FunctionCall(
                name='document.read_chunk',arguments=json.dumps({'doc_id':doc,'chunk_id':chunk}))))
    return calls


def public_grounded_messages(state: AgentState) -> List[GatewayMessage]:
    question = next((m.content or '' for m in reversed(state.messages) if m.role == 'user'), '')
    chunks = [m.content or '' for m in state.messages if m.role == 'tool' and m.name == 'document.read_chunk']
    sections = [m.content or '' for m in state.messages if m.role == 'tool' and m.name == 'document.read_section']
    # Whole verified bodies first. Never present search snippets as full evidence.
    blocks, size, omitted = [], 0, 0
    for body in chunks + sections:
        if size + len(body) > 12000:
            omitted += 1
            continue
        blocks.append(body);size += len(body)
    results = '\n\n'.join(blocks)
    if omitted:
        results += f'\n[문맥 예산으로 본문 {omitted}개를 제외했습니다. 해당 범위는 읽었다고 주장하지 마세요.]'
    if not blocks:
        results = '조회 가능한 본문 근거가 없습니다. 검색 스니펫만으로 확정하지 마세요.'
    history, width = [], 0
    for m in reversed(state.messages):
        if m.role not in ('user', 'assistant') or not m.content or m.tool_calls or m.content == question:
            continue
        if width + len(m.content) > 4000 or len(history) >= 6:
            break
        history.insert(0, {'role':m.role, 'content':m.content});width += len(m.content)
    return [GatewayMessage(role='user',content=(
        '다음 조회한 문서 본문만 근거로 현재 질문에 직접 답하세요. 본문에 없는 내용은 모른다고 말하세요. '
        '이력은 대화 대상 해석에만 사용하고 사실 근거로 간주하지 마세요.\n\n'
        f'[대화 이력]\n{json.dumps(history,ensure_ascii=False)}\n\n[현재 질문]\n{question}\n\n[문서 본문]\n{results}'))]
