"""Artifact event contract constants (shared with ``app.schemas.events``)."""

from __future__ import annotations

from typing import Tuple

#: WO-C06 event set. Order is the expected lifecycle order of one generation.
ARTIFACT_EVENT_TYPES: Tuple[str, ...] = (
    "artifact.created",
    "artifact.delta",
    "artifact.updated",
    "artifact.edit.proposed",
    "artifact.edit.applied",
    "artifact.edit.rejected",
    "artifact.error",
)
