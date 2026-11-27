"""Patch & Diff engine — pure operation semantics (WO-C05).

The engine never regenerates a whole document to change a region: each of the six operations
computes the new content from the *current* content plus a range, and every operation is guarded by
both ``base_revision`` and ``expected_original_text`` so a stale client can never silently
overwrite newer work.

This module holds only the pure text transformation. Conflict detection that must be atomic with
the write (base revision vs current revision, read of current content inside the transaction) lives
in ``app.canvas.storage``, which calls ``compute_patch`` inside its ``BEGIN IMMEDIATE``
transaction.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.models import PatchRequest
from app.canvas.offsets import Selection, codepoint_to_utf16_index, to_codepoint_range


@dataclass
class PatchComputation:
    content: str
    changed: bool
    applied_range_utf16: Optional[Dict[str, int]] = None
    replaced_codepoints: int = 0
    inserted_codepoints: int = 0


def _require_selection(request: PatchRequest) -> Selection:
    selection = request.resolved_selection()
    if selection is None:
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            f"operation '{request.operation}' requires 'selection' (or 'target_range')",
            context={"operation": request.operation},
        )
    return selection


def _require_replacement(request: PatchRequest, *, allow_empty: bool = False) -> str:
    if request.replacement_text is None:
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            f"operation '{request.operation}' requires 'replacement_text'",
            context={"operation": request.operation},
        )
    if not allow_empty and request.replacement_text == "":
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            f"operation '{request.operation}' requires a non-empty 'replacement_text'",
            context={"operation": request.operation},
        )
    return request.replacement_text


def _check_expected(actual: str, expected: Optional[str], *, offset: int) -> None:
    if expected is None:
        raise CanvasError(
            CanvasErrorCode.INVALID_REQUEST,
            "'expected_original_text' is required for this operation (silent overwrite is forbidden)",
            context={"offset": offset},
        )
    if expected != actual:
        raise CanvasError(
            CanvasErrorCode.EXPECTED_TEXT_MISMATCH,
            "'expected_original_text' does not match the current content in this range",
            retryable=True,
            context={
                "offset": offset,
                "expected_length": len(expected),
                "actual_length": len(actual),
                "actual_preview": actual[:120],
            },
        )


def compute_patch(content: str, request: PatchRequest) -> PatchComputation:
    """Apply one operation to ``content`` and return the new content (no persistence)."""
    operation = request.operation

    if operation == "replace_content":
        if request.resolved_selection() is not None:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "'replace_content' replaces the whole document and takes no selection",
            )
        replacement = _require_replacement(request, allow_empty=True)
        if request.expected_original_text is not None and request.expected_original_text != content:
            raise CanvasError(
                CanvasErrorCode.EXPECTED_TEXT_MISMATCH,
                "'expected_original_text' does not match the current document",
                retryable=True,
                context={
                    "expected_length": len(request.expected_original_text),
                    "actual_length": len(content),
                    "actual_preview": content[:120],
                },
            )
        end = len(replacement)
        return PatchComputation(
            content=replacement,
            changed=replacement != content,
            applied_range_utf16={"start": 0, "end": codepoint_to_utf16_index(replacement, end)},
            replaced_codepoints=len(content),
            inserted_codepoints=len(replacement),
        )

    if operation == "append_content":
        if request.resolved_selection() is not None:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "'append_content' appends at the end and takes no selection",
            )
        replacement = _require_replacement(request)
        if request.expected_original_text not in (None, ""):
            raise CanvasError(
                CanvasErrorCode.EXPECTED_TEXT_MISMATCH,
                "'append_content' expects an empty 'expected_original_text'",
                retryable=True,
                context={"expected_length": len(request.expected_original_text)},
            )
        updated = content + replacement
        start_unit = codepoint_to_utf16_index(updated, len(content))
        return PatchComputation(
            content=updated,
            changed=True,
            applied_range_utf16={"start": start_unit, "end": codepoint_to_utf16_index(updated, len(updated))},
            inserted_codepoints=len(replacement),
        )

    selection = _require_selection(request)
    start, end = to_codepoint_range(content, selection)
    selected = content[start:end]

    if operation == "replace_selection":
        replacement = _require_replacement(request, allow_empty=True)
        _check_expected(selected, request.expected_original_text, offset=selection.start)
        updated = content[:start] + replacement + content[end:]
        return PatchComputation(
            content=updated,
            changed=updated != content,
            applied_range_utf16={
                "start": codepoint_to_utf16_index(updated, start),
                "end": codepoint_to_utf16_index(updated, start + len(replacement)),
            },
            replaced_codepoints=len(selected),
            inserted_codepoints=len(replacement),
        )

    if operation == "delete_selection":
        if request.replacement_text not in (None, ""):
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "'delete_selection' takes no replacement text",
            )
        _check_expected(selected, request.expected_original_text, offset=selection.start)
        if start == end:
            raise CanvasError(
                CanvasErrorCode.INVALID_SELECTION,
                "delete_selection requires a non-empty range",
                context={"start": selection.start, "end": selection.end},
            )
        updated = content[:start] + content[end:]
        return PatchComputation(
            content=updated,
            changed=True,
            applied_range_utf16={
                "start": codepoint_to_utf16_index(updated, start),
                "end": codepoint_to_utf16_index(updated, start),
            },
            replaced_codepoints=len(selected),
        )

    if operation in ("insert_before", "insert_after"):
        replacement = _require_replacement(request)
        _check_expected(selected, request.expected_original_text, offset=selection.start)
        position = start if operation == "insert_before" else end
        updated = content[:position] + replacement + content[position:]
        return PatchComputation(
            content=updated,
            changed=True,
            applied_range_utf16={
                "start": codepoint_to_utf16_index(updated, position),
                "end": codepoint_to_utf16_index(updated, position + len(replacement)),
            },
            inserted_codepoints=len(replacement),
        )

    raise CanvasError(
        CanvasErrorCode.INVALID_REQUEST,
        f"unsupported patch operation '{operation}'",
        context={"operation": operation},
    )


def preview_patch(content: str, request: PatchRequest) -> PatchComputation:
    """Same semantics, used to render a diff preview before the user approves."""
    return compute_patch(content, request)
