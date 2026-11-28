"""R21 Document & Knowledge Engine (AGCore side: contract owner and federation layer)."""

from app.knowledge.contracts import CONTRACT_VERSION
from app.knowledge.references import (
    EVIDENCE_COVERAGE_EXTENSION,
    UNIFIED_REFERENCE_CONTRACT_VERSION,
    UNIFIED_REFERENCE_EXTENSION,
    AnalysisTarget,
    CoverageStatus,
    DocumentReference,
    EvidenceCoverage,
    EvidenceTracker,
    ReferenceResolution,
    ReferenceScope,
    ReferenceSourceType,
    SynthesisBudget,
    resolve_references,
)

__all__ = [
    "CONTRACT_VERSION",
    "UNIFIED_REFERENCE_CONTRACT_VERSION",
    "UNIFIED_REFERENCE_EXTENSION",
    "EVIDENCE_COVERAGE_EXTENSION",
    "AnalysisTarget",
    "CoverageStatus",
    "DocumentReference",
    "EvidenceCoverage",
    "EvidenceTracker",
    "ReferenceResolution",
    "ReferenceScope",
    "ReferenceSourceType",
    "SynthesisBudget",
    "resolve_references",
]
