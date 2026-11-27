"""Structure-aware, token-bounded chunking for workspace documents.

Used only when the upstream parser (AGTools/AGPilot0006) does not already return chunks for a
file — plain text/markdown or AGTools text-only extraction. AGTools chunking results are
reused untouched (see `ingest.py`), so this module never re-splits parser output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.workspace.textutil import estimate_tokens

CHUNKING_VERSION = "r2w-chunker-v1"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\n+")


@dataclass
class SourceChunk:
    content: str
    token_count: int
    page_number: Optional[int] = None
    section: Optional[str] = None
    offset_start: int = 0
    offset_end: int = 0
    metadata: dict = field(default_factory=dict)


def _split_long_block(block: str, max_tokens: int) -> List[str]:
    """Split an oversized paragraph on sentence/newline boundaries, then hard-cut."""

    pieces: List[str] = []
    buffer: List[str] = []
    tokens = 0
    for sentence in _SENTENCE_SPLIT.split(block):
        sentence = sentence.strip()
        if not sentence:
            continue
        sentence_tokens = estimate_tokens(sentence)
        if sentence_tokens > max_tokens:
            if buffer:
                pieces.append(" ".join(buffer).strip())
                buffer, tokens = [], 0
            # Hard cut: walk characters until the token budget is met.
            step = max(64, len(sentence) // max(1, (sentence_tokens // max_tokens) + 1))
            for start in range(0, len(sentence), step):
                pieces.append(sentence[start : start + step].strip())
            continue
        if tokens + sentence_tokens > max_tokens and buffer:
            pieces.append(" ".join(buffer).strip())
            buffer, tokens = [], 0
        buffer.append(sentence)
        tokens += sentence_tokens
    if buffer:
        pieces.append(" ".join(buffer).strip())
    return [piece for piece in pieces if piece]


def chunk_text(
    text: str,
    *,
    target_tokens: int = 768,
    overlap_tokens: int = 96,
    max_tokens: int = 1024,
    page_number: Optional[int] = None,
    section: Optional[str] = None,
) -> List[SourceChunk]:
    """Split `text` into paragraph-first chunks of ~`target_tokens` with overlap."""

    cleaned = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not cleaned:
        return []
    target = max(64, int(target_tokens))
    hard_max = max(target, int(max_tokens))
    overlap = max(0, min(int(overlap_tokens), target // 2))

    blocks: List[str] = []
    for paragraph in re.split(r"\n\s*\n", cleaned):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if estimate_tokens(paragraph) > hard_max:
            blocks.extend(_split_long_block(paragraph, hard_max))
        else:
            blocks.append(paragraph)

    chunks: List[SourceChunk] = []
    current: List[str] = []
    current_tokens = 0
    cursor = 0

    def flush() -> None:
        nonlocal current, current_tokens, cursor
        if not current:
            return
        content = "\n\n".join(current).strip()
        if not content:
            current, current_tokens = [], 0
            return
        start = cleaned.find(current[0], cursor)
        if start < 0:
            start = cursor
        end = cleaned.find(current[-1], start) + len(current[-1])
        cursor = end
        chunks.append(
            SourceChunk(
                content=content,
                token_count=estimate_tokens(content),
                page_number=page_number,
                section=section,
                offset_start=start,
                offset_end=end,
            )
        )
        if overlap and current:
            tail: List[str] = []
            tail_tokens = 0
            for block in reversed(current):
                block_tokens = estimate_tokens(block)
                if tail_tokens + block_tokens > overlap:
                    break
                tail.insert(0, block)
                tail_tokens += block_tokens
            current = tail
            current_tokens = tail_tokens
        else:
            current, current_tokens = [], 0

    for block in blocks:
        block_tokens = estimate_tokens(block)
        if current and current_tokens + block_tokens > target:
            flush()
            if current and current_tokens + block_tokens > target:
                # overlap alone already fills the budget; drop it for this block
                current, current_tokens = [], 0
        current.append(block)
        current_tokens += block_tokens
        if current_tokens >= hard_max:
            flush()
    flush()

    if not chunks:
        chunks = [
            SourceChunk(
                content=cleaned,
                token_count=estimate_tokens(cleaned),
                page_number=page_number,
                section=section,
                offset_start=0,
                offset_end=len(cleaned),
            )
        ]
    # Drop a chunk that is byte-identical to the one directly before it (overlap artefact on
    # tiny inputs). Repeated identical paragraphs elsewhere in the document must be kept.
    deduped: List[SourceChunk] = []
    previous: Optional[str] = None
    for chunk in chunks:
        if previous is not None and chunk.content == previous:
            continue
        deduped.append(chunk)
        previous = chunk.content
    return deduped
