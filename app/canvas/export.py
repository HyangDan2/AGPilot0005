"""Export providers (WO-C13).

Formats in R2: `.md`, `.txt`, `.html`, `.json` and the language-specific extension for source code.
Office formats are declared in the provider contract (:data:`OFFICE_EXPORT_CONTRACT`) and refused
with an explicit error — no DOCX/PDF/XLSX/PPTX engine is duplicated here.

Hardening:
* the download filename is built only from the artifact title plus a registry extension; the title
  is normalised, path separators / control characters / quotes are removed and ``..`` cannot survive,
* ``Content-Disposition`` never contains CR/LF (header-injection block is asserted, not assumed),
* a non-ASCII filename is offered through RFC 5987 ``filename*`` with an ASCII fallback.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Optional, Protocol, Tuple
from urllib.parse import quote

from app.canvas.errors import CanvasError, CanvasErrorCode
from app.canvas.security import sanitize_html_for_preview
from app.canvas.types import ArtifactTypeRegistry

FORMAT_MEDIA_TYPES: Dict[str, str] = {
    "md": "text/markdown; charset=utf-8",
    "txt": "text/plain; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "source": "text/plain; charset=utf-8",
}

OFFICE_EXPORT_CONTRACT: Dict[str, Dict[str, str]] = {
    "docx": {
        "media_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "provider": "office_export_provider",
        "status": "not_implemented_r2",
    },
    "pdf": {"media_type": "application/pdf", "provider": "office_export_provider", "status": "not_implemented_r2"},
    "xlsx": {
        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "provider": "office_export_provider",
        "status": "not_implemented_r2",
    },
    "pptx": {
        "media_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "provider": "office_export_provider",
        "status": "not_implemented_r2",
    },
}

_UNSAFE_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
_ASCII_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_HEADER_FORBIDDEN = re.compile(r"[\r\n\x00]")


@dataclass
class ExportResult:
    filename: str
    media_type: str
    content: bytes
    format: str
    extension: str
    artifact_id: str
    revision: int
    headers: Dict[str, str] = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def content_disposition(self) -> str:
        return build_content_disposition(self.filename)


def safe_filename(title: str, extension: str, *, max_stem: int = 80) -> str:
    """Build a filesystem- and header-safe name from a user title and a registry extension."""
    ext = extension if extension.startswith(".") else f".{extension}"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", ext):
        ext = ".txt"
    normalized = unicodedata.normalize("NFC", title or "")
    normalized = _UNSAFE_FILENAME_CHARS.sub("-", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    # ".." cannot survive: dots are legal but a path traversal needs a separator, which is removed
    normalized = normalized.replace("..", "-").strip(" .-")
    if not normalized:
        normalized = "artifact"
    stem = normalized[:max_stem].strip(" .-") or "artifact"
    return f"{stem}{ext}"


def _ascii_fallback(filename: str) -> str:
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        stem, ext = filename, ""
    ascii_stem = _ASCII_UNSAFE.sub("_", unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii"))
    ascii_stem = ascii_stem.strip("._-") or "artifact"
    return f"{ascii_stem}.{ext}" if ext else ascii_stem


def build_content_disposition(filename: str) -> str:
    if _HEADER_FORBIDDEN.search(filename):
        raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "refusing to build an unsafe header value")
    fallback = _ascii_fallback(filename)
    encoded = quote(filename, safe="")
    value = f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
    if _HEADER_FORBIDDEN.search(value):  # pragma: no cover - defence in depth
        raise CanvasError(CanvasErrorCode.STORAGE_FAILURE, "refusing to build an unsafe header value")
    return value


class ExportProvider(Protocol):
    def default_format(self, artifact_type: str) -> str: ...

    def export(self, *, artifact: Any, revision: Any, content: str, format: Optional[str]) -> ExportResult: ...


class TextExportProvider:
    """Markdown / text / HTML / JSON / source exporter (no external conversion engine)."""

    def __init__(self, types: ArtifactTypeRegistry) -> None:
        self.types = types

    def default_format(self, artifact_type: str) -> str:
        spec = self.types.get(artifact_type)
        return spec.export_formats[0]

    def export(self, *, artifact: Any, revision: Any, content: str, format: Optional[str]) -> ExportResult:
        spec = self.types.get(artifact.artifact_type)
        requested = (format or "").strip().lower() or spec.export_formats[0]
        if requested in OFFICE_EXPORT_CONTRACT:
            raise CanvasError(
                CanvasErrorCode.EXPORT_FORMAT_UNSUPPORTED,
                "office export is not implemented in R2",
                context={
                    "format": requested,
                    "contract": OFFICE_EXPORT_CONTRACT[requested],
                    "supported_formats": list(spec.export_formats),
                },
            )
        if requested not in spec.export_formats:
            raise CanvasError(
                CanvasErrorCode.EXPORT_FORMAT_UNSUPPORTED,
                f"format '{requested}' is not exported for artifact type '{spec.key}'",
                context={"format": requested, "supported_formats": list(spec.export_formats)},
            )

        warnings: list = []
        if requested == "source":
            extension = self.types.extension_for(artifact.artifact_type, artifact.language)
            media_type = FORMAT_MEDIA_TYPES["source"]
            body = content
        elif requested == "md":
            extension, media_type, body = ".md", FORMAT_MEDIA_TYPES["md"], content
        elif requested == "txt":
            extension, media_type, body = ".txt", FORMAT_MEDIA_TYPES["txt"], content
        elif requested == "json":
            extension, media_type, body = ".json", FORMAT_MEDIA_TYPES["json"], content
        elif requested == "html":
            if artifact.artifact_type == "html":
                sanitized = sanitize_html_for_preview(content)
                body = self._html_document(sanitized["html"], artifact.title)
                if sanitized["removed_tags"] or sanitized["blocked_attributes"]:
                    warnings.append(
                        {
                            "code": "html_sanitized",
                            "removed_tags": sanitized["removed_tags"],
                            "blocked_attributes": sanitized["blocked_attributes"],
                        }
                    )
            else:
                # A safe, escaped rendering of the text — not a Markdown renderer.
                body = self._html_document(
                    "<pre>" + _escape(content) + "</pre>", artifact.title
                )
                warnings.append(
                    {"code": "html_export_escaped", "detail": "content is escaped inside <pre>; no Markdown rendering"}
                )
            extension, media_type = ".html", FORMAT_MEDIA_TYPES["html"]
        else:  # pragma: no cover - guarded by spec.export_formats
            raise CanvasError(
                CanvasErrorCode.EXPORT_FORMAT_UNSUPPORTED,
                context={"format": requested},
            )

        filename = safe_filename(artifact.title, extension)
        payload = body.encode("utf-8")
        return ExportResult(
            filename=filename,
            media_type=media_type,
            content=payload,
            format=requested,
            extension=extension,
            artifact_id=artifact.artifact_id,
            revision=revision.revision,
            warnings=warnings,
        )

    @staticmethod
    def _html_document(body: str, title: str) -> str:
        return (
            "<!doctype html>\n<html lang=\"ko\"><head><meta charset=\"utf-8\">"
            f"<title>{_escape(title)}</title>"
            "<style>body{font-family:system-ui,-apple-system,'Segoe UI',sans-serif;line-height:1.6;"
            "margin:24px;color:#111}pre{white-space:pre-wrap;word-break:break-word}</style>"
            "</head><body>" + body + "</body></html>"
        )


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


class CompositeExportProvider:
    """Dispatches to the text provider and documents the office provider contract."""

    def __init__(self, types: ArtifactTypeRegistry) -> None:
        self.types = types
        self._text = TextExportProvider(types)

    def default_format(self, artifact_type: str) -> str:
        return self._text.default_format(artifact_type)

    def export(self, *, artifact: Any, revision: Any, content: str, format: Optional[str]) -> ExportResult:
        return self._text.export(artifact=artifact, revision=revision, content=content, format=format)

    def supported_formats(self, artifact_type: str) -> Tuple[str, ...]:
        spec = self.types.get(artifact_type)
        return tuple(spec.export_formats) + tuple(OFFICE_EXPORT_CONTRACT)

    def describe(self) -> Dict[str, Any]:
        return {
            "formats": dict(FORMAT_MEDIA_TYPES),
            "office_contract": OFFICE_EXPORT_CONTRACT,
            "defaults": {key: self.types.get(key).export_formats[0] for key in self.types.keys()},
        }


def provider_for(types: ArtifactTypeRegistry) -> CompositeExportProvider:
    return CompositeExportProvider(types)


def iter_formats(provider: Iterable[str]) -> Dict[str, str]:
    return {fmt: FORMAT_MEDIA_TYPES.get(fmt, "application/octet-stream") for fmt in provider}
