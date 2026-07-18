"""Manage current-user-private local worker state.

Static links/reparse points, path escapes, unsafe cleanup, normal concurrency, and
crash durability are handled here. A same-user process that can actively replace
ordinary directory entries between system calls is outside the security boundary.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import json
import os
import re
import secrets
import stat
from pathlib import Path
import tempfile
import time

from codex_worker_dispatcher.errors import WorkerError


TERMINAL_STATES = {
    "completed",
    "failed",
    "timed_out",
    "cancelled",
    "reaped",
    "orphaned",
}
_TASK_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_WINDOWS_RESERVED_BASENAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_MANIFEST_READ_ATTEMPTS = 20
_MANIFEST_RETRY_DELAY_SECONDS = 0.025
_STATE_WRITE_ATTEMPTS = 20
_STATE_WRITE_RETRY_DELAY_SECONDS = 0.025
_MANIFEST_LOCK_ATTEMPTS = 400
_MANIFEST_LOCK_RETRY_DELAY_SECONDS = 0.01
_MANIFEST_LOCK_FILENAME = ".manifest.lock"
_UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS = {errno.EINVAL, errno.ENOTSUP, errno.ENOSYS}
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_RETRYABLE_WINDOWS_REPLACE_ERRNOS = {
    errno.EACCES,
    errno.EBUSY,
    getattr(errno, "ETXTBSY", errno.EBUSY),
}
_RETRYABLE_WINDOWS_REPLACE_WINERRORS = {5, 32, 33, 170, 1224}


class _StateFileChanged(WorkerError):
    """An otherwise valid state file changed during one read attempt."""


class _StateWriteConflict(Exception):
    """A transient Windows conflict prevented one atomic replace attempt."""


def _close_descriptor(file_descriptor: int | None) -> None:
    if file_descriptor is None:
        return
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _closed_capability_error(kind: str, path: Path) -> WorkerError:
    return WorkerError(
        "invalid_state",
        f"{kind} capability is closed: {path}",
        {"path": str(path)},
    )


@dataclass
class _StateParent:
    path: Path
    identity: tuple[int, int, int]
    file_descriptor: int | None = None
    closed: bool = False

    def __enter__(self) -> "_StateParent":
        if self.closed:
            raise _closed_capability_error("State parent", self.path)
        return self

    def close(self) -> None:
        if self.closed:
            return
        _close_descriptor(self.file_descriptor)
        self.file_descriptor = None
        self.closed = True

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.close()


@dataclass
class _StateRoot:
    path: Path
    identity: tuple[int, int, int]
    file_descriptor: int | None = None
    parent: _StateParent | None = None
    closed: bool = False

    def __enter__(self) -> "_StateRoot":
        if self.closed:
            raise _closed_capability_error("State root", self.path)
        return self

    def close(self) -> None:
        if self.closed:
            return
        _close_descriptor(self.file_descriptor)
        self.file_descriptor = None
        if self.parent is not None:
            self.parent.close()
            self.parent = None
        self.closed = True

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.close()


@dataclass
class _TaskDirectory:
    path: Path
    identity: tuple[int, int, int]
    file_descriptor: int | None = None
    root: _StateRoot | None = None
    closed: bool = False

    @property
    def name(self) -> str:
        return self.path.name

    def __enter__(self) -> "_TaskDirectory":
        if self.closed:
            raise _closed_capability_error("Task directory", self.path)
        return self

    def close(self) -> None:
        if self.closed:
            return
        _close_descriptor(self.file_descriptor)
        self.file_descriptor = None
        if self.root is not None:
            self.root.close()
            self.root = None
        self.closed = True

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> None:
        self.close()


class _ManifestLock:
    def __init__(self, store: "StateStore", task: _TaskDirectory) -> None:
        self.store = store
        self.task = task
        self.descriptor: int | None = None
        self.acquired = False

    def __enter__(self) -> None:
        descriptor = self.store._open_manifest_lock(self.task)
        self.descriptor = descriptor
        try:
            for attempt in range(_MANIFEST_LOCK_ATTEMPTS):
                try:
                    self.acquired = self.store._try_acquire_manifest_lock(descriptor)
                except OSError as error:
                    raise WorkerError(
                        "invalid_state",
                        f"Could not acquire manifest lock for task: {self.task.name}",
                        {"task_id": self.task.name},
                    ) from error
                if self.acquired:
                    break
                if attempt + 1 < _MANIFEST_LOCK_ATTEMPTS:
                    time.sleep(_MANIFEST_LOCK_RETRY_DELAY_SECONDS)
            if not self.acquired:
                raise WorkerError(
                    "invalid_state",
                    f"Timed out acquiring manifest lock for task: {self.task.name}",
                    {"task_id": self.task.name},
                )
            self.store._ensure_manifest_lock_byte(descriptor, self.task)
            self.store._validate_task_dir(self.task)
        except BaseException:
            _close_descriptor(self.descriptor)
            self.descriptor = None
            raise

    def __exit__(
        self,
        exception_type: object,
        exception: object,
        traceback: object,
    ) -> bool:
        descriptor = self.descriptor
        self.descriptor = None
        if descriptor is None:
            return False
        if self.acquired:
            try:
                self.store._release_manifest_lock(descriptor)
            except OSError:
                pass
        _close_descriptor(descriptor)
        return False


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_task_id(task_id: str) -> bool:
    if _TASK_ID.fullmatch(task_id) is None or task_id.endswith("."):
        return False
    return task_id.split(".", 1)[0] not in _WINDOWS_RESERVED_BASENAMES


def default_state_root() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        root = Path(codex_home) / "worker-runs"
    else:
        root = Path.home() / ".codex" / "worker-runs"
    return _absolute_without_following_final_link(root)


def _absolute_without_following_final_link(path: Path) -> Path:
    absolute = Path(os.path.abspath(os.fspath(path.expanduser())))
    if absolute.parent == absolute:
        return absolute
    return absolute.parent.resolve(strict=False) / absolute.name


def _fsync_directory(path: Path, file_descriptor: int | None = None) -> None:
    if os.name == "nt":
        return

    opened_descriptor: int | None = None
    try:
        if file_descriptor is None:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            opened_descriptor = os.open(path, flags)
            file_descriptor = opened_descriptor
        os.fsync(file_descriptor)
    except OSError as error:
        if error.errno not in _UNSUPPORTED_DIRECTORY_FSYNC_ERRNOS:
            raise
    finally:
        if opened_descriptor is not None:
            os.close(opened_descriptor)


def _stat_identity(result: os.stat_result) -> tuple[int, int, int]:
    return result.st_dev, result.st_ino, result.st_mode


def _is_reparse_point(result: os.stat_result) -> bool:
    return bool(
        getattr(result, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _supports_directory_fd() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    supports_follow_symlinks = getattr(os, "supports_follow_symlinks", set())
    return (
        os.name != "nt"
        and os.open in supports_dir_fd
        and os.stat in supports_dir_fd
        and os.stat in supports_follow_symlinks
    )


def _supports_relative_replace() -> bool:
    supports_dir_fd = getattr(os, "supports_dir_fd", set())
    return os.replace in supports_dir_fd and os.unlink in supports_dir_fd


def _is_retryable_windows_file_conflict(error: OSError) -> bool:
    return os.name == "nt" and (
        error.errno in _RETRYABLE_WINDOWS_REPLACE_ERRNOS
        or getattr(error, "winerror", None) in _RETRYABLE_WINDOWS_REPLACE_WINERRORS
    )


class StateStore:
    def __init__(self, root: Path | None = None) -> None:
        selected_root = default_state_root() if root is None else root
        self.root = _absolute_without_following_final_link(selected_root)
        try:
            parent = self.root.parent.lstat()
        except OSError:
            self._parent_identity: tuple[int, int, int] | None = None
        else:
            self._parent_identity = _stat_identity(parent)

    def _existing_parent(
        self,
        *,
        missing_ok: bool = False,
    ) -> _StateParent | None:
        parent_path = self.root.parent
        try:
            initial = parent_path.lstat()
        except (FileNotFoundError, NotADirectoryError) as error:
            if missing_ok:
                return None
            raise WorkerError(
                "task_not_found",
                f"State parent not found: {parent_path}",
                {"parent": str(parent_path)},
            ) from error
        except OSError as error:
            raise WorkerError(
                "invalid_state",
                f"Could not inspect state parent: {parent_path}",
                {"parent": str(parent_path)},
            ) from error
        identity = _stat_identity(initial)
        if (
            not stat.S_ISDIR(initial.st_mode)
            or stat.S_ISLNK(initial.st_mode)
            or _is_reparse_point(initial)
            or (
                self._parent_identity is not None
                and identity != self._parent_identity
            )
        ):
            raise WorkerError(
                "invalid_state",
                f"State parent is not the original directory: {parent_path}",
                {"parent": str(parent_path)},
            )

        file_descriptor: int | None = None
        try:
            if _supports_directory_fd():
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                file_descriptor = os.open(parent_path, flags)
                opened = os.fstat(file_descriptor)
                if (
                    _stat_identity(opened) != identity
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    raise WorkerError(
                        "invalid_state",
                        f"State parent changed while opening: {parent_path}",
                        {"parent": str(parent_path)},
                    )
            current = parent_path.lstat()
            if (
                _stat_identity(current) != identity
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"State parent changed while validating: {parent_path}",
                    {"parent": str(parent_path)},
                )
            if self._parent_identity is None:
                self._parent_identity = identity
            elif self._parent_identity != identity:
                raise WorkerError(
                    "invalid_state",
                    f"State parent identity changed: {parent_path}",
                    {"parent": str(parent_path)},
                )
            return _StateParent(parent_path, identity, file_descriptor)
        except WorkerError:
            _close_descriptor(file_descriptor)
            raise
        except OSError as error:
            _close_descriptor(file_descriptor)
            raise WorkerError(
                "invalid_state",
                f"Could not inspect state parent: {parent_path}",
                {"parent": str(parent_path)},
            ) from error

    def _parent_matches(self, parent: _StateParent) -> bool:
        if parent.closed:
            return False
        try:
            current = parent.path.lstat()
            if (
                _stat_identity(current) != parent.identity
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                return False
            if parent.file_descriptor is not None:
                opened = os.fstat(parent.file_descriptor)
                if (
                    _stat_identity(opened) != parent.identity
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    return False
            return True
        except OSError:
            return False

    def _validate_parent(self, parent: _StateParent) -> None:
        if not self._parent_matches(parent):
            raise WorkerError(
                "invalid_state",
                f"State parent changed during state operation: {parent.path}",
                {"parent": str(parent.path)},
            )

    def _root_entry_lstat(self, parent: _StateParent) -> os.stat_result:
        self._validate_parent(parent)
        try:
            if parent.file_descriptor is not None and self.root.name:
                result = os.stat(
                    self.root.name,
                    dir_fd=parent.file_descriptor,
                    follow_symlinks=False,
                )
            else:
                result = self.root.lstat()
        except (FileNotFoundError, NotADirectoryError):
            self._validate_parent(parent)
            raise
        self._validate_parent(parent)
        return result

    def _task_path(self, task_id: str) -> Path:
        if not valid_task_id(task_id):
            raise WorkerError(
                "invalid_task_id",
                f"Invalid task ID: {task_id}",
                {"task_id": task_id},
            )
        return self.root / task_id

    def _existing_root(self, *, missing_ok: bool = False) -> _StateRoot | None:
        parent = self._existing_parent(missing_ok=missing_ok)
        if parent is None:
            return None
        file_descriptor: int | None = None
        try:
            try:
                initial = self._root_entry_lstat(parent)
            except (FileNotFoundError, NotADirectoryError) as error:
                if missing_ok:
                    parent.close()
                    return None
                raise WorkerError(
                    "task_not_found",
                    f"State root not found: {self.root}",
                    {"root": str(self.root)},
                ) from error
            if (
                not stat.S_ISDIR(initial.st_mode)
                or stat.S_ISLNK(initial.st_mode)
                or _is_reparse_point(initial)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"State root is not a regular directory: {self.root}",
                    {"root": str(self.root)},
                )
            if _supports_directory_fd():
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                if parent.file_descriptor is not None and self.root.name:
                    file_descriptor = os.open(
                        self.root.name,
                        flags,
                        dir_fd=parent.file_descriptor,
                    )
                else:
                    file_descriptor = os.open(self.root, flags)
                opened = os.fstat(file_descriptor)
                if (
                    _stat_identity(opened) != _stat_identity(initial)
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    raise WorkerError(
                        "invalid_state",
                        f"State root changed while opening: {self.root}",
                        {"root": str(self.root)},
                    )
            current = self._root_entry_lstat(parent)
            if (
                _stat_identity(current) != _stat_identity(initial)
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"State root changed while validating: {self.root}",
                    {"root": str(self.root)},
                )
            return _StateRoot(
                path=self.root,
                identity=_stat_identity(initial),
                file_descriptor=file_descriptor,
                parent=parent,
            )
        except WorkerError:
            _close_descriptor(file_descriptor)
            parent.close()
            raise
        except OSError as error:
            _close_descriptor(file_descriptor)
            parent.close()
            raise WorkerError(
                "invalid_state",
                f"Could not inspect state root: {self.root}",
                {"root": str(self.root)},
            ) from error

    def _root_matches(self, root: _StateRoot) -> bool:
        if root.closed:
            return False
        try:
            if root.parent is not None and not self._parent_matches(root.parent):
                return False
            if (
                root.parent is not None
                and root.parent.file_descriptor is not None
                and root.path.name
            ):
                current = os.stat(
                    root.path.name,
                    dir_fd=root.parent.file_descriptor,
                    follow_symlinks=False,
                )
            else:
                current = root.path.lstat()
            if (
                _stat_identity(current) != root.identity
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                return False
            if root.file_descriptor is not None:
                opened = os.fstat(root.file_descriptor)
                if (
                    _stat_identity(opened) != root.identity
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    return False
            if root.parent is not None and not self._parent_matches(root.parent):
                return False
            return True
        except OSError:
            return False

    def _validate_root(self, root: _StateRoot) -> None:
        if not self._root_matches(root):
            raise WorkerError(
                "invalid_state",
                f"State root changed during state operation: {root.path}",
                {"root": str(root.path)},
            )

    def _clone_root(self, root: _StateRoot) -> _StateRoot:
        self._validate_root(root)
        file_descriptor: int | None = None
        parent: _StateParent | None = None
        try:
            if root.parent is not None:
                parent = self._clone_parent(root.parent)
            if root.file_descriptor is not None:
                file_descriptor = os.dup(root.file_descriptor)
            clone = _StateRoot(root.path, root.identity, file_descriptor, parent)
            self._validate_root(root)
            self._validate_root(clone)
            return clone
        except (OSError, WorkerError):
            _close_descriptor(file_descriptor)
            if parent is not None:
                parent.close()
            raise

    def _clone_parent(self, parent: _StateParent) -> _StateParent:
        self._validate_parent(parent)
        file_descriptor: int | None = None
        try:
            if parent.file_descriptor is not None:
                file_descriptor = os.dup(parent.file_descriptor)
            clone = _StateParent(parent.path, parent.identity, file_descriptor)
            self._validate_parent(parent)
            self._validate_parent(clone)
            return clone
        except (OSError, WorkerError):
            _close_descriptor(file_descriptor)
            raise

    def _task_entry_lstat(
        self,
        root: _StateRoot,
        task_name: str,
    ) -> os.stat_result:
        self._validate_root(root)
        try:
            if root.file_descriptor is not None:
                result = os.stat(
                    task_name,
                    dir_fd=root.file_descriptor,
                    follow_symlinks=False,
                )
            else:
                result = (root.path / task_name).lstat()
        except (FileNotFoundError, NotADirectoryError):
            self._validate_root(root)
            raise
        self._validate_root(root)
        return result

    def task_dir(self, task_id: str) -> Path:
        task_dir = self._task_path(task_id)
        root = self._existing_root(missing_ok=True)
        if root is None:
            return task_dir
        with root:
            try:
                current = self._task_entry_lstat(root, task_id)
            except (FileNotFoundError, NotADirectoryError):
                return task_dir
            except OSError as error:
                raise WorkerError(
                    "invalid_state",
                    f"Could not inspect task directory: {task_id}",
                    {"task_id": task_id},
                ) from error
            if stat.S_ISLNK(current.st_mode) or _is_reparse_point(current):
                raise WorkerError(
                    "invalid_task_id",
                    f"Task directory cannot be a link or reparse point: {task_id}",
                    {"task_id": task_id},
                )
        return task_dir

    def _create_missing_ancestors(self, directory: Path) -> None:
        missing: list[Path] = []
        current = directory
        while True:
            try:
                existing = current.lstat()
            except (FileNotFoundError, NotADirectoryError):
                if current.parent == current:
                    raise WorkerError(
                        "invalid_state",
                        f"Could not find an existing state ancestor: {directory}",
                        {"ancestor": str(directory)},
                    )
                missing.append(current)
                current = current.parent
                continue
            if (
                not stat.S_ISDIR(existing.st_mode)
                or stat.S_ISLNK(existing.st_mode)
                or _is_reparse_point(existing)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"State ancestor is not a directory: {current}",
                    {"ancestor": str(current)},
                )
            break

        for ancestor in reversed(missing):
            parent_path = ancestor.parent
            try:
                os.mkdir(ancestor, mode=0o777)
            except FileExistsError:
                pass
            entry = ancestor.lstat()
            if (
                not stat.S_ISDIR(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
                or _is_reparse_point(entry)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Created state ancestor is invalid: {ancestor}",
                    {"ancestor": str(ancestor)},
                )
            _fsync_directory(parent_path)

    def _create_root(self) -> None:
        parent = self._existing_parent(missing_ok=True)
        if parent is None:
            self._create_missing_ancestors(self.root.parent)
            parent = self._existing_parent()
        if parent is None:
            raise WorkerError(
                "invalid_state",
                f"Could not create state parent: {self.root.parent}",
                {"parent": str(self.root.parent)},
            )
        with parent:
            self._validate_parent(parent)
            try:
                if (
                    parent.file_descriptor is not None
                    and self.root.name
                    and os.mkdir in getattr(os, "supports_dir_fd", set())
                ):
                    os.mkdir(
                        self.root.name,
                        mode=0o777,
                        dir_fd=parent.file_descriptor,
                    )
                else:
                    os.mkdir(self.root)
            except FileExistsError:
                pass
            current = self._root_entry_lstat(parent)
            if (
                not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Created state root is invalid: {self.root}",
                    {"root": str(self.root)},
                )
            _fsync_directory(parent.path, parent.file_descriptor)
            self._validate_parent(parent)

    def create_task_dir(self, task_id: str) -> Path:
        task_dir = self._task_path(task_id)
        try:
            root = self._existing_root(missing_ok=True)
            if root is None:
                self._create_root()
                root = self._existing_root()
            if root is None:
                raise WorkerError(
                    "invalid_state",
                    f"Could not create state root: {self.root}",
                    {"root": str(self.root)},
                )
            with root:
                self._validate_root(root)
                try:
                    if (
                        root.file_descriptor is not None
                        and os.mkdir in getattr(os, "supports_dir_fd", set())
                    ):
                        os.mkdir(task_id, mode=0o700, dir_fd=root.file_descriptor)
                    else:
                        os.mkdir(task_dir, mode=0o700)
                except FileExistsError as error:
                    current = self._task_entry_lstat(root, task_id)
                    self._validate_root(root)
                    _fsync_directory(root.path, root.file_descriptor)
                    self._validate_root(root)
                    if stat.S_ISLNK(current.st_mode) or _is_reparse_point(current):
                        raise WorkerError(
                            "invalid_task_id",
                            f"Task directory cannot be a link or reparse point: {task_id}",
                            {"task_id": task_id},
                        ) from error
                    raise WorkerError(
                        "task_exists",
                        f"Task already exists: {task_id}",
                        {"task_id": task_id},
                    ) from error
                created = self._task_entry_lstat(root, task_id)
                if (
                    not stat.S_ISDIR(created.st_mode)
                    or stat.S_ISLNK(created.st_mode)
                    or _is_reparse_point(created)
                ):
                    raise WorkerError(
                        "invalid_state",
                        f"Created task directory is invalid: {task_id}",
                        {"task_id": task_id},
                    )
                self._validate_root(root)
                _fsync_directory(root.path, root.file_descriptor)
                self._validate_root(root)
        except OSError as error:
            raise WorkerError(
                "invalid_state",
                f"Could not create task directory: {task_id}",
                {"task_id": task_id},
            ) from error
        return task_dir

    def _existing_task_dir(
        self,
        task_dir: Path,
        *,
        root: _StateRoot | None = None,
        expected_identity: tuple[int, int, int] | None = None,
    ) -> _TaskDirectory:
        task_dir = Path(task_dir)
        if (
            task_dir.parent != self.root
            or ".." in task_dir.parts
            or not valid_task_id(task_dir.name)
        ):
            if root is not None:
                root.close()
            raise WorkerError(
                "invalid_task_id",
                f"Invalid task directory: {task_dir}",
                {"task_dir": str(task_dir)},
            )
        if root is None:
            root = self._existing_root()
        if root is None:
            raise WorkerError(
                "task_not_found",
                f"Task not found: {task_dir.name}",
                {"task_id": task_dir.name},
            )
        file_descriptor: int | None = None
        try:
            try:
                initial = self._task_entry_lstat(root, task_dir.name)
            except (FileNotFoundError, NotADirectoryError) as error:
                raise WorkerError(
                    "task_not_found",
                    f"Task not found: {task_dir.name}",
                    {"task_id": task_dir.name},
                ) from error
            if stat.S_ISLNK(initial.st_mode) or _is_reparse_point(initial):
                raise WorkerError(
                    "invalid_task_id",
                    f"Task directory cannot be a link or reparse point: {task_dir.name}",
                    {"task_id": task_dir.name},
                )
            if not stat.S_ISDIR(initial.st_mode):
                raise WorkerError(
                    "task_not_found",
                    f"Task not found: {task_dir.name}",
                    {"task_id": task_dir.name},
                )
            if (
                expected_identity is not None
                and _stat_identity(initial) != expected_identity
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Task directory changed before opening: {task_dir.name}",
                    {"task_id": task_dir.name},
                )
            if _supports_directory_fd():
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                if root.file_descriptor is not None:
                    file_descriptor = os.open(
                        task_dir.name,
                        flags,
                        dir_fd=root.file_descriptor,
                    )
                else:
                    file_descriptor = os.open(task_dir, flags)
                opened = os.fstat(file_descriptor)
                if (
                    _stat_identity(opened) != _stat_identity(initial)
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    raise WorkerError(
                        "invalid_state",
                        f"Task directory changed while opening: {task_dir.name}",
                        {"task_id": task_dir.name},
                    )
            current = self._task_entry_lstat(root, task_dir.name)
            if (
                _stat_identity(current) != _stat_identity(initial)
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Task directory changed while validating: {task_dir.name}",
                    {"task_id": task_dir.name},
                )
            return _TaskDirectory(
                path=task_dir,
                identity=_stat_identity(initial),
                file_descriptor=file_descriptor,
                root=root,
            )
        except WorkerError:
            _close_descriptor(file_descriptor)
            root.close()
            raise
        except OSError as error:
            _close_descriptor(file_descriptor)
            root.close()
            raise WorkerError(
                "invalid_state",
                f"Could not inspect task directory: {task_dir.name}",
                {"task_id": task_dir.name},
            ) from error

    def _task_dir_matches(self, task_dir: _TaskDirectory) -> bool:
        if task_dir.closed:
            return False
        try:
            if task_dir.root is not None and not self._root_matches(task_dir.root):
                return False
            if task_dir.root is not None and task_dir.root.file_descriptor is not None:
                current = os.stat(
                    task_dir.name,
                    dir_fd=task_dir.root.file_descriptor,
                    follow_symlinks=False,
                )
            else:
                current = task_dir.path.lstat()
            if (
                _stat_identity(current) != task_dir.identity
                or not stat.S_ISDIR(current.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or _is_reparse_point(current)
            ):
                return False
            if task_dir.file_descriptor is not None:
                opened = os.fstat(task_dir.file_descriptor)
                if (
                    _stat_identity(opened) != task_dir.identity
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    return False
            if task_dir.root is not None and not self._root_matches(task_dir.root):
                return False
            return True
        except OSError:
            return False

    def _validate_task_dir(self, task_dir: _TaskDirectory) -> None:
        if not self._task_dir_matches(task_dir):
            raise WorkerError(
                "invalid_state",
                f"Task directory changed during state operation: {task_dir.name}",
                {"task_id": task_dir.name},
            )

    def _state_lstat(
        self,
        task_dir: _TaskDirectory,
        filename: str,
    ) -> os.stat_result:
        self._validate_task_dir(task_dir)
        try:
            if task_dir.file_descriptor is not None:
                result = os.stat(
                    filename,
                    dir_fd=task_dir.file_descriptor,
                    follow_symlinks=False,
                )
            else:
                result = (task_dir.path / filename).lstat()
        except FileNotFoundError:
            self._validate_task_dir(task_dir)
            raise
        except OSError as error:
            raise WorkerError(
                "invalid_state",
                f"Could not inspect {filename} for task: {task_dir.name}",
                {"task_id": task_dir.name, "filename": filename},
            ) from error
        self._validate_task_dir(task_dir)
        return result

    def _require_regular_state_file(
        self,
        task_dir: _TaskDirectory,
        filename: str,
        result: os.stat_result,
    ) -> None:
        if task_dir.closed:
            raise _closed_capability_error("Task directory", task_dir.path)
        if (
            not stat.S_ISREG(result.st_mode)
            or stat.S_ISLNK(result.st_mode)
            or _is_reparse_point(result)
        ):
            raise WorkerError(
                "invalid_state",
                f"State file is not a regular file: {filename}",
                {"task_id": task_dir.name, "filename": filename},
            )

    def _read_state_text(self, task_dir: _TaskDirectory, filename: str) -> str:
        before = self._state_lstat(task_dir, filename)
        self._require_regular_state_file(task_dir, filename, before)

        file_descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            self._validate_task_dir(task_dir)
            if task_dir.file_descriptor is not None:
                file_descriptor = os.open(
                    filename,
                    flags,
                    dir_fd=task_dir.file_descriptor,
                )
            else:
                file_descriptor = os.open(task_dir.path / filename, flags)
            opened = os.fstat(file_descriptor)
            if (
                _stat_identity(opened) != _stat_identity(before)
                or not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
            ):
                raise _StateFileChanged(
                    "invalid_state",
                    f"State file changed while opening: {filename}",
                    {"task_id": task_dir.name, "filename": filename},
                )
            self._validate_task_dir(task_dir)
            stream = os.fdopen(file_descriptor, "r", encoding="utf-8", newline="")
            file_descriptor = None
            with stream:
                content = stream.read()
            after = self._state_lstat(task_dir, filename)
            self._require_regular_state_file(task_dir, filename, after)
            if _stat_identity(after) != _stat_identity(before):
                raise _StateFileChanged(
                    "invalid_state",
                    f"State file changed while reading: {filename}",
                    {"task_id": task_dir.name, "filename": filename},
                )
            self._validate_task_dir(task_dir)
            return content
        except FileNotFoundError as error:
            raise _StateFileChanged(
                "invalid_state",
                f"State file disappeared while reading: {filename}",
                {"task_id": task_dir.name, "filename": filename},
            ) from error
        finally:
            _close_descriptor(file_descriptor)

    def _cleanup_temp_file(
        self,
        task_dir: _TaskDirectory,
        temp_name: str,
        identity: tuple[int, int, int],
        relative: bool,
    ) -> None:
        if task_dir.closed:
            return
        try:
            if relative and task_dir.file_descriptor is not None:
                current = os.stat(
                    temp_name,
                    dir_fd=task_dir.file_descriptor,
                    follow_symlinks=False,
                )
                if (
                    _stat_identity(current) == identity
                    and stat.S_ISREG(current.st_mode)
                    and not _is_reparse_point(current)
                ):
                    os.unlink(temp_name, dir_fd=task_dir.file_descriptor)
                return
            if not self._task_dir_matches(task_dir):
                return
            temp_path = task_dir.path / temp_name
            current = temp_path.lstat()
            if (
                _stat_identity(current) == identity
                and stat.S_ISREG(current.st_mode)
                and not stat.S_ISLNK(current.st_mode)
                and not _is_reparse_point(current)
            ):
                temp_path.unlink()
        except OSError:
            pass

    def _validate_temp_file(
        self,
        task_dir: _TaskDirectory,
        temp_name: str,
        identity: tuple[int, int, int],
    ) -> None:
        current = self._state_lstat(task_dir, temp_name)
        self._require_regular_state_file(task_dir, temp_name, current)
        if _stat_identity(current) != identity:
            raise WorkerError(
                "invalid_state",
                f"Temporary state file changed: {temp_name}",
                {"task_id": task_dir.name, "filename": temp_name},
            )

    def _validate_readable_state_file(
        self,
        task_dir: _TaskDirectory,
        filename: str,
    ) -> None:
        for attempt in range(_STATE_WRITE_ATTEMPTS):
            file_descriptor: int | None = None
            try:
                current = self._state_lstat(task_dir, filename)
                self._require_regular_state_file(task_dir, filename, current)
                flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                self._validate_task_dir(task_dir)
                if task_dir.file_descriptor is not None:
                    file_descriptor = os.open(
                        filename,
                        flags,
                        dir_fd=task_dir.file_descriptor,
                    )
                else:
                    file_descriptor = os.open(task_dir.path / filename, flags)
                opened = os.fstat(file_descriptor)
                if not stat.S_ISREG(opened.st_mode) or _is_reparse_point(opened):
                    raise WorkerError(
                        "invalid_state",
                        f"Persisted state file is not readable: {filename}",
                        {"task_id": task_dir.name, "filename": filename},
                    )
                self._validate_task_dir(task_dir)
                return
            except OSError as error:
                if (
                    not _is_retryable_windows_file_conflict(error)
                    or attempt + 1 == _STATE_WRITE_ATTEMPTS
                ):
                    raise
                time.sleep(_STATE_WRITE_RETRY_DELAY_SECONDS)
            finally:
                _close_descriptor(file_descriptor)

    def _create_temp_file(
        self,
        task_dir: _TaskDirectory,
        filename: str,
    ) -> tuple[int, str, tuple[int, int, int], bool]:
        self._validate_task_dir(task_dir)
        file_descriptor: int | None = None
        temp_name: str | None = None
        identity: tuple[int, int, int] | None = None
        relative = (
            task_dir.file_descriptor is not None and _supports_relative_replace()
        )
        try:
            if relative:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_BINARY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                for _ in range(100):
                    temp_name = f".{filename}.{secrets.token_hex(8)}.tmp"
                    try:
                        file_descriptor = os.open(
                            temp_name,
                            flags,
                            0o600,
                            dir_fd=task_dir.file_descriptor,
                        )
                        break
                    except FileExistsError:
                        continue
                else:
                    raise OSError(errno.EEXIST, "Could not allocate temp file")
            else:
                file_descriptor, temp_path = tempfile.mkstemp(
                    prefix=f".{filename}.",
                    suffix=".tmp",
                    dir=task_dir.path,
                )
                temp_name = Path(temp_path).name

            try:
                opened = os.fstat(file_descriptor)
            except OSError:
                # This retry is cleanup-only. If the descriptor remains
                # uninspectable, the pathname is unknown and must be retained.
                try:
                    cleanup_opened = os.fstat(file_descriptor)
                except OSError:
                    pass
                else:
                    if (
                        stat.S_ISREG(cleanup_opened.st_mode)
                        and not _is_reparse_point(cleanup_opened)
                    ):
                        identity = _stat_identity(cleanup_opened)
                raise
            identity = _stat_identity(opened)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Temporary state file is not regular: {filename}",
                    {"task_id": task_dir.name, "filename": filename},
                )
            if relative and task_dir.file_descriptor is not None:
                entry = os.stat(
                    temp_name,
                    dir_fd=task_dir.file_descriptor,
                    follow_symlinks=False,
                )
            else:
                self._validate_task_dir(task_dir)
                entry = (task_dir.path / temp_name).lstat()
                self._validate_task_dir(task_dir)
            if (
                _stat_identity(entry) != identity
                or not stat.S_ISREG(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
                or _is_reparse_point(entry)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Temporary state entry changed after creation: {filename}",
                    {"task_id": task_dir.name, "filename": filename},
                )
            self._validate_task_dir(task_dir)
            self._validate_temp_file(task_dir, temp_name, identity)
            return file_descriptor, temp_name, identity, relative
        except (OSError, WorkerError):
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if temp_name is not None and identity is not None:
                self._cleanup_temp_file(task_dir, temp_name, identity, relative)
            raise

    def _write_atomic_text(
        self,
        task_dir: _TaskDirectory,
        filename: str,
        content: str,
    ) -> None:
        last_error: _StateWriteConflict | None = None
        for attempt in range(_STATE_WRITE_ATTEMPTS):
            try:
                self._write_atomic_text_once(task_dir, filename, content)
                return
            except _StateWriteConflict as error:
                last_error = error
                if attempt + 1 < _STATE_WRITE_ATTEMPTS:
                    time.sleep(_STATE_WRITE_RETRY_DELAY_SECONDS)
        raise WorkerError(
            "invalid_state",
            f"Could not persist {filename} for task: {task_dir.name}",
            {"task_id": task_dir.name, "filename": filename},
        ) from last_error

    def _write_atomic_text_once(
        self,
        task_dir: _TaskDirectory,
        filename: str,
        content: str,
    ) -> None:
        try:
            target = self._state_lstat(task_dir, filename)
        except FileNotFoundError:
            pass
        else:
            self._require_regular_state_file(task_dir, filename, target)

        file_descriptor: int | None = None
        temp_name: str | None = None
        temp_identity: tuple[int, int, int] | None = None
        relative = False
        replaced = False
        try:
            (
                file_descriptor,
                temp_name,
                temp_identity,
                relative,
            ) = self._create_temp_file(
                task_dir,
                filename,
            )
            stream = os.fdopen(
                file_descriptor,
                "w",
                encoding="utf-8",
                newline="",
            )
            file_descriptor = None
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_task_dir(task_dir)
            self._validate_temp_file(task_dir, temp_name, temp_identity)
            try:
                target = self._state_lstat(task_dir, filename)
            except FileNotFoundError:
                pass
            else:
                self._require_regular_state_file(task_dir, filename, target)
            self._validate_task_dir(task_dir)
            # The private-directory threat model does not promise resistance to
            # same-user entry swaps here; Windows has no stdlib relative replace.
            try:
                if relative and task_dir.file_descriptor is not None:
                    os.replace(
                        temp_name,
                        filename,
                        src_dir_fd=task_dir.file_descriptor,
                        dst_dir_fd=task_dir.file_descriptor,
                    )
                else:
                    os.replace(task_dir.path / temp_name, task_dir.path / filename)
            except OSError as error:
                if _is_retryable_windows_file_conflict(error):
                    raise _StateWriteConflict from error
                raise
            replaced = True
            self._validate_readable_state_file(task_dir, filename)
            _fsync_directory(task_dir.path, task_dir.file_descriptor)
            self._validate_readable_state_file(task_dir, filename)
        except (OSError, UnicodeError, WorkerError, _StateWriteConflict) as error:
            if file_descriptor is not None:
                try:
                    os.close(file_descriptor)
                except OSError:
                    pass
            if (
                not replaced
                and temp_name is not None
                and temp_identity is not None
            ):
                self._cleanup_temp_file(
                    task_dir,
                    temp_name,
                    temp_identity,
                    relative,
                )
            if isinstance(error, (WorkerError, _StateWriteConflict)):
                raise
            raise WorkerError(
                "invalid_state",
                f"Could not persist {filename} for task: {task_dir.name}",
                {"task_id": task_dir.name, "filename": filename},
            ) from error

    def _read_manifest(self, task: _TaskDirectory) -> dict[str, object]:
        last_error: Exception | None = None
        invalid_manifest_seen = False
        changed_during_read_seen = False
        read_conflict_seen = False
        for attempt in range(_MANIFEST_READ_ATTEMPTS):
            try:
                content = self._read_state_text(task, "manifest.json")
                value = json.loads(content)
                if (
                    isinstance(value, dict)
                    and isinstance(value.get("task_id"), str)
                    and value["task_id"] == task.name
                ):
                    return value
                invalid_manifest_seen = True
                last_error = ValueError("Manifest identity does not match task")
            except _StateFileChanged as error:
                changed_during_read_seen = True
                last_error = error
            except FileNotFoundError as error:
                last_error = error
            except json.JSONDecodeError as error:
                invalid_manifest_seen = True
                last_error = error
            except OSError as error:
                if _is_retryable_windows_file_conflict(error):
                    read_conflict_seen = True
                    last_error = error
                else:
                    raise WorkerError(
                        "invalid_state",
                        f"Could not read manifest for task: {task.name}",
                        {"task_id": task.name},
                    ) from error
            except UnicodeError as error:
                raise WorkerError(
                    "invalid_state",
                    f"Could not read manifest for task: {task.name}",
                    {"task_id": task.name},
                ) from error

            if attempt + 1 < _MANIFEST_READ_ATTEMPTS:
                time.sleep(_MANIFEST_RETRY_DELAY_SECONDS)

        if invalid_manifest_seen or changed_during_read_seen or read_conflict_seen:
            raise WorkerError(
                "invalid_state",
                f"Invalid manifest for task: {task.name}",
                {"task_id": task.name},
            ) from last_error
        raise WorkerError(
            "task_not_found",
            f"Manifest not found for task: {task.name}",
            {"task_id": task.name},
        ) from last_error

    def read_manifest(self, task_dir: Path) -> dict[str, object]:
        with self._existing_task_dir(task_dir) as task:
            return self._read_manifest(task)

    def _open_manifest_lock(self, task: _TaskDirectory) -> int:
        self._validate_task_dir(task)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        try:
            if task.file_descriptor is not None:
                descriptor = os.open(
                    _MANIFEST_LOCK_FILENAME,
                    flags,
                    0o600,
                    dir_fd=task.file_descriptor,
                )
            else:
                descriptor = os.open(
                    task.path / _MANIFEST_LOCK_FILENAME,
                    flags,
                    0o600,
                )
        except OSError as error:
            raise WorkerError(
                "invalid_state",
                f"Could not open manifest lock for task: {task.name}",
                {"task_id": task.name},
            ) from error
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
                or opened.st_nlink != 1
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Manifest lock is not a regular file: {task.name}",
                    {"task_id": task.name},
                )
            self._validate_task_dir(task)
            entry = self._state_lstat(task, _MANIFEST_LOCK_FILENAME)
            if (
                _stat_identity(entry) != _stat_identity(opened)
                or not stat.S_ISREG(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
                or _is_reparse_point(entry)
                or entry.st_nlink != 1
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Manifest lock changed while opening: {task.name}",
                    {"task_id": task.name},
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            self._validate_task_dir(task)
            return descriptor
        except (OSError, WorkerError) as error:
            _close_descriptor(descriptor)
            if isinstance(error, WorkerError):
                raise
            raise WorkerError(
                "invalid_state",
                f"Could not initialize manifest lock for task: {task.name}",
                {"task_id": task.name},
            ) from error

    def _ensure_manifest_lock_byte(
        self,
        descriptor: int,
        task: _TaskDirectory,
    ) -> None:
        try:
            opened = os.fstat(descriptor)
            entry = self._state_lstat(task, _MANIFEST_LOCK_FILENAME)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _is_reparse_point(opened)
                or opened.st_nlink != 1
                or _stat_identity(entry) != _stat_identity(opened)
                or not stat.S_ISREG(entry.st_mode)
                or stat.S_ISLNK(entry.st_mode)
                or _is_reparse_point(entry)
                or entry.st_nlink != 1
            ):
                raise WorkerError(
                    "invalid_state",
                    f"Manifest lock changed before initialization: {task.name}",
                    {"task_id": task.name},
                )
            if opened.st_size < 1:
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
        except (OSError, WorkerError) as error:
            if isinstance(error, WorkerError):
                raise
            raise WorkerError(
                "invalid_state",
                f"Could not initialize manifest lock for task: {task.name}",
                {"task_id": task.name},
            ) from error

    def _try_acquire_manifest_lock(self, descriptor: int) -> bool:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            try:
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    return False
                raise
            return True

        import fcntl

        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise
        return True

    def _release_manifest_lock(self, descriptor: int) -> None:
        if os.name == "nt":
            import msvcrt

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN)

    def _manifest_lock(self, task: _TaskDirectory) -> _ManifestLock:
        return _ManifestLock(self, task)

    def _current_manifest_or_none(
        self,
        task: _TaskDirectory,
    ) -> dict[str, object] | None:
        try:
            current = self._state_lstat(task, "manifest.json")
        except FileNotFoundError:
            return None
        self._require_regular_state_file(task, "manifest.json", current)
        return self._read_manifest(task)

    def _validate_manifest_identity(
        self,
        task: _TaskDirectory,
        manifest: dict[str, object],
    ) -> None:
        if (
            not isinstance(manifest, dict)
            or not isinstance(manifest.get("task_id"), str)
            or manifest["task_id"] != task.name
        ):
            raise WorkerError(
                "invalid_state",
                f"Manifest identity does not match task: {task.name}",
                {"task_id": task.name},
            )

    def _write_manifest_unlocked(
        self,
        task: _TaskDirectory,
        manifest: dict[str, object],
    ) -> None:
        try:
            content = json.dumps(manifest, ensure_ascii=False, indent=2)
        except (TypeError, ValueError) as error:
            raise WorkerError(
                "invalid_state",
                f"Manifest is not serializable for task: {task.name}",
                {"task_id": task.name},
            ) from error
        self._write_atomic_text(task, "manifest.json", content)

    def write_manifest(
        self,
        task_dir: Path,
        manifest: dict[str, object],
    ) -> None:
        with self._existing_task_dir(task_dir) as task:
            self._validate_manifest_identity(task, manifest)
            with self._manifest_lock(task):
                current = self._current_manifest_or_none(task)
                if current is not None and current.get("status") in TERMINAL_STATES:
                    return
                self._write_manifest_unlocked(task, manifest)

    def update_manifest(
        self,
        task_dir: Path,
        updater: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        """Update a manifest under its cross-process lock and return the winner."""

        if not callable(updater):
            raise TypeError("updater must be callable")
        with self._existing_task_dir(task_dir) as task:
            with self._manifest_lock(task):
                current = self._current_manifest_or_none(task)
                if current is None:
                    raise WorkerError(
                        "task_not_found",
                        f"Manifest not found for task: {task.name}",
                        {"task_id": task.name},
                    )
                if current.get("status") in TERMINAL_STATES:
                    return current
                candidate = updater(dict(current))
                self._validate_manifest_identity(task, candidate)
                if candidate == current:
                    return current
                self._write_manifest_unlocked(task, candidate)
                return candidate

    def read_prompt(self, task_dir: Path) -> str:
        with self._existing_task_dir(task_dir) as task:
            try:
                return self._read_state_text(task, "prompt.txt")
            except FileNotFoundError as error:
                raise WorkerError(
                    "task_not_found",
                    f"Prompt not found for task: {task.name}",
                    {"task_id": task.name},
                ) from error
            except (OSError, UnicodeError) as error:
                raise WorkerError(
                    "invalid_state",
                    f"Could not read prompt for task: {task.name}",
                    {"task_id": task.name},
                ) from error

    def write_prompt(self, task_dir: Path, prompt: str) -> None:
        with self._existing_task_dir(task_dir) as task:
            self._write_atomic_text(task, "prompt.txt", prompt)

    def _directory_has_manifest(
        self,
        root: _StateRoot,
        entry_name: str,
        entry_identity: tuple[int, int, int],
    ) -> bool:
        self._validate_root(root)
        file_descriptor: int | None = None
        try:
            if root.file_descriptor is not None:
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                flags |= getattr(os, "O_NOFOLLOW", 0)
                flags |= getattr(os, "O_CLOEXEC", 0)
                file_descriptor = os.open(
                    entry_name,
                    flags,
                    dir_fd=root.file_descriptor,
                )
                opened = os.fstat(file_descriptor)
                if (
                    _stat_identity(opened) != entry_identity
                    or not stat.S_ISDIR(opened.st_mode)
                    or _is_reparse_point(opened)
                ):
                    raise WorkerError(
                        "invalid_state",
                        f"State entry changed while opening: {entry_name}",
                        {"entry": str(root.path / entry_name)},
                    )
                self._validate_root(root)
                try:
                    os.stat(
                        "manifest.json",
                        dir_fd=file_descriptor,
                        follow_symlinks=False,
                    )
                except (FileNotFoundError, NotADirectoryError):
                    self._validate_root(root)
                    return False
                self._validate_root(root)
                return True

            entry_path = root.path / entry_name
            self._validate_root(root)
            before = entry_path.lstat()
            if (
                _stat_identity(before) != entry_identity
                or not stat.S_ISDIR(before.st_mode)
                or stat.S_ISLNK(before.st_mode)
                or _is_reparse_point(before)
            ):
                raise WorkerError(
                    "invalid_state",
                    f"State entry changed while inspecting: {entry_name}",
                    {"entry": str(entry_path)},
                )
            try:
                (entry_path / "manifest.json").lstat()
            except (FileNotFoundError, NotADirectoryError):
                self._validate_root(root)
                return False
            after = entry_path.lstat()
            if _stat_identity(after) != entry_identity:
                raise WorkerError(
                    "invalid_state",
                    f"State entry changed while inspecting: {entry_name}",
                    {"entry": str(entry_path)},
                )
            self._validate_root(root)
            return True
        finally:
            _close_descriptor(file_descriptor)

    def list_manifests(self) -> list[dict[str, object]]:
        root = self._existing_root(missing_ok=True)
        if root is None:
            return []
        try:
            with root:
                if root.file_descriptor is not None:
                    with os.scandir(root.file_descriptor) as iterator:
                        entry_names = sorted(entry.name for entry in iterator)
                else:
                    with os.scandir(root.path) as iterator:
                        entry_names = sorted(entry.name for entry in iterator)
                self._validate_root(root)

                manifests: list[dict[str, object]] = []
                for entry_name in entry_names:
                    try:
                        entry_stat = self._task_entry_lstat(root, entry_name)
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                    if stat.S_ISLNK(entry_stat.st_mode) or _is_reparse_point(
                        entry_stat
                    ):
                        raise WorkerError(
                            "invalid_state",
                            f"State entry cannot be a link: {entry_name}",
                            {"entry": str(root.path / entry_name)},
                        )
                    if not stat.S_ISDIR(entry_stat.st_mode):
                        continue
                    if not valid_task_id(entry_name):
                        if not self._directory_has_manifest(
                            root,
                            entry_name,
                            _stat_identity(entry_stat),
                        ):
                            continue
                        raise WorkerError(
                            "invalid_state",
                            f"Manifest belongs to an invalid task directory: {entry_name}",
                            {"task_id": entry_name},
                        )
                    try:
                        task_root = self._clone_root(root)
                        with self._existing_task_dir(
                            root.path / entry_name,
                            root=task_root,
                            expected_identity=_stat_identity(entry_stat),
                        ) as task:
                            try:
                                self._state_lstat(task, "manifest.json")
                            except (FileNotFoundError, NotADirectoryError):
                                continue
                            manifests.append(self._read_manifest(task))
                    except WorkerError as error:
                        if error.code in {"task_not_found", "invalid_task_id"}:
                            raise WorkerError(
                                "invalid_state",
                                f"Manifest disappeared for task: {entry_name}",
                                {"task_id": entry_name},
                            ) from error
                        raise
                    self._validate_root(root)
        except OSError as error:
            raise WorkerError(
                "invalid_state",
                f"Could not list state root: {self.root}",
                {"root": str(self.root)},
            ) from error

        manifests.sort(key=lambda manifest: str(manifest.get("task_id", "")))
        return manifests
