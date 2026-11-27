"""Diff engine (WO-C04 / WO-C05).

Two representations are produced for every comparison:

* ``hunks`` — line-based, 1-based line numbers, one entry per changed region with per-line
  operations. This is what the Canvas Diff panel renders.
* ``unified`` — a standard unified diff string, useful for logs, review tools and tests.

Line diffing uses ``difflib.SequenceMatcher`` (stdlib, deterministic, no new dependency). For a
replaced line pair, a character-level emphasis is computed so the UI can highlight the changed
fragment instead of repainting whole lines. Emphasis ranges are expressed in **code points** and
declared as such, because they are produced by the backend from Python strings.

Comparison is always between two *stored* revisions of the same artifact (old → new), and never
mutates content, so a preview can be produced before the user approves an edit.
"""
from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Sequence, Tuple

CHAR_EMPHASIS_MAX_CHARS = 400
MAX_HUNK_LINES = 400
CONTEXT_LINES = 3


def split_lines(text: str) -> List[str]:
    """Split keeping line content without terminators; ``splitlines`` handles CRLF/CR/LF."""
    return text.splitlines()


def _emphasis(old_line: str, new_line: str) -> Tuple[List[Dict[str, int]], List[Dict[str, int]]]:
    """Character spans that differ inside a replaced line pair (code-point offsets)."""
    if len(old_line) > CHAR_EMPHASIS_MAX_CHARS or len(new_line) > CHAR_EMPHASIS_MAX_CHARS:
        return [], []
    matcher = difflib.SequenceMatcher(None, old_line, new_line, autojunk=False)
    old_spans: List[Dict[str, int]] = []
    new_spans: List[Dict[str, int]] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete") and i2 > i1:
            old_spans.append({"start": i1, "end": i2})
        if tag in ("replace", "insert") and j2 > j1:
            new_spans.append({"start": j1, "end": j2})
    return old_spans, new_spans


def _line(
    kind: str,
    text: str,
    old_line_no: Optional[int],
    new_line_no: Optional[int],
    emphasis: Optional[List[Dict[str, int]]] = None,
) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "type": kind,  # equal | insert | delete
        "text": text,
        "old_line_no": old_line_no,
        "new_line_no": new_line_no,
    }
    if emphasis:
        entry["emphasis"] = emphasis
        entry["emphasis_unit"] = "codepoint"
    return entry


def diff_texts(
    old: str,
    new: str,
    *,
    from_revision: Optional[int] = None,
    to_revision: Optional[int] = None,
    artifact_id: Optional[str] = None,
    context_lines: int = CONTEXT_LINES,
    max_lines: int = MAX_HUNK_LINES,
    include_unified: bool = True,
) -> Dict[str, Any]:
    """Return a structured, render-ready diff between two contents."""
    old_lines = split_lines(old)
    new_lines = split_lines(new)
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)

    hunks: List[Dict[str, Any]] = []
    added = removed = changed = 0
    truncated = False
    emitted_lines = 0

    for group in matcher.get_grouped_opcodes(context_lines):
        hunk_lines: List[Dict[str, Any]] = []
        hunk_added = hunk_removed = 0
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for offset in range(i2 - i1):
                    hunk_lines.append(_line("equal", old_lines[i1 + offset], i1 + offset + 1, j1 + offset + 1))
            elif tag == "delete":
                for offset in range(i2 - i1):
                    hunk_lines.append(_line("delete", old_lines[i1 + offset], i1 + offset + 1, None))
                hunk_removed += i2 - i1
            elif tag == "insert":
                for offset in range(j2 - j1):
                    hunk_lines.append(_line("insert", new_lines[j1 + offset], None, j1 + offset + 1))
                hunk_added += j2 - j1
            else:  # replace
                paired = min(i2 - i1, j2 - j1)
                for offset in range(paired):
                    old_span, new_span = _emphasis(old_lines[i1 + offset], new_lines[j1 + offset])
                    hunk_lines.append(
                        _line("delete", old_lines[i1 + offset], i1 + offset + 1, None, old_span)
                    )
                    hunk_lines.append(
                        _line("insert", new_lines[j1 + offset], None, j1 + offset + 1, new_span)
                    )
                    changed += 1
                for offset in range(paired, i2 - i1):
                    hunk_lines.append(_line("delete", old_lines[i1 + offset], i1 + offset + 1, None))
                    hunk_removed += 1
                for offset in range(paired, j2 - j1):
                    hunk_lines.append(_line("insert", new_lines[j1 + offset], None, j1 + offset + 1))
                    hunk_added += 1
                hunk_removed += paired
                hunk_added += paired

        if emitted_lines + len(hunk_lines) > max_lines:
            truncated = True
            break
        emitted_lines += len(hunk_lines)
        added += hunk_added
        removed += hunk_removed
        hunks.append(
            {
                "old_start": group[0][1] + 1,
                "old_lines": group[-1][2] - group[0][1],
                "new_start": group[0][3] + 1,
                "new_lines": group[-1][4] - group[0][3],
                "added_lines": hunk_added,
                "removed_lines": hunk_removed,
                "lines": hunk_lines,
            }
        )

    result: Dict[str, Any] = {
        "artifact_id": artifact_id,
        "from_revision": from_revision,
        "to_revision": to_revision,
        "identical": old == new,
        "stats": {
            "added_lines": added,
            "removed_lines": removed,
            "changed_line_pairs": changed,
            "hunks": len(hunks),
            "old_line_count": len(old_lines),
            "new_line_count": len(new_lines),
        },
        "hunks": hunks,
        "truncated": truncated,
        "emphasis_unit": "codepoint",
    }
    if include_unified:
        unified = list(
            difflib.unified_diff(
                [line + "\n" for line in old_lines],
                [line + "\n" for line in new_lines],
                fromfile=f"revision-{from_revision}" if from_revision is not None else "old",
                tofile=f"revision-{to_revision}" if to_revision is not None else "new",
                n=context_lines,
            )
        )
        result["unified"] = "".join(unified)
    return result


def selection_preview(
    content: str,
    start: int,
    end: int,
    replacement: str,
    *,
    artifact_id: Optional[str] = None,
    base_revision: Optional[int] = None,
    context_lines: int = CONTEXT_LINES,
) -> Dict[str, Any]:
    """Diff for a proposed replacement of ``content[start:end]`` without storing anything."""
    updated = content[:start] + replacement + content[end:]
    diff = diff_texts(
        content,
        updated,
        from_revision=base_revision,
        to_revision=None,
        artifact_id=artifact_id,
        context_lines=context_lines,
    )
    diff["proposed_replacement"] = replacement
    diff["selection"] = {"start": start, "end": end}
    return diff


def summarize(diff: Dict[str, Any]) -> str:
    stats = diff.get("stats") or {}
    return (
        f"+{stats.get('added_lines', 0)} -{stats.get('removed_lines', 0)} "
        f"in {stats.get('hunks', 0)} hunk(s)"
    )


def first_change_lines(diff: Dict[str, Any]) -> List[Tuple[Optional[int], Optional[int]]]:
    """(old_line_no, new_line_no) of every changed line, for jump-to-change UI."""
    positions: List[Tuple[Optional[int], Optional[int]]] = []
    for hunk in diff.get("hunks") or []:
        for line in hunk.get("lines") or []:
            if line.get("type") != "equal":
                positions.append((line.get("old_line_no"), line.get("new_line_no")))
    return positions
