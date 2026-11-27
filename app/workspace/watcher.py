"""File watcher for folder-based workspaces (Amendment 01 §A1.4).

A lightweight polling watcher: it needs no watchdog/inotify dependency and behaves the same on
every platform. Each pass lists the open folder-backed workspaces and reconciles each one through
`WorkspaceManager.sync_workspace_record`, which diffs the real tree against metadata and applies
create / modify / move / delete to the RAG index.

* `poll_once()` runs at most one pass and returns per-workspace results (tests and the CLI use it).
* `start()` / `stop()` drive the background thread with `watch_interval_seconds`.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional

from app.workspace.config import WorkspaceSettings
from app.workspace.manager import WorkspaceManager


class WorkspaceWatcher:
    def __init__(self, manager: WorkspaceManager, settings: WorkspaceSettings) -> None:
        self.manager = manager
        self.settings = settings
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_results: Dict[str, Dict[str, Any]] = {}

    # -- one pass ----------------------------------------------------------------
    def poll_once(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cap = int(limit or self.settings.watch_max_workspaces_per_pass)
        for record in self.manager.store.list_watchable_workspaces(cap):
            workspace_id = record["workspace_id"]
            try:
                result = self.manager.sync_workspace_record(record, full=False)
                entry = {
                    "workspace_id": workspace_id,
                    "changed": result.created + result.modified + result.moved + result.deleted,
                    "created": result.created,
                    "modified": result.modified,
                    "moved": result.moved,
                    "deleted": result.deleted,
                    "error": None,
                }
            except Exception as exc:  # a broken root must not stop the other workspaces
                entry = {
                    "workspace_id": workspace_id,
                    "changed": 0,
                    "created": 0,
                    "modified": 0,
                    "moved": 0,
                    "deleted": 0,
                    "error": str(exc)[:300],
                }
            self.last_results[workspace_id] = entry
            results.append(entry)
        return results

    # -- background thread ---------------------------------------------------------
    def start(self, interval: Optional[float] = None) -> None:
        if not self.settings.watch_enabled:
            return
        if self._thread and self._thread.is_alive():
            return
        period = float(interval if interval is not None else self.settings.watch_interval_seconds)
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, args=(period,), name="workspace-watcher", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
        if thread is not None and not thread.is_alive():
            self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self, interval: float) -> None:
        period = max(0.02, interval)
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - the watcher must survive any single failure
                pass
            self._stop.wait(period)
