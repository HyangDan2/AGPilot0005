"""Allowed-root policy for folder-based workspaces (Amendment 01 §A1.7, §A1.9).

Every server-side folder path that becomes a workspace root passes through this module:

* the path must be absolute and already normalized (no `..`, no control characters)
* it must resolve **inside** a declared allowed root for the caller's scope
* no component may be a symlink (a link would let a workspace escape its root)
* the root must exist, be a directory and not contain the engine's metadata store

`PathSafety` is also the single join gate used by `folder.py` for relative paths inside a root.
"""
from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

from app.workspace.contracts import AllowedRootInfo, InvalidRelativePath, validate_relative_path


class RootPolicyError(Exception):
    """Root/relative-path failure; `code` uses the workspace error vocabulary."""

    def __init__(self, code: str, message: str = "") -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class AllowedRoot:
    """One allowed root. `{owner}` / `{tenant}` placeholders are expanded per caller."""

    root_id: str
    path: str
    scope: str = "owner"  # owner | tenant | global
    writable: bool = True
    is_default: bool = False


@dataclass(frozen=True)
class ResolvedRoot:
    root: AllowedRoot
    path: Path
    display_path: str

    @property
    def writable(self) -> bool:
        return self.root.writable


def parse_allowed_roots(raw: str, *, default_root: Optional[Path] = None) -> Tuple[AllowedRoot, ...]:
    """Parse `id:/abs/path:scope[:ro]` comma-separated specs.

    An empty value yields one personal default root (`default`) at `default_root`.
    """

    specs = [item.strip() for item in (raw or "").split(",") if item.strip()]
    roots: List[AllowedRoot] = []
    for spec in specs:
        parts = spec.split(":")
        # a Windows drive letter would split too; server roots are POSIX paths
        if len(parts) < 2:
            continue
        root_id = parts[0].strip()
        # rejoin the path when a drive-like prefix was split off
        path_parts = [p for p in parts[1:] if p not in {"owner", "tenant", "global", "ro", "rw"}]
        scope = next((p for p in parts[1:] if p in {"owner", "tenant", "global"}), "owner")
        mode = next((p for p in parts[1:] if p in {"ro", "rw"}), "rw")
        path = ":".join(path_parts).strip()
        if not root_id or not path:
            continue
        roots.append(
            AllowedRoot(
                root_id=root_id,
                path=path,
                scope=scope,
                writable=mode != "ro",
                is_default=(len(roots) == 0),
            )
        )
    if not roots and default_root is not None:
        roots.append(
            AllowedRoot(
                root_id="default",
                path=str(Path(default_root) / "{tenant}" / "{owner}"),
                scope="owner",
                writable=True,
                is_default=True,
            )
        )
    return tuple(roots)


class AllowedRootPolicy:
    def __init__(
        self,
        roots: Sequence[AllowedRoot],
        *,
        deny_symlinks: bool = True,
        protected_paths: Iterable[Path | str] = (),
    ) -> None:
        self.roots = tuple(
            root for root in roots
            if Path(root.path).expanduser().is_absolute() and ".." not in Path(root.path).parts
        )
        self.deny_symlinks = deny_symlinks
        self.protected_paths = tuple(Path(p).expanduser().resolve() for p in protected_paths)

    # -- scope --------------------------------------------------------------------
    @staticmethod
    def _expand(root: AllowedRoot, tenant_id: str, owner_id: str) -> Tuple[Path, Path, Path]:
        template = Path(root.path).expanduser()
        parts = template.parts
        if root.scope in {"owner", "tenant"} and "{tenant}" not in parts:
            if "{owner}" in parts:
                owner_index = parts.index("{owner}")
                template = Path(*parts[:owner_index], "{tenant}", *parts[owner_index:])
            else:
                template /= "{tenant}"
        if root.scope == "owner" and "{owner}" not in template.parts:
            template /= "{owner}"

        scope_values = {"{tenant}": _safe_scope_segment(tenant_id), "{owner}": _safe_scope_segment(owner_id)}
        boundary = next((index for index, part in enumerate(template.parts) if part in scope_values), len(template.parts))
        base = Path(*template.parts[:boundary])
        # Resolve system aliases above the configured base while preserving the base and
        # every tenant/owner segment for the no-symlink check below.
        anchor = _resolve_lenient(base.parent)
        suffix = [scope_values.get(part, part) for part in template.parts[boundary:]]
        if base != base.parent:
            suffix.insert(0, base.name)
        return anchor.joinpath(*suffix), base.parent.joinpath(*suffix), anchor

    def visible_roots(self, tenant_id: str, owner_id: str) -> List[ResolvedRoot]:
        out: List[ResolvedRoot] = []
        seen: set[str] = set()
        for root in self.roots:
            path, _, _ = self._expand(root, tenant_id, owner_id)
            key = f"{root.root_id}:{path}"
            if key in seen:
                continue
            seen.add(key)
            try:
                display = str(path)
            except OSError:  # pragma: no cover - hostile path
                display = root.path
            out.append(ResolvedRoot(root=root, path=path, display_path=display))
        return out

    def describe(self, tenant_id: str, owner_id: str) -> List[AllowedRootInfo]:
        return [
            AllowedRootInfo(
                root_id=item.root.root_id,
                display_path=item.display_path,
                writable=item.root.writable,
                scope=item.root.scope,
                is_default=item.root.is_default,
            )
            for item in self.visible_roots(tenant_id, owner_id)
        ]

    # -- validation ---------------------------------------------------------------
    def resolve(
        self,
        tenant_id: str,
        owner_id: str,
        requested: str,
        *,
        root_id: Optional[str] = None,
        must_exist: bool = True,
        for_write: bool = False,
    ) -> ResolvedRoot:
        text = (requested or "").strip()
        if not text:
            raise RootPolicyError("invalid_path", "empty path")
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            raise RootPolicyError("invalid_path", "control characters rejected")
        candidate_in = Path(text)
        if not candidate_in.is_absolute():
            raise RootPolicyError("invalid_path", "absolute path required")
        if ".." in candidate_in.parts:
            raise RootPolicyError("invalid_path", "path traversal rejected")

        # Pick the root by the *textual* path first, so a symlink at the root boundary is reported
        # as a symlink rather than as an unrelated root miss, then verify the resolved path.
        text_roots: List[Tuple[int, AllowedRoot, Path, Path, Path]] = []
        for root in self.roots:
            if root_id is not None and root.root_id != root_id:
                continue
            expanded_root, configured_root, anchor = self._expand(root, tenant_id, owner_id)
            if _is_inside(candidate_in, expanded_root):
                candidate_text = candidate_in
            elif _is_inside(candidate_in, configured_root):
                candidate_text = expanded_root / candidate_in.relative_to(configured_root)
            else:
                continue
            text_roots.append((len(str(expanded_root)), root, expanded_root, anchor, candidate_text))
        if not text_roots:
            raise RootPolicyError("path_outside_allowed_root", "path is outside every allowed root")
        text_roots.sort(key=lambda entry: entry[0], reverse=True)
        _, matched, expanded_root, anchor, candidate_text = text_roots[0]
        if self.deny_symlinks:
            self._assert_no_symlink_components(anchor, candidate_text)
        root_path = _resolve_lenient(expanded_root)
        candidate = _resolve_lenient(candidate_text)
        if not _is_inside(candidate, root_path):
            reason = "symlink_escape" if self.deny_symlinks else "path_outside_allowed_root"
            raise RootPolicyError(reason, "path escapes the allowed root")

        if for_write and not matched.writable:
            raise RootPolicyError("read_only_workspace", "allowed root is read-only")
        if must_exist:
            try:
                info = os.lstat(candidate)
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise RootPolicyError("root_unavailable", "root does not exist") from exc
            if stat.S_ISLNK(info.st_mode):
                raise RootPolicyError("symlink_escape", "root is a symlink")
            if not candidate.is_dir():
                raise RootPolicyError("root_unavailable", "root is not a directory")
        self._assert_metadata_separate(candidate)
        return ResolvedRoot(root=matched, path=candidate, display_path=str(candidate))

    def assert_inside(self, root: Path, rel: str) -> str:
        """Validate a workspace-relative path; returns the normalized POSIX form."""

        try:
            clean = validate_relative_path(rel)
        except InvalidRelativePath as exc:
            raise RootPolicyError("invalid_path", str(exc)) from exc
        target = _resolve_lenient(root)
        if self.deny_symlinks:
            self._assert_no_symlink_components(target, target / clean)
        resolved = _resolve_lenient(target / clean)
        if not _is_inside(resolved, target):
            raise RootPolicyError("symlink_escape", "path escapes the workspace root")
        return clean

    # -- internals ----------------------------------------------------------------
    def _assert_no_symlink_components(self, root: Path, requested: Path) -> None:
        """Reject any symlink on the way from `root` down to `requested`."""

        try:
            relative = Path(requested).relative_to(Path(root))
        except ValueError:
            # The requested text uses a different prefix than the resolved root; containment is
            # already proven by `resolve()`, so there is nothing extra to walk.
            return
        current = Path(root)
        for part in relative.parts:
            current = current / part
            try:
                info = os.lstat(current)
            except (FileNotFoundError, NotADirectoryError):
                return
            except OSError as exc:  # pragma: no cover - hostile filesystem
                raise RootPolicyError("invalid_path", "unreadable path component") from exc
            if stat.S_ISLNK(info.st_mode):
                raise RootPolicyError("symlink_escape", "symlink component rejected")

    def _assert_metadata_separate(self, root: Path) -> None:
        resolved_root = _resolve_lenient(root)
        for protected in self.protected_paths:
            if _is_inside(protected, resolved_root) or _is_inside(resolved_root, protected):
                raise RootPolicyError(
                    "metadata_overlap", "workspace root would contain the workspace metadata store"
                )


def _safe_scope_segment(value: str) -> str:
    """Encode scope IDs in distinct, traversal-safe, case-insensitive directory segments."""
    raw = str(value or "")
    readable = set("abcdefghijklmnopqrstuvwxyz0123456789_-")
    if raw and len(raw) <= 64 and all(ch in readable for ch in raw):
        return raw
    return "~" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _resolve_lenient(path: Path | str) -> Path:
    """`Path.resolve()` without requiring existence (py3.12 already tolerates missing)."""

    try:
        return Path(path).resolve()
    except OSError:  # pragma: no cover - hostile filesystem
        return Path(path).absolute()


def _is_inside(candidate: Path, root: Path) -> bool:
    if candidate == root:
        return True
    root_text = str(root)
    if not root_text.endswith(os.sep):
        root_text += os.sep
    return str(candidate).startswith(root_text)
