"""Content-addressed workspace file storage with an atomic, streaming writer.

Path policy
-----------
The on-disk path is built **only** from server identifiers:

    {storage_root}/{tenant_segment}/{workspace_id}/{file_id}/r{revision}.blob
    {storage_root}/{tenant_segment}/{workspace_id}/.tmp/{file_id}.{random}.part

User supplied file names never reach the file system, so path traversal is impossible by
construction; every path is also re-checked against the root, and symlinked components are
refused. Reads and writes use `O_NOFOLLOW`.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
from pathlib import Path
from typing import BinaryIO, Iterable, Iterator, Optional, Tuple

_ID_RE = re.compile(r"^[a-z]{2,4}_[a-z0-9]{4,40}$")
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]")


class StorageError(Exception):
    """Storage-level failure; `code` matches the workspace error vocabulary."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def _safe_segment(value: str, *, max_len: int = 64) -> str:
    """Map an identifier to a safe single path segment (never contains '/' or '..')."""

    if not value:
        return "_"
    cleaned = _SAFE_SEGMENT.sub("_", value)[:max_len]
    if cleaned in {"", ".", ".."}:
        cleaned = "_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
    if cleaned != value:
        cleaned = f"{cleaned}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}"
    return cleaned


def validate_storage_id(value: str, kind: str) -> str:
    if not value or not _ID_RE.match(value) or not value.startswith(f"{kind}_"):
        raise StorageError("invalid_path", f"invalid {kind} id")
    return value


class WorkspaceStorage:
    def __init__(self, root: Path | str, *, max_file_bytes: int, chunk_bytes: int = 1024 * 1024) -> None:
        self.root = Path(root).expanduser().resolve()
        self.max_file_bytes = int(max_file_bytes)
        self.chunk_bytes = max(1, int(chunk_bytes))
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise StorageError("invalid_path", "storage root must not be a symlink")

    # -- path helpers -------------------------------------------------------------
    def _assert_inside(self, path: Path) -> Path:
        try:
            resolved = path.resolve()
        except OSError as exc:  # pragma: no cover - hostile filesystem
            raise StorageError("invalid_path", str(exc)) from exc
        root = str(self.root)
        if resolved != self.root and not str(resolved).startswith(root + os.sep):
            raise StorageError("invalid_path", "path escapes storage root")
        return resolved

    def _assert_no_symlink_components(self, path: Path) -> None:
        current = self.root
        for part in path.relative_to(self.root).parts:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                return
            if stat.S_ISLNK(info.st_mode):
                raise StorageError("invalid_path", "symlink component rejected")

    def workspace_dir(self, tenant_id: str, workspace_id: str) -> Path:
        validate_storage_id(workspace_id, "ws")
        path = self.root / _safe_segment(tenant_id) / workspace_id
        self._assert_inside(path)
        return path

    def file_dir(self, tenant_id: str, workspace_id: str, file_id: str) -> Path:
        validate_storage_id(file_id, "wf")
        path = self.workspace_dir(tenant_id, workspace_id) / file_id
        self._assert_inside(path)
        return path

    def blob_path(self, tenant_id: str, workspace_id: str, file_id: str, revision: int) -> Path:
        if revision < 1:
            raise StorageError("invalid_path", "invalid revision")
        path = self.file_dir(tenant_id, workspace_id, file_id) / f"r{int(revision)}.blob"
        self._assert_inside(path)
        return path

    def _ensure_dir(self, path: Path) -> None:
        self._assert_inside(path)
        self._assert_no_symlink_components(path)
        path.mkdir(parents=True, exist_ok=True)
        self._assert_no_symlink_components(path)

    # -- writes -------------------------------------------------------------------
    def write_stream(
        self,
        tenant_id: str,
        workspace_id: str,
        file_id: str,
        revision: int,
        chunks: Iterable[bytes],
        *,
        max_bytes: Optional[int] = None,
    ) -> Tuple[int, str]:
        """Stream `chunks` to a temp file, fsync, then atomically publish the blob.

        Returns (size_bytes, "sha256:<hex>"). On any failure the temp file is removed and the
        previous blob (if any) is untouched.
        """

        limit = int(max_bytes if max_bytes is not None else self.max_file_bytes)
        target = self.blob_path(tenant_id, workspace_id, file_id, revision)
        directory = target.parent
        self._ensure_dir(directory)
        tmp_dir = self.workspace_dir(tenant_id, workspace_id) / ".tmp"
        self._ensure_dir(tmp_dir)

        tmp_path = tmp_dir / f"{file_id}.{secrets.token_hex(8)}.part"
        digest = hashlib.sha256()
        size = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(tmp_path, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > limit:
                        raise StorageError("file_too_large", "file exceeds size limit")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size == 0:
                raise StorageError("invalid_request", "empty file")
            os.replace(tmp_path, target)
            self._fsync_dir(directory)
        except BaseException:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass
            raise
        return size, "sha256:" + digest.hexdigest()

    def write_bytes(
        self,
        tenant_id: str,
        workspace_id: str,
        file_id: str,
        revision: int,
        data: bytes,
        *,
        max_bytes: Optional[int] = None,
    ) -> Tuple[int, str]:
        return self.write_stream(tenant_id, workspace_id, file_id, revision, [data], max_bytes=max_bytes)

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        try:
            fd = os.open(directory, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    # -- reads --------------------------------------------------------------------
    def open_reader(self, tenant_id: str, workspace_id: str, file_id: str, revision: int) -> BinaryIO:
        path = self.blob_path(tenant_id, workspace_id, file_id, revision)
        self._assert_inside(path)
        self._assert_no_symlink_components(path)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError as exc:
            raise StorageError("file_not_found", "blob missing") from exc
        except OSError as exc:
            raise StorageError("invalid_path", str(exc)) from exc
        return os.fdopen(fd, "rb")

    def read_bytes(self, tenant_id: str, workspace_id: str, file_id: str, revision: int) -> bytes:
        with self.open_reader(tenant_id, workspace_id, file_id, revision) as handle:
            return handle.read()

    def iter_content(
        self, tenant_id: str, workspace_id: str, file_id: str, revision: int, chunk_size: Optional[int] = None
    ) -> Iterator[bytes]:
        size = int(chunk_size or self.chunk_bytes)
        with self.open_reader(tenant_id, workspace_id, file_id, revision) as handle:
            while True:
                block = handle.read(size)
                if not block:
                    break
                yield block

    def read_head(self, tenant_id: str, workspace_id: str, file_id: str, revision: int, limit: int = 64 * 1024) -> str:
        with self.open_reader(tenant_id, workspace_id, file_id, revision) as handle:
            raw = handle.read(max(1, int(limit)))
        return raw.decode("utf-8", errors="replace")

    # -- deletes ------------------------------------------------------------------
    def delete_blob(self, tenant_id: str, workspace_id: str, file_id: str, revision: int) -> bool:
        try:
            path = self.blob_path(tenant_id, workspace_id, file_id, revision)
            self._assert_no_symlink_components(path)
            path.unlink()
            return True
        except (FileNotFoundError, StorageError):
            return False

    def delete_file(self, tenant_id: str, workspace_id: str, file_id: str) -> int:
        directory = self.file_dir(tenant_id, workspace_id, file_id)
        self._assert_no_symlink_components(directory)
        if not directory.exists():
            return 0
        removed = sum(1 for _ in directory.glob("*.blob"))
        shutil.rmtree(directory, ignore_errors=True)
        return removed

    def delete_workspace(self, tenant_id: str, workspace_id: str) -> int:
        directory = self.workspace_dir(tenant_id, workspace_id)
        self._assert_inside(directory)
        self._assert_no_symlink_components(directory)
        if not directory.exists():
            return 0
        removed = sum(1 for _ in directory.rglob("*.blob"))
        shutil.rmtree(directory, ignore_errors=True)
        return removed

    def workspace_usage(self, tenant_id: str, workspace_id: str) -> int:
        directory = self.workspace_dir(tenant_id, workspace_id)
        if not directory.exists():
            return 0
        total = 0
        for entry in directory.rglob("*.blob"):
            try:
                total += entry.stat().st_size
            except OSError:
                continue
        return total

    # -- recovery -----------------------------------------------------------------
    def sweep_temp(self, older_than_seconds: float = 3600.0, now: Optional[float] = None) -> int:
        """Remove interrupted upload temp files older than the grace period."""

        import time

        cutoff = (now if now is not None else time.time()) - older_than_seconds
        removed = 0
        for tmp_dir in self.root.rglob(".tmp"):
            if not tmp_dir.is_dir():
                continue
            for entry in tmp_dir.iterdir():
                try:
                    if entry.is_file() and entry.stat().st_mtime <= cutoff:
                        entry.unlink()
                        removed += 1
                except OSError:
                    continue
        return removed

    def orphan_blobs(self, known_paths: Iterable[Path]) -> Iterator[Path]:
        known = {str(Path(p).resolve()) for p in known_paths}
        for blob in self.root.rglob("*.blob"):
            try:
                resolved = str(blob.resolve())
            except OSError:
                continue
            if resolved not in known:
                yield blob
