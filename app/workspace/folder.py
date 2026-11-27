"""Filesystem access for folder-based workspaces (Amendment 01 §A1.3, §A1.5).

Everything here is intentionally small and explicit:

* `scan_root` walks a workspace root without following symlinks and returns entries keyed by
  POSIX relative path. Excluded names (VCS/build directories) are skipped, and the engine's own
  bookkeeping files are never returned.
* `read_relative` / `write_relative` / `ensure_directory` all go through `_secure_join`, which
  re-validates the relative path and refuses symlink components on the way.
* Writes are atomic: temp file in the target directory, fsync, `os.replace`.

No function copies file bodies anywhere else; the workspace root is the only owner of the bytes.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Sequence, Set, Tuple

from app.workspace.contracts import FileKind, validate_relative_path
from app.workspace.rootfs import RootPolicyError, _is_inside, _resolve_lenient

DEFAULT_EXCLUDES = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
)
METADATA_NAMES = (".hd2lab-workspace.json", ".hd2lab-workspace")


@dataclass(frozen=True)
class FolderEntry:
    relative_path: str
    kind: FileKind
    size_bytes: int
    mtime: float

    @property
    def is_file(self) -> bool:
        return self.kind is FileKind.FILE


class FolderError(Exception):
    """Filesystem failure mapped onto a workspace error code."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


def _secure_join(root: Path, relative: str) -> Tuple[Path, str]:
    try:
        clean = validate_relative_path(relative)
    except Exception as exc:  # InvalidRelativePath
        raise FolderError("invalid_path", str(exc)) from exc
    base = _resolve_lenient(root)
    current = base
    for part in clean.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as exc:  # pragma: no cover - hostile filesystem
            raise FolderError("invalid_path", "unreadable path component") from exc
        if stat.S_ISLNK(info.st_mode):
            raise FolderError("symlink_escape", "symlink component rejected")
    target = _resolve_lenient(base / clean)
    if not _is_inside(target, base):
        raise FolderError("symlink_escape", "path escapes the workspace root")
    return target, clean


def scan_root(
    root: Path,
    *,
    excludes: Sequence[str] = DEFAULT_EXCLUDES,
    max_entries: int = 20000,
    max_depth: int = 24,
) -> Tuple[List[FolderEntry], List[dict]]:
    """Walk `root` and return (entries, errors). Directories come before their children."""

    base = _resolve_lenient(root)
    if not base.exists() or not base.is_dir():
        raise FolderError("root_unavailable", "workspace root is not a directory")
    if base.is_symlink():
        raise FolderError("symlink_escape", "workspace root is a symlink")

    excluded: Set[str] = {name for name in excludes if name}
    entries: List[FolderEntry] = []
    errors: List[dict] = []
    stack: List[Tuple[Path, str, int]] = [(base, "", 0)]
    while stack:
        directory, prefix, depth = stack.pop()
        if depth > max_depth:
            errors.append({"path": prefix, "code": "max_depth"})
            continue
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            errors.append({"path": prefix, "code": "unreadable", "detail": str(exc)[:200]})
            continue
        for child in children:
            rel = f"{prefix}/{child.name}" if prefix else child.name
            if child.name in excluded or child.name in METADATA_NAMES:
                continue
            try:
                info = child.stat(follow_symlinks=False)
            except OSError as exc:
                errors.append({"path": rel, "code": "unreadable", "detail": str(exc)[:200]})
                continue
            if stat.S_ISLNK(info.st_mode):
                errors.append({"path": rel, "code": "symlink_skipped"})
                continue
            if stat.S_ISDIR(info.st_mode):
                entries.append(
                    FolderEntry(relative_path=rel, kind=FileKind.FOLDER, size_bytes=0, mtime=info.st_mtime)
                )
                if len(entries) >= max_entries:
                    errors.append({"path": rel, "code": "scan_truncated"})
                    return entries, errors
                stack.append((Path(child.path), rel, depth + 1))
            elif stat.S_ISREG(info.st_mode):
                entries.append(
                    FolderEntry(
                        relative_path=rel,
                        kind=FileKind.FILE,
                        size_bytes=int(info.st_size),
                        mtime=float(info.st_mtime),
                    )
                )
                if len(entries) >= max_entries:
                    errors.append({"path": rel, "code": "scan_truncated"})
                    return entries, errors
    return entries, errors


def content_hash(path: Path, *, max_bytes: int = 0) -> Tuple[str, int]:
    """`sha256:<hex>` of a file; `max_bytes` > 0 rejects larger files with `file_too_large`."""

    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if max_bytes and total > max_bytes:
                    raise FolderError("file_too_large", "file exceeds size limit")
                digest.update(block)
    except FileNotFoundError as exc:
        raise FolderError("file_not_found", "file disappeared") from exc
    except OSError as exc:
        raise FolderError("invalid_path", str(exc)) from exc
    return "sha256:" + digest.hexdigest(), total


def hash_relative(root: Path, relative: str, *, max_bytes: int = 0) -> Tuple[str, int]:
    """Hash a file by relative path without loading it into memory."""

    target, clean = _secure_join(root, relative)
    if not target.exists() or not target.is_file():
        raise FolderError("file_not_found", f"missing file: {clean}")
    return content_hash(target, max_bytes=max_bytes)


def read_relative(root: Path, relative: str, *, max_bytes: int = 0) -> Tuple[bytes, str]:
    target, clean = _secure_join(root, relative)
    if not target.exists() or not target.is_file():
        raise FolderError("file_not_found", f"missing file: {clean}")
    digest = hashlib.sha256()
    chunks: List[bytes] = []
    total = 0
    try:
        with open(target, "rb") as handle:
            while True:
                block = handle.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if max_bytes and total > max_bytes:
                    raise FolderError("file_too_large", "file exceeds size limit")
                digest.update(block)
                chunks.append(block)
    except FileNotFoundError as exc:
        raise FolderError("file_not_found", "file disappeared") from exc
    except OSError as exc:
        raise FolderError("invalid_path", str(exc)) from exc
    return b"".join(chunks), "sha256:" + digest.hexdigest()


def iter_relative(root: Path, relative: str, *, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    target, clean = _secure_join(root, relative)
    if not target.exists() or not target.is_file():
        raise FolderError("file_not_found", f"missing file: {clean}")
    with open(target, "rb") as handle:
        while True:
            block = handle.read(max(1, chunk_size))
            if not block:
                return
            yield block


def ensure_directory(root: Path, relative: str) -> Path:
    """Create `relative` (a directory) under root, rejecting symlink components."""

    if not relative:
        return _resolve_lenient(root)
    try:
        clean = validate_relative_path(relative)
    except Exception as exc:
        raise FolderError("invalid_path", str(exc)) from exc
    base = _resolve_lenient(root)
    current = base
    for part in clean.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=0o755)
            continue
        except OSError as exc:  # pragma: no cover - hostile filesystem
            raise FolderError("invalid_path", str(exc)) from exc
        if stat.S_ISLNK(info.st_mode):
            raise FolderError("symlink_escape", "symlink component rejected")
        if not stat.S_ISDIR(info.st_mode):
            raise FolderError("invalid_path", "path component is not a directory")
    return current


def write_relative(
    root: Path,
    relative: str,
    data: bytes,
    *,
    create_parents: bool = True,
    overwrite: bool = True,
    max_bytes: int = 0,
    mode: int = 0o644,
) -> Tuple[int, str]:
    """Write bytes atomically at `relative`; returns (size, content_hash)."""

    target, clean = _secure_join(root, relative)
    if max_bytes and len(data) > max_bytes:
        raise FolderError("file_too_large", "file exceeds size limit")
    if target.exists() and not overwrite:
        raise FolderError("file_exists", f"file exists: {clean}")
    if target.exists() and target.is_dir():
        raise FolderError("invalid_path", "target is a directory")
    parent_rel = clean.rsplit("/", 1)[0] if "/" in clean else ""
    if parent_rel:
        if not create_parents:
            parent, _ = _secure_join(root, parent_rel)
            if not parent.exists():
                raise FolderError("file_not_found", "parent directory missing")
        else:
            ensure_directory(root, parent_rel)
    directory = target.parent
    tmp = directory / f".{target.name}.{secrets.token_hex(6)}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256(data)
    fd = os.open(tmp, flags, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return len(data), "sha256:" + digest.hexdigest()


def remove_relative(root: Path, relative: str) -> int:
    """Delete a single file inside the root (never a directory tree)."""

    target, clean = _secure_join(root, relative)
    if not target.exists():
        return 0
    if target.is_dir():
        raise FolderError("invalid_path", "refusing to remove a directory as a file")
    target.unlink()
    return 1


def try_remove_empty_directory(root: Path, relative: str) -> bool:
    target, _clean = _secure_join(root, relative)
    try:
        if target.is_dir() and not any(target.iterdir()):
            target.rmdir()
            return True
    except OSError:
        return False
    return False


def is_metadata_free(root: Path) -> bool:
    """True when the engine has written no bookkeeping inside the root."""

    try:
        return not any((Path(root) / name).exists() for name in METADATA_NAMES)
    except OSError:  # pragma: no cover
        return True


def existing_rel_paths(root: Path, relatives: Iterable[str]) -> Set[str]:
    found: Set[str] = set()
    for rel in relatives:
        try:
            target, clean = _secure_join(root, rel)
        except FolderError:
            continue
        if target.exists():
            found.add(clean)
    return found


__all__ = [
    "DEFAULT_EXCLUDES",
    "FolderEntry",
    "FolderError",
    "RootPolicyError",
    "content_hash",
    "ensure_directory",
    "existing_rel_paths",
    "hash_relative",
    "is_metadata_free",
    "iter_relative",
    "read_relative",
    "remove_relative",
    "scan_root",
    "try_remove_empty_directory",
    "write_relative",
]
