"""Extensible Artifact Type Registry (WO-C02).

Five artifact types ship enabled (markdown, plain text, source code, HTML source, structured
JSON). Chart / table / spreadsheet / office document are declared as *reserved* entries so the
frontend and tool schemas can already negotiate them, while the engine refuses to store a type it
has no editor/preview/export contract for.

Adding a type later is one ``register`` call plus, if needed, an export provider — no schema
migration, because ``artifact_type`` is a textual key validated against this registry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

from app.canvas.errors import CanvasError, CanvasErrorCode

_TYPE_KEY = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_LANGUAGE_KEY = re.compile(r"^[A-Za-z0-9+#._-]{1,32}$")

#: extension used when a source-code artifact has no language (or an unknown one)
DEFAULT_SOURCE_EXTENSION = ".txt"

#: language → file extension for source-code export
SOURCE_EXTENSIONS: Dict[str, str] = {
    "python": ".py",
    "javascript": ".js",
    "typescript": ".ts",
    "jsx": ".jsx",
    "tsx": ".tsx",
    "java": ".java",
    "kotlin": ".kt",
    "go": ".go",
    "rust": ".rs",
    "c": ".c",
    "cpp": ".cpp",
    "csharp": ".cs",
    "swift": ".swift",
    "php": ".php",
    "ruby": ".rb",
    "scala": ".scala",
    "dart": ".dart",
    "r": ".r",
    "sql": ".sql",
    "shell": ".sh",
    "bash": ".sh",
    "powershell": ".ps1",
    "yaml": ".yml",
    "toml": ".toml",
    "html": ".html",
    "css": ".css",
    "scss": ".scss",
    "xml": ".xml",
    "markdown": ".md",
    "text": ".txt",
}


@dataclass(frozen=True)
class ArtifactTypeSpec:
    """Behaviour contract for one artifact type."""

    key: str
    label: str
    media_type: str
    default_extension: str
    export_formats: Tuple[str, ...]
    preview_family: str = "text"  # text | code | html | json
    html_preview: bool = False
    enabled: bool = True
    language_required: bool = False
    metadata: Dict[str, str] = field(default_factory=dict)

    def to_public(self) -> Dict[str, object]:
        return {
            "artifact_type": self.key,
            "label": self.label,
            "media_type": self.media_type,
            "default_extension": self.default_extension,
            "export_formats": list(self.export_formats),
            "preview_family": self.preview_family,
            "html_preview": self.html_preview,
            "enabled": self.enabled,
            "language_required": self.language_required,
            "metadata": dict(self.metadata),
        }


BUILTIN_TYPES: Tuple[ArtifactTypeSpec, ...] = (
    ArtifactTypeSpec(
        key="markdown",
        label="Markdown Document",
        media_type="text/markdown",
        default_extension=".md",
        export_formats=("md", "txt", "html"),
        preview_family="text",
    ),
    ArtifactTypeSpec(
        key="plain_text",
        label="Plain Text",
        media_type="text/plain",
        default_extension=".txt",
        export_formats=("txt",),
        preview_family="text",
    ),
    ArtifactTypeSpec(
        key="source_code",
        label="Source Code",
        media_type="text/plain",
        default_extension=DEFAULT_SOURCE_EXTENSION,
        export_formats=("source", "txt"),
        preview_family="code",
        language_required=True,
    ),
    ArtifactTypeSpec(
        key="html",
        label="HTML Source",
        media_type="text/html",
        default_extension=".html",
        export_formats=("html", "txt"),
        preview_family="html",
        html_preview=True,
    ),
    ArtifactTypeSpec(
        key="structured_json",
        label="Structured JSON",
        media_type="application/json",
        default_extension=".json",
        export_formats=("json", "txt"),
        preview_family="json",
    ),
)

#: Declared for forward compatibility only; ``get`` refuses them until enabled.
RESERVED_TYPES: Tuple[ArtifactTypeSpec, ...] = (
    ArtifactTypeSpec("chart", "Chart", "application/json", ".json", ("json",), "json", enabled=False),
    ArtifactTypeSpec("table", "Table", "application/json", ".json", ("json",), "json", enabled=False),
    ArtifactTypeSpec(
        "spreadsheet", "Spreadsheet", "application/json", ".json", ("json",), "json", enabled=False,
        metadata={"planned_export": "xlsx"},
    ),
    ArtifactTypeSpec(
        "office_document", "Office Document",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx", ("docx",), "text", enabled=False, metadata={"planned_export": "docx,pdf"},
    ),
)


class ArtifactTypeRegistry:
    """Type registry with additive extension points."""

    def __init__(self, specs: Iterable[ArtifactTypeSpec] = BUILTIN_TYPES) -> None:
        self._specs: Dict[str, ArtifactTypeSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ArtifactTypeSpec) -> ArtifactTypeSpec:
        if not _TYPE_KEY.match(spec.key):
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                "artifact type key must match ^[a-z][a-z0-9_]{1,39}$",
                context={"artifact_type": spec.key},
            )
        self._specs[spec.key] = spec
        return spec

    def enable(self, key: str) -> ArtifactTypeSpec:
        """Enable a declared type (e.g. after its editor/export provider ships)."""
        spec = self._specs.get(key)
        if spec is None:
            raise CanvasError(CanvasErrorCode.UNSUPPORTED_ARTIFACT_TYPE, context={"artifact_type": key})
        enabled = ArtifactTypeSpec(
            key=spec.key, label=spec.label, media_type=spec.media_type,
            default_extension=spec.default_extension, export_formats=spec.export_formats,
            preview_family=spec.preview_family, html_preview=spec.html_preview,
            enabled=True, language_required=spec.language_required, metadata=spec.metadata,
        )
        self._specs[key] = enabled
        return enabled

    def get(self, key: Optional[str]) -> ArtifactTypeSpec:
        spec = self._specs.get((key or "").strip())
        if spec is None or not spec.enabled:
            raise CanvasError(
                CanvasErrorCode.UNSUPPORTED_ARTIFACT_TYPE,
                f"artifact type '{key}' is not enabled",
                context={"artifact_type": key, "enabled_types": self.keys()},
            )
        return spec

    def keys(self) -> List[str]:
        return sorted(k for k, s in self._specs.items() if s.enabled)

    def describe(self) -> List[Dict[str, object]]:
        """Public catalogue including reserved (disabled) entries."""
        return [self._specs[k].to_public() for k in sorted(self._specs)]

    def normalize_language(self, artifact_type: str, language: Optional[str]) -> Optional[str]:
        spec = self.get(artifact_type)
        cleaned = (language or "").strip()
        if cleaned:
            if not _LANGUAGE_KEY.match(cleaned):
                raise CanvasError(
                    CanvasErrorCode.INVALID_REQUEST,
                    "language must match ^[A-Za-z0-9+#._-]{1,32}$",
                    context={"language": language},
                )
            cleaned = cleaned.lower()
            return cleaned
        if spec.language_required:
            raise CanvasError(
                CanvasErrorCode.INVALID_REQUEST,
                f"artifact type '{artifact_type}' requires a language",
                context={"artifact_type": artifact_type},
            )
        return None

    def extension_for(self, artifact_type: str, language: Optional[str] = None) -> str:
        spec = self.get(artifact_type)
        if spec.key == "source_code":
            return SOURCE_EXTENSIONS.get((language or "").lower(), DEFAULT_SOURCE_EXTENSION)
        return spec.default_extension


default_type_registry = ArtifactTypeRegistry(list(BUILTIN_TYPES) + list(RESERVED_TYPES))
