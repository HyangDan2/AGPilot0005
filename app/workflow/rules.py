"""WP-04 Rules Engine.

Rules arrive from four layers — ``System > Product > Workspace > Session`` — and the
engine resolves them into one effective rule set while recording exactly what happened
to every rule (:class:`AppliedRuleRecord`) and every collision
(:class:`RuleConflict`).

Two behaviours coexist by design:

* **Ordinary categories** follow normal configuration layering: the more specific layer
  (session) may refine a product/workspace default, and the override is recorded.
* **Protected categories** (``auth``, ``security``, ``approval``, ``data_isolation``)
  cannot be weakened or neutralised by a lower layer.  A lower-layer attempt is refused,
  the upper-layer value stays in force, and a ``RuleConflict(blocked=True)`` is recorded
  so the attempt is auditable.

Workspace rules are loaded from ``<workspace>/.agcore/rules/`` (``*.json`` / ``*.md``)
or ``<workspace>/rules.json`` / ``<workspace>/rules.md``.
"""
from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "RuleLayer",
    "PROTECTED_CATEGORIES",
    "LAYER_AUTHORITY",
    "LAYER_SPECIFICITY",
    "RuleDefinition",
    "RuleConflict",
    "AppliedRuleRecord",
    "RulesResolution",
    "RulesEngine",
    "RuleLoadError",
    "parse_rule_document",
    "load_workspace_rules",
]


class RuleLayer(str, Enum):
    SYSTEM = "system"
    PRODUCT = "product"
    WORKSPACE = "workspace"
    SESSION = "session"


#: Authority — who wins when a conflict is not allowed to be resolved by specificity.
LAYER_AUTHORITY: Dict[RuleLayer, int] = {
    RuleLayer.SYSTEM: 4,
    RuleLayer.PRODUCT: 3,
    RuleLayer.WORKSPACE: 2,
    RuleLayer.SESSION: 1,
}

#: Specificity — who is closer to the current execution (used for ordinary overrides).
LAYER_SPECIFICITY: Dict[RuleLayer, int] = {
    RuleLayer.SYSTEM: 1,
    RuleLayer.PRODUCT: 2,
    RuleLayer.WORKSPACE: 3,
    RuleLayer.SESSION: 4,
}

#: Categories a lower layer may never weaken.
PROTECTED_CATEGORIES = frozenset({"auth", "security", "approval", "data_isolation"})

_MAX_LIMIT_HINTS = ("max", "limit", "budget", "cap", "token", "count", "timeout")
_MIN_HINTS = ("min", "required")
_SEVERITY_RANKS = {"required": 3, "mandatory": 3, "enforced": 3, "recommended": 2, "default": 2, "optional": 1, "off": 0}


class RuleLoadError(ValueError):
    """A rules document could not be parsed."""

    def __init__(self, path: str, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"rule_load_error: {path}: {reason}")


class RuleDefinition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    layer: RuleLayer
    key: str
    value: Any = None
    category: str = "general"
    priority: int = 0
    source: Optional[str] = None
    description: str = ""

    @field_validator("key")
    @classmethod
    def _key_required(cls, value: str) -> str:
        cleaned = (value or "").strip()
        if not cleaned:
            raise ValueError("rule_key_required")
        if len(cleaned) > 128:
            raise ValueError("rule_key_too_long")
        return cleaned

    @field_validator("category")
    @classmethod
    def _category(cls, value: str) -> str:
        return (value or "general").strip().lower()

    @property
    def is_protected(self) -> bool:
        return self.category in PROTECTED_CATEGORIES


class RuleConflict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    category: str
    winning_layer: RuleLayer
    losing_layer: RuleLayer
    winning_rule_id: str
    losing_rule_id: str
    blocked: bool
    reason: str


class AppliedRuleRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    rule_id: str
    layer: RuleLayer
    category: str
    value: Any = None
    decision: Literal["applied", "override", "blocked", "unchanged", "shadowed"]
    superseded_rule_id: Optional[str] = None
    reason: str = ""


class RulesResolution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    effective: Dict[str, Any] = Field(default_factory=dict)
    applied: List[AppliedRuleRecord] = Field(default_factory=list)
    conflicts: List[RuleConflict] = Field(default_factory=list)
    blocked: List[RuleConflict] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)

    def has_blocked_conflicts(self) -> bool:
        return bool(self.blocked)

    def explain(self, key: str) -> List[AppliedRuleRecord]:
        return [record for record in self.applied if record.key == key]


# --------------------------------------------------------------------------------------
# Parsing / loading
# --------------------------------------------------------------------------------------
_MD_RULE = re.compile(r"^\s*[-*]\s*([A-Za-z0-9_.\-]+)\s*:\s*(.+?)\s*$")
_MD_META = re.compile(r"(?:#\s*|\[)([A-Za-z_]+)\s*[=:]\s*([A-Za-z0-9_\-]+)\]?", re.I)


def _coerce_scalar(raw: str) -> Any:
    text = raw.strip().rstrip(",").strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _rule_from_mapping(payload: Mapping[str, Any], *, layer: RuleLayer, source: Optional[str], index: int) -> RuleDefinition:
    key = str(payload.get("key") or "").strip()
    if not key:
        raise RuleLoadError(source or "<memory>", f"rule[{index}] missing key")
    meta = payload.get("meta") if isinstance(payload.get("meta"), Mapping) else {}
    category = payload.get("category") or meta.get("category") or "general"
    priority = payload.get("priority", meta.get("priority", 0))
    return RuleDefinition(
        rule_id=str(payload.get("rule_id") or f"{layer.value}:{key}:{index}"),
        layer=RuleLayer(payload.get("layer") or layer),
        key=key,
        value=payload.get("value"),
        category=str(category),
        priority=int(priority or 0),
        source=source,
        description=str(payload.get("description") or ""),
    )


def parse_rule_document(
    text: str,
    *,
    layer: RuleLayer,
    source: Optional[str] = None,
    fmt: str = "json",
) -> List[RuleDefinition]:
    """Parse a JSON or Markdown rule document into definitions."""
    if fmt == "json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuleLoadError(source or "<memory>", f"invalid_json: {exc.msg}") from exc

        entries: Sequence[Any]
        default_category = "general"
        if isinstance(payload, Mapping):
            default_category = str(payload.get("category") or "general")
            entries = payload.get("rules") if isinstance(payload.get("rules"), list) else []
            if not entries:
                entries = [
                    {"key": key, "value": value}
                    for key, value in payload.items()
                    if key not in ("version", "category", "rules", "description")
                ]
        elif isinstance(payload, list):
            entries = payload
        else:
            raise RuleLoadError(source or "<memory>", "unsupported_json_shape")

        rules: List[RuleDefinition] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, Mapping):
                raise RuleLoadError(source or "<memory>", f"rule[{index}] is not an object")
            item = dict(entry)
            item.setdefault("category", default_category)
            rules.append(_rule_from_mapping(item, layer=layer, source=source, index=index))
        return rules

    if fmt in ("md", "markdown"):
        rules = []
        for index, raw_line in enumerate(text.splitlines()):
            line = raw_line.strip()
            if not line or line.startswith("#") and not _MD_META.search(line):
                continue
            match = _MD_RULE.match(raw_line)
            if not match:
                continue
            key, raw_value = match.group(1), match.group(2)
            meta: Dict[str, str] = {}
            for meta_key, meta_value in _MD_META.findall(raw_value):
                meta[meta_key.lower()] = meta_value
            value_text = _MD_META.sub("", raw_value).strip()
            rules.append(RuleDefinition(
                rule_id=f"{layer.value}:{key}:{index}",
                layer=layer,
                key=key,
                value=_coerce_scalar(value_text),
                category=meta.get("category", "general"),
                priority=int(meta.get("priority", 0) or 0),
                source=source,
                description=meta.get("description", ""),
            ))
        return rules

    raise RuleLoadError(source or "<memory>", f"unsupported_format: {fmt}")


def _candidate_rule_files(root: Path) -> List[Path]:
    files: List[Path] = []
    rules_dir = root / ".agcore" / "rules"
    if rules_dir.is_dir():
        files.extend(sorted(path for path in rules_dir.iterdir() if path.suffix.lower() in (".json", ".md")))
    for path in (root / ".agcore" / "rules.json", root / ".agcore" / "rules.md",
                 root / "rules.json", root / "rules.md"):
        if path.is_file() and path not in files:
            files.append(path)
    return files


def load_workspace_rules(root: str | Path, *, layer: RuleLayer = RuleLayer.WORKSPACE) -> List[RuleDefinition]:
    """Load every rule document found in a workspace root (missing root = no rules)."""
    base = Path(root)
    if not base.exists():
        raise RuleLoadError(str(base), "workspace_not_found")
    loaded: List[RuleDefinition] = []
    for path in _candidate_rule_files(base):
        text = path.read_text(encoding="utf-8")
        fmt = "json" if path.suffix.lower() == ".json" else "md"
        source = str(path.relative_to(base))
        loaded.extend(parse_rule_document(text, layer=layer, source=source, fmt=fmt))
    return loaded


# --------------------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------------------
def _as_set(value: Any) -> Optional[set]:
    if isinstance(value, (list, tuple, set, frozenset)):
        try:
            return {json.dumps(item, sort_keys=True) if isinstance(item, (dict, list)) else item for item in value}
        except TypeError:
            return None
    return None


def is_stricter(upper: Any, lower: Any, *, key: str = "") -> bool:
    """True when ``lower`` provably narrows the permission/limit expressed by ``upper``.

    Used only for protected categories: anything that is not provably stricter is
    treated as a weakening attempt.
    """
    if upper == lower:
        return True
    if isinstance(upper, bool) or isinstance(lower, bool):
        return isinstance(upper, bool) and isinstance(lower, bool) and (lower is True) and (upper is False)
    upper_set, lower_set = _as_set(upper), _as_set(lower)
    if upper_set is not None and lower_set is not None:
        return lower_set < upper_set
    if isinstance(upper, Mapping) and isinstance(lower, Mapping):
        changed = False
        for field, upper_value in upper.items():
            if field not in lower:
                return False
            lower_value = lower[field]
            if lower_value == upper_value:
                continue
            if not is_stricter(upper_value, lower_value, key=f"{key}.{field}"):
                return False
            changed = True
        return changed or set(lower) <= set(upper)
    if isinstance(upper, (int, float)) and isinstance(lower, (int, float)) and not isinstance(upper, bool):
        lowered = key.lower()
        if any(hint in lowered for hint in _MAX_LIMIT_HINTS):
            return lower < upper
        if any(hint in lowered for hint in _MIN_HINTS):
            return lower > upper
        return False
    if isinstance(upper, str) and isinstance(lower, str):
        upper_rank = _SEVERITY_RANKS.get(upper.lower())
        lower_rank = _SEVERITY_RANKS.get(lower.lower())
        if upper_rank is not None and lower_rank is not None:
            return lower_rank > upper_rank
    return False


class RulesEngine:
    """Resolves layered rules with a documented audit trail."""

    def __init__(
        self,
        rules: Optional[Iterable[RuleDefinition]] = None,
        *,
        workspace_root: Optional[str | Path] = None,
        auto_load_workspace: bool = True,
    ) -> None:
        self._rules: List[RuleDefinition] = list(rules or [])
        self.workspace_root = Path(workspace_root) if workspace_root else None
        self.auto_load_workspace = auto_load_workspace
        self._workspace_loaded = False
        self._workspace_sources: List[str] = []
        self.last_resolution: Optional[RulesResolution] = None

    # -- registration ------------------------------------------------------------------
    def register(self, rule: RuleDefinition) -> RuleDefinition:
        if any(existing.rule_id == rule.rule_id for existing in self._rules):
            raise ValueError(f"duplicate_rule_id: {rule.rule_id}")
        self._rules.append(rule)
        return rule

    def add(
        self,
        layer: RuleLayer | str,
        key: str,
        value: Any,
        *,
        category: str = "general",
        rule_id: Optional[str] = None,
        priority: int = 0,
        description: str = "",
    ) -> RuleDefinition:
        rule_layer = RuleLayer(layer)
        rule = RuleDefinition(
            rule_id=rule_id or f"{rule_layer.value}:{key}:{len(self._rules)}",
            layer=rule_layer,
            key=key,
            value=value,
            category=category,
            priority=priority,
            description=description,
        )
        return self.register(rule)

    def rules(self, layer: Optional[RuleLayer] = None) -> List[RuleDefinition]:
        if layer is None:
            return list(self._rules)
        return [rule for rule in self._rules if rule.layer == layer]

    # -- resolution --------------------------------------------------------------------
    def _collect(self, layers: Optional[Mapping[Any, Sequence[RuleDefinition]]]) -> Dict[RuleLayer, List[RuleDefinition]]:
        grouped: Dict[RuleLayer, List[RuleDefinition]] = {layer: [] for layer in RuleLayer}
        for rule in self._rules:
            grouped[rule.layer].append(rule)
        if layers:
            for raw_layer, rules in layers.items():
                layer = RuleLayer(raw_layer)
                grouped[layer].extend(rules)
        if self.auto_load_workspace and self.workspace_root is not None and not self._workspace_loaded:
            for rule in load_workspace_rules(self.workspace_root):
                grouped[RuleLayer.WORKSPACE].append(rule)
                if rule.source and rule.source not in self._workspace_sources:
                    self._workspace_sources.append(rule.source)
            self._workspace_loaded = True
        return grouped

    def resolve(
        self,
        layers: Optional[Mapping[Any, Sequence[RuleDefinition]]] = None,
        *,
        workspace_root: Optional[str | Path] = None,
    ) -> RulesResolution:
        if workspace_root is not None:
            self.workspace_root = Path(workspace_root)
            self._workspace_loaded = False
        grouped = self._collect(layers)

        effective: Dict[str, Any] = {}
        applied: List[AppliedRuleRecord] = []
        conflicts: List[RuleConflict] = []
        blocked: List[RuleConflict] = []
        sources: List[str] = list(self._workspace_sources)

        keys: List[str] = []
        for layer in RuleLayer:
            for rule in grouped[layer]:
                if rule.key not in keys:
                    keys.append(rule.key)
                if rule.source and rule.source not in sources:
                    sources.append(rule.source)

        # Highest authority first; inside a layer, higher explicit priority first.
        ordered_layers = sorted(RuleLayer, key=lambda layer: -LAYER_AUTHORITY[layer])
        for key in keys:
            candidates: List[RuleDefinition] = []
            for layer in ordered_layers:
                layer_rules = [rule for rule in grouped[layer] if rule.key == key]
                layer_rules.sort(key=lambda rule: (-rule.priority, rule.rule_id))
                candidates.extend(layer_rules)
            if not candidates:
                continue

            winner = candidates[0]
            effective[key] = winner.value
            applied.append(AppliedRuleRecord(
                key=key, rule_id=winner.rule_id, layer=winner.layer, category=winner.category,
                value=winner.value, decision="applied", reason="highest_authority",
            ))

            for candidate in candidates[1:]:
                if candidate.value == winner.value:
                    applied.append(AppliedRuleRecord(
                        key=key, rule_id=candidate.rule_id, layer=candidate.layer, category=candidate.category,
                        value=candidate.value, decision="unchanged", superseded_rule_id=winner.rule_id,
                        reason="identical_value",
                    ))
                    continue

                protected = winner.is_protected or candidate.is_protected
                if protected:
                    if is_stricter(winner.value, candidate.value, key=key):
                        applied.append(AppliedRuleRecord(
                            key=key, rule_id=candidate.rule_id, layer=candidate.layer, category=candidate.category,
                            value=candidate.value, decision="override", superseded_rule_id=winner.rule_id,
                            reason="stricter_protected_rule",
                        ))
                        winner = candidate
                        effective[key] = candidate.value
                    else:
                        conflict = RuleConflict(
                            key=key,
                            category=winner.category if winner.is_protected else candidate.category,
                            winning_layer=winner.layer,
                            losing_layer=candidate.layer,
                            winning_rule_id=winner.rule_id,
                            losing_rule_id=candidate.rule_id,
                            blocked=True,
                            reason="protected_rule_weakening_blocked",
                        )
                        conflicts.append(conflict)
                        blocked.append(conflict)
                        applied.append(AppliedRuleRecord(
                            key=key, rule_id=candidate.rule_id, layer=candidate.layer, category=candidate.category,
                            value=candidate.value, decision="blocked", superseded_rule_id=winner.rule_id,
                            reason="protected_rule_weakening_blocked",
                        ))
                    continue

                # Ordinary layering: the more specific layer refines the default.
                if LAYER_SPECIFICITY[candidate.layer] >= LAYER_SPECIFICITY[winner.layer]:
                    conflicts.append(RuleConflict(
                        key=key,
                        category=candidate.category,
                        winning_layer=candidate.layer,
                        losing_layer=winner.layer,
                        winning_rule_id=candidate.rule_id,
                        losing_rule_id=winner.rule_id,
                        blocked=False,
                        reason="specific_layer_override",
                    ))
                    applied.append(AppliedRuleRecord(
                        key=key, rule_id=candidate.rule_id, layer=candidate.layer, category=candidate.category,
                        value=candidate.value, decision="override", superseded_rule_id=winner.rule_id,
                        reason="specific_layer_override",
                    ))
                    applied.append(AppliedRuleRecord(
                        key=key, rule_id=winner.rule_id, layer=winner.layer, category=winner.category,
                        value=winner.value, decision="shadowed", superseded_rule_id=candidate.rule_id,
                        reason="specific_layer_override",
                    ))
                    winner = candidate
                    effective[key] = candidate.value
                else:
                    applied.append(AppliedRuleRecord(
                        key=key, rule_id=candidate.rule_id, layer=candidate.layer, category=candidate.category,
                        value=candidate.value, decision="shadowed", superseded_rule_id=winner.rule_id,
                        reason="lower_specificity",
                    ))

        resolution = RulesResolution(
            effective=effective,
            applied=applied,
            conflicts=conflicts,
            blocked=blocked,
            sources=sources,
        )
        self.last_resolution = resolution
        return resolution

    def effective(self, **kwargs: Any) -> Dict[str, Any]:
        return self.resolve(**kwargs).effective
