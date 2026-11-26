"""R1-FIX-14: Policy Lifecycle, Version Registry, Pinning & Rollback."""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from app.runtime.policy.ag_policy import AGPolicy, DefaultAGPolicy

ENGINE_VERSION = "1.1.0-r1.1"


class PolicyRegistry:
    """Manages registered AGPolicy implementations independently from AGCore Engine Version.

    - Supports runtime policy selection & rollback without code changes.
    - Runs pin the returned AGPolicy instance at start; switching active policy affects new runs only.
    - Live ACL revocation remains governed by ConstraintSpec.check_tool_permission() immediately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._policies: Dict[str, AGPolicy] = {}
        self._active_version: Optional[str] = None
        self._history: List[str] = []

    def register(self, policy: AGPolicy, *, activate: bool = False) -> None:
        with self._lock:
            self._policies[policy.version] = policy
            if self._active_version is None or activate:
                if self._active_version is not None and self._active_version != policy.version:
                    self._history.append(self._active_version)
                self._active_version = policy.version

    def activate(self, version: str) -> AGPolicy:
        with self._lock:
            if version not in self._policies:
                raise KeyError(f"Unknown policy version: {version}")
            if self._active_version and self._active_version != version:
                self._history.append(self._active_version)
            self._active_version = version
            return self._policies[version]

    def rollback(self) -> AGPolicy:
        with self._lock:
            if not self._history:
                raise RuntimeError("No previous policy version available for rollback")
            prev = self._history.pop()
            self._active_version = prev
            return self._policies[prev]

    def get_active(self) -> AGPolicy:
        with self._lock:
            if self._active_version is None:
                default = DefaultAGPolicy()
                self._policies[default.version] = default
                self._active_version = default.version
            return self._policies[self._active_version]

    def list_versions(self) -> List[str]:
        with self._lock:
            return sorted(self._policies.keys())


policy_registry = PolicyRegistry()
policy_registry.register(DefaultAGPolicy(), activate=True)
