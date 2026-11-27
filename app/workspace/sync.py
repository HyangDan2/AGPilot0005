"""Folder scan reconciliation for zero-copy workspaces (Amendment 01 §A1.4).

One `sync` pass compares the real folder tree with the metadata rows and applies the difference
through the existing revision and index lifecycle:

    create → file.created + PENDING index job
    modify → new revision, in-flight job cancelled, chunks dropped, file.updated
    move   → file.moved, file_id and index kept (same content hash detected by path difference)
    delete → chunks/vectors removed, jobs cancelled, file.deleted

Folders are rows too (kind=folder) so the explorer tree and display paths work unchanged. The
engine writes nothing into the workspace root: all state stays in the metadata store.
"""
from __future__ import annotations

import mimetypes
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.workspace import folder
from app.workspace.contracts import FileKind, IndexState, WorkspaceSyncResult


def _basename(rel: str) -> str:
    return rel.rsplit("/", 1)[-1]


def _dirname(rel: str) -> Optional[str]:
    return rel.rsplit("/", 1)[0] if "/" in rel else None


class FolderSyncService:
    """Reconciles one folder-backed workspace. Owned by the manager; never touches other rows."""

    def __init__(self, manager: Any, settings: Any) -> None:
        self.manager = manager
        self.store = manager.store
        self.settings = settings
        self.clock = manager.clock

    # ------------------------------------------------------------------ public
    def sync(self, workspace: Dict[str, Any], *, full: bool = False) -> WorkspaceSyncResult:
        workspace_id = workspace["workspace_id"]
        root = Path(workspace["root_path"] or "")
        started = self.clock()
        entries, errors = folder.scan_root(
            root,
            excludes=self.settings.scan_excludes,
            max_entries=self.settings.scan_max_entries,
            max_depth=self.settings.scan_max_depth,
        )
        scan: Dict[str, folder.FolderEntry] = {entry.relative_path: entry for entry in entries}
        rows = self.store.list_external_files(workspace_id)
        by_rel: Dict[str, Dict[str, Any]] = {row["rel_path"]: row for row in rows if row.get("rel_path")}

        counts = {"created": 0, "modified": 0, "moved": 0, "deleted": 0, "unchanged": 0}
        scan_files = [entry for entry in entries if entry.is_file]
        file_limit = int(self.settings.max_files_per_workspace)
        live_files = sum(1 for row in rows if row["kind"] == FileKind.FILE.value)

        # 1. Folders, parents before children.
        folder_rows: Dict[str, str] = {}
        for entry in sorted(
            (item for item in entries if not item.is_file),
            key=lambda item: (item.relative_path.count("/"), item.relative_path),
        ):
            rel = entry.relative_path
            parent_id = folder_rows.get(_dirname(rel) or "")
            row = by_rel.get(rel)
            if row is None or row["kind"] != FileKind.FOLDER.value:
                new_id = self._insert_row(workspace_id, rel, FileKind.FOLDER, parent_id, size=0, digest=None, mtime=entry.mtime)
                folder_rows[rel] = new_id
                counts["created"] += 1
                by_rel[rel] = {"file_id": new_id, "kind": FileKind.FOLDER.value, "rel_path": rel, "parent_id": parent_id}
                continue
            if row["parent_id"] != parent_id or row["name"] != _basename(rel):
                self.store.update_file(
                    workspace_id,
                    row["file_id"],
                    {"parent_id": parent_id, "name": _basename(rel), "updated_at": self.clock(), "synced_at": self.clock()},
                )
                counts["moved"] += 1
            folder_rows[rel] = row["file_id"]

        # 2. Files that vanished from disk are delete or move candidates.
        vanished = [
            row
            for rel, row in by_rel.items()
            if rel not in scan and row["kind"] == FileKind.FILE.value and row.get("rel_path")
        ]
        by_hash: Dict[str, List[Dict[str, Any]]] = {}
        for row in vanished:
            if row.get("content_hash"):
                by_hash.setdefault(row["content_hash"], []).append(row)

        for entry in scan_files:
            rel = entry.relative_path
            row = by_rel.get(rel)
            if row is not None and row["kind"] == FileKind.FILE.value:
                outcome = self._refresh_existing(workspace, row, entry)
                counts[outcome] += 1
                continue

            if live_files >= file_limit:
                errors.append({"path": rel, "code": "file_limit"})
                continue
            digest = self._digest(root, entry, errors)
            if digest is None:
                continue
            moved_from = self._take_move_candidate(by_hash, digest)
            if moved_from is not None:
                parent_id = folder_rows.get(_dirname(rel) or "")
                self.store.update_file(
                    workspace_id,
                    moved_from["file_id"],
                    {
                        "parent_id": parent_id,
                        "name": _basename(rel),
                        "rel_path": rel,
                        "ext_mtime": entry.mtime,
                        "synced_at": self.clock(),
                        "updated_at": self.clock(),
                    },
                )
                self.manager._emit(
                    workspace_id,
                    moved_from["file_id"],
                    "filesystem.changed",
                    int(moved_from["revision"]),
                    {"change": "moved", "path": rel, "from": moved_from.get("rel_path")},
                )
                counts["moved"] += 1
                by_rel.pop(moved_from.get("rel_path") or "", None)
                by_rel[rel] = {**moved_from, "rel_path": rel}
                continue

            parent_id = folder_rows.get(_dirname(rel) or "")
            file_id = self._insert_row(
                workspace_id,
                rel,
                FileKind.FILE,
                parent_id,
                size=entry.size_bytes,
                digest=digest,
                mtime=entry.mtime,
            )
            live_files += 1
            counts["created"] += 1
            by_rel[rel] = {"file_id": file_id, "kind": FileKind.FILE.value, "rel_path": rel, "parent_id": parent_id}

        # 3. Whatever is still missing is gone.
        for rel, row in list(by_rel.items()):
            if rel in scan:
                continue
            if row["kind"] == FileKind.FOLDER.value:
                continue
            self._delete_row(workspace_id, row, scan)
            counts["deleted"] += 1

        # 4. Folder rows with no scanned path and no live children are removed too.
        for rel in sorted(
            (rel for rel, row in by_rel.items() if row["kind"] == FileKind.FOLDER.value and rel not in scan),
            key=lambda item: item.count("/"),
            reverse=True,
        ):
            row = by_rel[rel]
            if self.store.list_files(workspace_id, parent_id=row["file_id"]):
                continue
            self.store.soft_delete_file(workspace_id, row["file_id"], self.clock())
            self.manager._emit(workspace_id, row["file_id"], "file.deleted", int(row.get("revision") or 0))
            counts["deleted"] += 1

        finished = self.clock()
        return WorkspaceSyncResult(
            workspace_id=workspace_id,
            full=full,
            scanned=len(scan_files),
            created=counts["created"],
            modified=counts["modified"],
            moved=counts["moved"],
            deleted=counts["deleted"],
            unchanged=counts["unchanged"],
            errors=errors[:50],
            started_at=started,
            finished_at=finished,
        )

    # ------------------------------------------------------------------ helpers
    def _digest(self, root: Path, entry: folder.FolderEntry, errors: List[dict]) -> Optional[str]:
        try:
            digest, _size = folder.hash_relative(root, entry.relative_path, max_bytes=self.settings.max_file_bytes)
            return digest
        except folder.FolderError as exc:
            errors.append({"path": entry.relative_path, "code": exc.code})
            return None

    def _refresh_existing(
        self, workspace: Dict[str, Any], row: Dict[str, Any], entry: folder.FolderEntry
    ) -> str:
        workspace_id = workspace["workspace_id"]
        root = Path(workspace["root_path"] or "")
        stamp = self.clock()
        # Fast path: unchanged size and mtime need no hashing.
        if row.get("content_hash") and int(row.get("size_bytes") or 0) == entry.size_bytes and row.get("ext_mtime") == entry.mtime:
            self.store.update_file(workspace_id, row["file_id"], {"synced_at": stamp})
            return "unchanged"
        try:
            digest, size = folder.hash_relative(root, entry.relative_path, max_bytes=self.settings.max_file_bytes)
        except folder.FolderError as exc:
            if exc.code == "file_too_large":
                self.store.update_file(
                    workspace_id,
                    row["file_id"],
                    {"size_bytes": entry.size_bytes, "ext_mtime": entry.mtime, "synced_at": stamp},
                )
                self.store.mark_index_status(workspace_id, row["file_id"], IndexState.FAILED.value, error="file_too_large", at=stamp)
                return "unchanged"
            return "unchanged"
        if row.get("content_hash") == digest:
            self.store.update_file(
                workspace_id,
                row["file_id"],
                {"size_bytes": size, "ext_mtime": entry.mtime, "synced_at": stamp},
            )
            return "unchanged"

        revision = int(row["revision"]) + 1
        self.store.add_revision(
            {
                "file_id": row["file_id"],
                "workspace_id": workspace_id,
                "revision": revision,
                "content_hash": digest,
                "size_bytes": size,
                "blob_key": "external",
                "parser_version": None,
                "created_at": stamp,
            }
        )
        self.store.cancel_jobs_for_file(row["file_id"], stamp, reason="file_changed")
        self.store.delete_chunks(row["file_id"])
        self.store.update_file(
            workspace_id,
            row["file_id"],
            {
                "size_bytes": size,
                "content_hash": digest,
                "revision": revision,
                "index_status": IndexState.PENDING.value,
                "index_error": None,
                "index_updated_at": stamp,
                "ext_mtime": entry.mtime,
                "synced_at": stamp,
                "updated_at": stamp,
            },
        )
        self.manager._prune_revisions(workspace["tenant_id"], workspace_id, row["file_id"], origin="external")
        self.manager._emit(workspace_id, row["file_id"], "filesystem.changed", revision, {"change": "modified", "path": entry.relative_path})
        self.manager._emit(workspace_id, row["file_id"], "file.updated", revision)
        self.manager._notify_file(workspace_id, row["file_id"])
        return "modified"

    @staticmethod
    def _take_move_candidate(
        by_hash: Dict[str, List[Dict[str, Any]]], digest: str
    ) -> Optional[Dict[str, Any]]:
        candidates = by_hash.get(digest)
        if not candidates or len(candidates) != 1:
            return None
        return candidates.pop(0)

    def _insert_row(
        self,
        workspace_id: str,
        rel_path: str,
        kind: FileKind,
        parent_id: Optional[str],
        *,
        size: int,
        digest: Optional[str],
        mtime: Optional[float],
    ) -> str:
        stamp = self.clock()
        file_id = "wf_" + secrets.token_hex(6)
        name = _basename(rel_path)
        record = {
            "file_id": file_id,
            "workspace_id": workspace_id,
            "parent_id": parent_id,
            "kind": kind.value,
            "name": name,
            "mime_type": mimetypes.guess_type(name)[0] if kind is FileKind.FILE else None,
            "size_bytes": int(size),
            "content_hash": digest,
            "revision": 1,
            "index_status": IndexState.PENDING.value if kind is FileKind.FILE else None,
            "index_error": None,
            "index_updated_at": stamp if kind is FileKind.FILE else None,
            "created_at": stamp,
            "updated_at": stamp,
            "deleted_at": None,
            "origin": "external",
            "rel_path": rel_path,
            "ext_mtime": mtime,
            "synced_at": stamp,
        }
        self.store.insert_file(record)
        if kind is FileKind.FILE and digest:
            self.store.add_revision(
                {
                    "file_id": file_id,
                    "workspace_id": workspace_id,
                    "revision": 1,
                    "content_hash": digest,
                    "size_bytes": int(size),
                    "blob_key": "external",
                    "parser_version": None,
                    "created_at": stamp,
                }
            )
        self.manager._emit(workspace_id, file_id, "filesystem.changed", 1, {"change": "created", "path": rel_path})
        self.manager._emit(workspace_id, file_id, "file.created", 1)
        if kind is FileKind.FILE:
            self.manager._notify_file(workspace_id, file_id)
        return file_id

    def _delete_row(self, workspace_id: str, row: Dict[str, Any], scan: Dict[str, folder.FolderEntry]) -> None:
        stamp = self.clock()
        self.store.cancel_jobs_for_file(row["file_id"], stamp, reason="file_deleted")
        self.store.delete_chunks(row["file_id"])
        self.store.delete_revisions(row["file_id"])
        self.store.soft_delete_file(workspace_id, row["file_id"], stamp)
        self.manager._emit(
            workspace_id,
            row["file_id"],
            "filesystem.changed",
            int(row.get("revision") or 0),
            {"change": "deleted", "path": row.get("rel_path")},
        )
        self.manager._emit(workspace_id, row["file_id"], "file.deleted", int(row.get("revision") or 0))
        _ = scan
