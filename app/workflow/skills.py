"""WP-03 Skills Engine.

Skills are packaged instructions (``SKILL.md``: YAML front-matter + Markdown body) that
teach the planner how to perform a multi-step job with a bounded set of capabilities.

Key properties
--------------
* **On-demand discovery / lazy loading** — :meth:`SkillsEngine.discover_skills` returns
  summaries only (name, version, description, required capabilities, reference count).
  The instructions and reference documents are materialised by
  :meth:`SkillsEngine.load_skill` and cached; a request never carries the whole library.
* **No privilege escalation** — :meth:`SkillsEngine.authorize_execution` grants only the
  capabilities listed in the manifest, and only if the caller already holds all of them.
  :meth:`SkillsEngine.enforce_tool_call` then rejects any tool outside that grant.
* **Bundled skills** — six built-ins registered at construction so Scenario A can run
  without a skill marketplace.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "BUILTIN_SKILL_NAMES",
    "SkillScope",
    "SkillManifest",
    "SkillSummary",
    "SkillPackage",
    "LoadedSkill",
    "SkillExecutionGrant",
    "SkillError",
    "SkillNotFound",
    "SkillPrivilegeError",
    "SkillFormatError",
    "parse_skill_markdown",
    "SkillsEngine",
]

SkillScope = Literal["global", "product", "workspace", "session"]

BUILTIN_SKILL_NAMES: Tuple[str, ...] = (
    "document-summary",
    "document-comparison",
    "spreadsheet-analysis",
    "presentation-generation",
    "report-generation",
    "workspace-research",
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{1,63}$")
_VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")


class SkillError(RuntimeError):
    pass


class SkillNotFound(SkillError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"skill_not_found: {name}")


class SkillPrivilegeError(SkillError, PermissionError):
    def __init__(self, code: str, *, skill: str = "", denied: Iterable[str] = ()) -> None:
        self.code = code
        self.skill = skill
        self.denied = tuple(denied)
        detail = f" skill={skill}" if skill else ""
        denied_text = f" denied={','.join(self.denied)}" if self.denied else ""
        super().__init__(f"{code}{detail}{denied_text}")


class SkillFormatError(SkillError):
    def __init__(self, reason: str, source: str = "") -> None:
        self.reason = reason
        self.source = source
        super().__init__(f"skill_format_error: {reason}" + (f" ({source})" if source else ""))


class SkillManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    description: str
    input_schema: Dict[str, Any] = Field(default_factory=dict)
    output_schema: Dict[str, Any] = Field(default_factory=dict)
    required_capabilities: Tuple[str, ...] = ()
    references: Tuple[str, ...] = ()
    scope: SkillScope = "global"
    tags: Tuple[str, ...] = ()
    source: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not _NAME_RE.match(value or ""):
            raise ValueError("invalid_skill_name")
        return value

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        if not _VERSION_RE.match(value or ""):
            raise ValueError("invalid_skill_version")
        return value

    @field_validator("required_capabilities", "references", "tags", mode="before")
    @classmethod
    def _tuple(cls, value: Any) -> Tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return tuple(str(item) for item in value)


class SkillSummary(BaseModel):
    """Discovery payload — deliberately contains no instruction or reference text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str
    description: str
    scope: SkillScope
    required_capabilities: Tuple[str, ...] = ()
    reference_count: int = 0
    tags: Tuple[str, ...] = ()
    checksum: str
    loaded: bool = False

    def prompt_line(self) -> str:
        caps = ", ".join(self.required_capabilities) or "none"
        return f"- {self.name}@{self.version}: {self.description} (capabilities: {caps})"


class LoadedSkill(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest: SkillManifest
    instructions: str
    references: Dict[str, str] = Field(default_factory=dict)
    checksum: str
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def name(self) -> str:
        return self.manifest.name


class SkillExecutionGrant(BaseModel):
    """Capabilities a skill run may use — never wider than the caller's own set."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill: str
    allowed_capabilities: Tuple[str, ...]

    def allows(self, capability: str) -> bool:
        return capability in self.allowed_capabilities


def _checksum(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------------------
# SKILL.md parsing
# --------------------------------------------------------------------------------------
def _simple_yaml(text: str) -> Dict[str, Any]:
    """Minimal front-matter parser used when PyYAML is unavailable.

    Supports ``key: value`` scalars and ``- item`` lists one level deep.
    """
    data: Dict[str, Any] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        if line.lstrip().startswith("- ") and current:
            data.setdefault(current, [])
            if isinstance(data[current], list):
                data[current].append(line.lstrip()[2:].strip().strip("'\""))
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            current = key
            if not value:
                data[key] = []
            else:
                if value.startswith("[") and value.endswith("]"):
                    data[key] = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
                elif value.lower() in ("true", "false"):
                    data[key] = value.lower() == "true"
                else:
                    data[key] = value.strip("'\"")
    return data


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---"):
        raise SkillFormatError("missing_frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise SkillFormatError("unterminated_frontmatter")
    front, body = parts[1], parts[2]
    try:
        import yaml  # type: ignore

        meta = yaml.safe_load(front) or {}
    except Exception:  # noqa: BLE001 - fall back to the minimal parser
        meta = _simple_yaml(front)
    if not isinstance(meta, Mapping):
        raise SkillFormatError("frontmatter_not_a_mapping")
    return dict(meta), body.lstrip("\n")


def _manifest_from_meta(meta: Mapping[str, Any], *, source: Optional[str]) -> SkillManifest:
    missing = [field for field in ("name", "version", "description") if not meta.get(field)]
    if missing:
        raise SkillFormatError(f"missing_fields: {','.join(missing)}", source or "")
    references = meta.get("references") or ()
    if isinstance(references, str):
        references = (references,)
    return SkillManifest(
        name=str(meta["name"]).strip(),
        version=str(meta["version"]).strip(),
        description=str(meta["description"]).strip(),
        input_schema=dict(meta.get("input_schema") or {}),
        output_schema=dict(meta.get("output_schema") or {}),
        required_capabilities=tuple(str(item) for item in (meta.get("required_capabilities") or ())),
        references=tuple(str(item) for item in references),
        scope=str(meta.get("scope") or "global").strip().lower(),
        tags=tuple(str(item) for item in (meta.get("tags") or ())),
        source=source,
    )


class SkillPackage:
    """In-memory skill source: manifest + instructions + reference loader."""

    def __init__(
        self,
        manifest: SkillManifest,
        instructions: str,
        *,
        reference_loader: Optional[Callable[[str], str]] = None,
        base_dir: Optional[Path] = None,
    ) -> None:
        self.manifest = manifest
        self._instructions = instructions
        self._reference_loader = reference_loader
        self.base_dir = base_dir
        self.checksum = _checksum(f"{manifest.model_dump_json()}\n{instructions}")

    def summary(self, *, loaded: bool = False) -> SkillSummary:
        return SkillSummary(
            name=self.manifest.name,
            version=self.manifest.version,
            description=self.manifest.description,
            scope=self.manifest.scope,
            required_capabilities=self.manifest.required_capabilities,
            reference_count=len(self.manifest.references),
            tags=self.manifest.tags,
            checksum=self.checksum,
            loaded=loaded,
        )

    def load_references(self) -> Dict[str, str]:
        loaded: Dict[str, str] = {}
        for ref in self.manifest.references:
            if self._reference_loader is None:
                loaded[ref] = ""
                continue
            loaded[ref] = self._reference_loader(ref)
        return loaded

    def materialise(self) -> LoadedSkill:
        return LoadedSkill(
            manifest=self.manifest,
            instructions=self._instructions,
            references=self.load_references(),
            checksum=self.checksum,
        )


def parse_skill_markdown(text: str, *, source: Optional[str] = None) -> SkillPackage:
    meta, body = _parse_frontmatter(text)
    manifest = _manifest_from_meta(meta, source=source)
    if not body.strip():
        raise SkillFormatError("empty_skill_body", source or "")
    return SkillPackage(manifest, body)


def _read_reference(base: Path, ref: str) -> str:
    candidate = (base / ref).resolve()
    if base.resolve() not in candidate.parents and candidate != base.resolve():
        raise SkillFormatError(f"reference_escapes_skill_directory: {ref}")
    if not candidate.is_file():
        raise SkillFormatError(f"missing_reference: {ref}")
    return candidate.read_text(encoding="utf-8")


# --------------------------------------------------------------------------------------
# Built-in skills
# --------------------------------------------------------------------------------------
_BUILTIN_DEFINITIONS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "document-summary",
        "version": "1.0.0",
        "description": "Summarise a document into key claims, evidence and open questions.",
        "required_capabilities": ("document.get_outline", "document.read_section"),
        "tags": ("document", "summary"),
        "instructions": (
            "# Document Summary\n\n"
            "1. Read the outline before any section body; never summarise from page one only.\n"
            "2. Select the sections that carry the argument (skip annexes unless cited).\n"
            "3. Extract: thesis, 3-5 supporting claims with their evidence, and open questions.\n"
            "4. Produce a five-sentence executive summary followed by a bullet list of claims.\n"
            "5. Cite section ids for every claim; do not invent facts missing from the document.\n"
            "6. If a section is empty or unreadable, say so instead of filling the gap.\n"
        ),
        "reference": ("citation-style.md", "# Citation style\n- Use `[section:<id>]` after each claim.\n- Quote at most 25 words per citation.\n"),
    },
    {
        "name": "document-comparison",
        "version": "1.0.0",
        "description": "Compare two documents and report added, removed and conflicting content.",
        "required_capabilities": ("office.docx.compare",),
        "tags": ("document", "comparison"),
        "instructions": (
            "# Document Comparison\n\n"
            "1. Run a structural comparison first (headings), then a paragraph comparison.\n"
            "2. Classify each difference: added, removed, reworded, contradicts.\n"
            "3. Report contradictions separately — they need a human decision, not a merge.\n"
            "4. Never silently pick a winner between contradictory statements.\n"
        ),
        "reference": ("diff-taxonomy.md", "# Diff taxonomy\n- added / removed / reworded / contradicts\n"),
    },
    {
        "name": "spreadsheet-analysis",
        "version": "1.0.0",
        "description": "Analyse a spreadsheet: shape, column statistics and anomalies.",
        "required_capabilities": ("office.xlsx.analyze",),
        "tags": ("spreadsheet", "analysis"),
        "instructions": (
            "# Spreadsheet Analysis\n\n"
            "1. Report sheet shape (rows x columns) and column types before any statistic.\n"
            "2. Compute only the requested operations; state if a column is non-numeric.\n"
            "3. Flag anomalies (blank ratio, out-of-range values) as observations, not conclusions.\n"
            "4. Put the numbers in a table; never round without saying so.\n"
        ),
        "reference": ("stats-checklist.md", "# Checklist\n- row_count\n- numeric columns\n- blank ratio\n"),
    },
    {
        "name": "presentation-generation",
        "version": "1.0.0",
        "description": "Build a presentation deck from a written brief.",
        "required_capabilities": ("office.pptx.generate", "workspace.file.write"),
        "tags": ("presentation", "generation"),
        "instructions": (
            "# Presentation Generation\n\n"
            "1. One idea per slide; the title states the conclusion, not the topic.\n"
            "2. Limit each slide to five bullets and one supporting visual description.\n"
            "3. Keep a fixed narrative: context → problem → approach → evidence → ask.\n"
            "4. Save the artifact through the workspace writer and return its artifact id.\n"
        ),
        "reference": ("slide-budget.md", "# Slide budget\n- 10-14 slides for a 20 minute talk\n"),
    },
    {
        "name": "report-generation",
        "version": "1.0.0",
        "description": "Assemble a sourced report from workspace research.",
        "required_capabilities": ("workspace.rag.search", "workspace.file.write"),
        "tags": ("report", "generation"),
        "instructions": (
            "# Report Generation\n\n"
            "1. Search the workspace and keep a source list before writing prose.\n"
            "2. Every factual sentence carries a source path.\n"
            "3. Separate findings, interpretation and recommendation headings.\n"
            "4. If sources disagree, present both and mark the uncertainty.\n"
            "5. Write the finished report into the workspace and return the path.\n"
        ),
        "reference": ("report-outline.md", "# Outline\n1. Summary\n2. Findings\n3. Interpretation\n4. Recommendation\n"),
    },
    {
        "name": "workspace-research",
        "version": "1.0.0",
        "description": "Answer a question from workspace documents with citations.",
        "required_capabilities": ("workspace.rag.search",),
        "tags": ("research", "rag"),
        "instructions": (
            "# Workspace Research\n\n"
            "1. Translate the question into 2-4 search queries; run them separately.\n"
            "2. Prefer primary documents over summaries; note the path of every hit used.\n"
            "3. Answer in one paragraph, then list evidence with scores.\n"
            "4. If the workspace has no supporting hit, say \"not found in workspace\".\n"
        ),
        "reference": ("query-recipes.md", "# Query recipes\n- entity + action\n- synonym expansion\n"),
    },
)


def builtin_packages() -> List[SkillPackage]:
    packages: List[SkillPackage] = []
    for definition in _BUILTIN_DEFINITIONS:
        manifest = SkillManifest(
            name=definition["name"],
            version=definition["version"],
            description=definition["description"],
            input_schema={"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]},
            output_schema={"type": "object"},
            required_capabilities=definition["required_capabilities"],
            references=(definition["reference"][0],),
            scope="global",
            tags=definition["tags"],
            source="builtin",
        )
        ref_name, ref_body = definition["reference"]
        packages.append(SkillPackage(
            manifest,
            definition["instructions"],
            reference_loader=lambda _ref, body=ref_body: body,
        ))
    return packages


# --------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------
class SkillsEngine:
    """Registry + lazy loader for skills."""

    def __init__(self, *, register_builtins: bool = True) -> None:
        self._packages: Dict[Tuple[str, str], SkillPackage] = {}
        self._latest: Dict[str, Tuple[str, str]] = {}
        self._loaded: Dict[Tuple[str, str], LoadedSkill] = {}
        self.discovery_calls = 0
        self.loads = 0
        self.reference_reads: List[str] = []
        if register_builtins:
            for package in builtin_packages():
                self.register(package)

    # -- registration ------------------------------------------------------------------
    def register(self, package: SkillPackage) -> SkillSummary:
        if not isinstance(package, SkillPackage):
            raise SkillError("package_must_be_SkillPackage")
        key = (package.manifest.name, package.manifest.version)
        if key in self._packages:
            raise SkillError(f"duplicate_skill: {package.manifest.name}@{package.manifest.version}")
        self._packages[key] = package
        current = self._latest.get(package.manifest.name)
        if current is None or _version_key(package.manifest.version) >= _version_key(current[1]):
            self._latest[package.manifest.name] = key
        return package.summary(loaded=key in self._loaded)

    def register_skill_file(self, path: str | Path, *, scope: Optional[SkillScope] = None) -> SkillSummary:
        file_path = Path(path)
        if file_path.is_dir():
            file_path = file_path / "SKILL.md"
        if not file_path.is_file():
            raise SkillNotFound(str(path))
        text = file_path.read_text(encoding="utf-8")
        package = parse_skill_markdown(text, source=str(file_path))
        if scope is not None and package.manifest.scope != scope:
            package.manifest = package.manifest.model_copy(update={"scope": scope})
        base = file_path.parent
        package.base_dir = base
        package._reference_loader = lambda ref: self._read_reference(base, ref)
        return self.register(package)

    def _read_reference(self, base: Path, ref: str) -> str:
        self.reference_reads.append(ref)
        return _read_reference(base, ref)

    # -- discovery ---------------------------------------------------------------------
    def discover_skills(
        self,
        *,
        scope: Optional[SkillScope] = None,
        capability: Optional[str] = None,
        caller_capabilities: Optional[Iterable[str]] = None,
        query: Optional[str] = None,
    ) -> List[SkillSummary]:
        """Return summaries only — instructions and references stay untouched."""
        self.discovery_calls += 1
        held = set(caller_capabilities) if caller_capabilities is not None else None
        needle = (query or "").strip().lower()
        summaries: List[SkillSummary] = []
        for name, key in sorted(self._latest.items()):
            package = self._packages[key]
            manifest = package.manifest
            if scope is not None and manifest.scope != scope:
                continue
            if capability is not None and capability not in manifest.required_capabilities:
                continue
            if held is not None and not set(manifest.required_capabilities) <= held:
                continue
            if needle and needle not in name.lower() and needle not in manifest.description.lower():
                continue
            summaries.append(package.summary(loaded=key in self._loaded))
        return summaries

    def discovery_prompt(self, **kwargs: Any) -> str:
        return "\n".join(summary.prompt_line() for summary in self.discover_skills(**kwargs))

    def summary(self, name: str) -> SkillSummary:
        key = self._key(name)
        return self._packages[key].summary(loaded=key in self._loaded)

    # -- loading -----------------------------------------------------------------------
    def _key(self, name: str, version: Optional[str] = None) -> Tuple[str, str]:
        if version is not None:
            key = (name, version)
            if key not in self._packages:
                raise SkillNotFound(f"{name}@{version}")
            return key
        key = self._latest.get(name)
        if key is None:
            raise SkillNotFound(name)
        return key

    def is_loaded(self, name: str, version: Optional[str] = None) -> bool:
        return self._key(name, version) in self._loaded

    def load_skill(self, name: str, *, version: Optional[str] = None) -> LoadedSkill:
        """Materialise instructions + references for one skill (cached)."""
        key = self._key(name, version)
        cached = self._loaded.get(key)
        if cached is not None:
            return cached
        package = self._packages[key]
        before = len(self.reference_reads)
        loaded = package.materialise()
        self.loads += 1
        if len(self.reference_reads) == before:
            # Non-file packages expose their reference count through this counter too,
            # so tests can assert references are not read during discovery.
            self.reference_reads.extend(loaded.references.keys())
        self._loaded[key] = loaded
        return loaded

    def loaded_skill_count(self) -> int:
        return len(self._loaded)

    # -- privilege boundary ------------------------------------------------------------
    def authorize_execution(
        self,
        name: str,
        caller_capabilities: Iterable[str],
        *,
        version: Optional[str] = None,
    ) -> SkillExecutionGrant:
        """Grant exactly the manifest capabilities the caller already holds.

        Raises :class:`SkillPrivilegeError` when the skill needs a capability the caller
        does not have — a skill can never widen the caller's authority.
        """
        key = self._key(name, version)
        manifest = self._packages[key].manifest
        held = set(caller_capabilities)
        required = set(manifest.required_capabilities)
        missing = sorted(required - held)
        if missing:
            raise SkillPrivilegeError("insufficient_capabilities", skill=name, denied=missing)
        return SkillExecutionGrant(skill=name, allowed_capabilities=tuple(sorted(required)))

    @staticmethod
    def enforce_tool_call(grant: SkillExecutionGrant, capability: str) -> None:
        if not grant.allows(capability):
            raise SkillPrivilegeError("capability_not_in_skill_grant", skill=grant.skill, denied=[capability])


def _version_key(version: str) -> Tuple[int, ...]:
    parts = []
    for chunk in version.split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)
