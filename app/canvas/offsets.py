"""Offset contract (Backend ↔ Frontend) for Canvas selections.

The browser edits text with ``textarea.selectionStart`` / ``String.prototype.slice``, which count
**UTF-16 code units**. Python indices count **code points**. Korean text lives in the BMP so both
agree, but astral characters (emoji, some CJK ext. B, mathematical letters) make them differ, and a
range that splits a surrogate pair can never be represented in a DOM selection. This module is the
single conversion point for the whole engine so no module invents its own rule.

Accepted units: ``utf16`` (default) and ``codepoint``. ``utf8_byte`` is not accepted — browsers do
not produce it and mixing byte offsets with character offsets is how silent corruption happens.
"""
from __future__ import annotations

from typing import Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.canvas.errors import CanvasError, CanvasErrorCode

OffsetUnit = Literal["utf16", "codepoint"]
DEFAULT_OFFSET_UNIT: OffsetUnit = "utf16"


class Selection(BaseModel):
    """A half-open range ``[start, end)`` in the declared offset unit."""

    model_config = ConfigDict(extra="forbid")

    start: int = Field(ge=0)
    end: int = Field(ge=0)
    offset_unit: OffsetUnit = DEFAULT_OFFSET_UNIT

    @property
    def is_caret(self) -> bool:
        return self.start == self.end


def utf16_length(text: str) -> int:
    """Number of UTF-16 code units (what ``String.length`` reports in JS)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def content_metrics(text: str) -> Dict[str, int]:
    """Length of the same text under every unit a client may reason about."""
    return {
        "codepoints": len(text),
        "utf16_units": utf16_length(text),
        "utf8_bytes": len(text.encode("utf-8", "surrogatepass")),
    }


def utf16_to_codepoint_index(text: str, offset: int) -> int:
    """Convert a UTF-16 code-unit offset to a Python code-point index.

    Raises ``invalid_selection`` when the offset is out of bounds or would split a surrogate pair
    (a position the browser itself cannot place a caret at).
    """
    if offset < 0:
        raise CanvasError(CanvasErrorCode.INVALID_SELECTION, "negative offset", context={"offset": offset})
    units = 0
    if offset == 0:
        return 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units == offset:
            return index
        if units + char_units > offset:
            raise CanvasError(
                CanvasErrorCode.INVALID_SELECTION,
                "offset splits a surrogate pair",
                context={"offset": offset, "offset_unit": "utf16"},
            )
        units += char_units
    if units == offset:
        return len(text)
    raise CanvasError(
        CanvasErrorCode.INVALID_SELECTION,
        "offset beyond end of content",
        context={"offset": offset, "content_units": units, "offset_unit": "utf16"},
    )


def codepoint_to_utf16_index(text: str, index: int) -> int:
    if index < 0:
        raise CanvasError(CanvasErrorCode.INVALID_SELECTION, "negative offset", context={"offset": index})
    if index > len(text):
        raise CanvasError(
            CanvasErrorCode.INVALID_SELECTION,
            "offset beyond end of content",
            context={"offset": index, "content_codepoints": len(text), "offset_unit": "codepoint"},
        )
    return utf16_length(text[:index])


def to_codepoint_range(text: str, selection: Selection) -> tuple[int, int]:
    """Validate a Selection against ``text`` and return Python code-point bounds."""
    if selection.offset_unit == "codepoint":
        start, end = selection.start, selection.end
        length = len(text)
        if start > length or end > length:
            raise CanvasError(
                CanvasErrorCode.INVALID_SELECTION,
                "range beyond end of content",
                context={"start": start, "end": end, "content_codepoints": length},
            )
    elif selection.offset_unit == "utf16":
        start = utf16_to_codepoint_index(text, selection.start)
        end = utf16_to_codepoint_index(text, selection.end)
    else:  # pragma: no cover - Literal already blocks this
        raise CanvasError(
            CanvasErrorCode.INVALID_SELECTION,
            "unsupported offset unit",
            context={"offset_unit": selection.offset_unit},
        )
    if start > end:
        raise CanvasError(
            CanvasErrorCode.INVALID_SELECTION,
            "range start is after range end",
            context={"start": selection.start, "end": selection.end},
        )
    return start, end


def to_client_offset(text: str, index: int, unit: OffsetUnit = DEFAULT_OFFSET_UNIT) -> int:
    """Convert a Python index back to the client's unit."""
    if unit == "codepoint":
        return index
    return codepoint_to_utf16_index(text, index)


def normalize_selection(selection: Optional[Selection], offset_unit: Optional[OffsetUnit] = None) -> Optional[Selection]:
    """Apply a request-level default unit when the selection did not declare one."""
    if selection is None:
        return None
    if offset_unit and selection.offset_unit == DEFAULT_OFFSET_UNIT and offset_unit != DEFAULT_OFFSET_UNIT:
        return Selection(start=selection.start, end=selection.end, offset_unit=offset_unit)
    return selection
