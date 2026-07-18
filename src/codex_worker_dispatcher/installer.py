from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sysconfig
import uuid

from codex_worker_dispatcher import __version__
from codex_worker_dispatcher.errors import WorkerError


MARKER_NAME = ".codex-worker-dispatcher.json"
_MARKER_OWNER = "codex-worker-dispatcher"
_SKILL_NAME = "dispatching-codex-workers"
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)
_MAX_MARKER_BYTES = 64 * 1024


def default_skill_target() -> Path:
    return Path.home() / ".agents" / "skills" / _SKILL_NAME


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise WorkerError(
            "invalid_arguments",
            "Could not inspect the skill target",
            {"target": str(path), "error": str(error)},
        ) from error
    return True


def _stat_identity(result: os.stat_result) -> tuple[int, int, int]:
    return result.st_dev, result.st_ino, result.st_mode


def _is_reparse_point(result: os.stat_result) -> bool:
    return bool(
        getattr(result, "st_file_attributes", 0)
        & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _safe_directory_stat(
    path: Path,
    *,
    code: str,
    message: str,
) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as error:
        raise WorkerError(
            code,
            message,
            {"path": str(path), "error": str(error)},
        ) from error
    if (
        not stat.S_ISDIR(result.st_mode)
        or stat.S_ISLNK(result.st_mode)
        or _is_reparse_point(result)
    ):
        raise WorkerError(code, message, {"path": str(path)})
    return result


def _safe_tree_entries(
    directory: Path,
    *,
    code: str,
    message: str,
) -> tuple[os.stat_result, tuple[tuple[str, Path, os.stat_result], ...]]:
    root = _safe_directory_stat(directory, code=code, message=message)
    root_identity = _stat_identity(root)
    entries: list[tuple[str, Path, os.stat_result]] = []

    def visit(current: Path) -> None:
        current_before = _safe_directory_stat(current, code=code, message=message)
        current_identity = _stat_identity(current_before)
        try:
            with os.scandir(current) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise WorkerError(
                code,
                message,
                {"path": str(current), "error": str(error)},
            ) from error
        for child in children:
            child_path = current / child.name
            try:
                child_stat = child_path.lstat()
            except OSError as error:
                raise WorkerError(
                    code,
                    message,
                    {"path": str(child_path), "error": str(error)},
                ) from error
            if stat.S_ISLNK(child_stat.st_mode) or _is_reparse_point(child_stat):
                raise WorkerError(code, message, {"path": str(child_path)})
            relative = child_path.relative_to(directory).as_posix()
            if stat.S_ISDIR(child_stat.st_mode):
                entries.append((relative, child_path, child_stat))
                visit(child_path)
            elif stat.S_ISREG(child_stat.st_mode):
                entries.append((relative, child_path, child_stat))
            else:
                raise WorkerError(code, message, {"path": str(child_path)})
        current_after = _safe_directory_stat(current, code=code, message=message)
        if _stat_identity(current_after) != current_identity:
            raise WorkerError(code, message, {"path": str(current)})

    visit(directory)
    root_after = _safe_directory_stat(directory, code=code, message=message)
    if _stat_identity(root_after) != root_identity:
        raise WorkerError(code, message, {"path": str(directory)})
    return root, tuple(entries)


def _safe_file_bytes(
    path: Path,
    before: os.stat_result,
    *,
    require_single_link: bool,
    max_bytes: int | None = None,
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            _stat_identity(opened) != _stat_identity(before)
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(opened.st_mode)
            or _is_reparse_point(opened)
            or (require_single_link and opened.st_nlink != 1)
        ):
            raise OSError("file changed while opening")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            content = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(content) > max_bytes:
            raise OSError("file is too large")
        after = path.lstat()
        if (
            _stat_identity(after) != _stat_identity(before)
            or not stat.S_ISREG(after.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or _is_reparse_point(after)
            or (require_single_link and after.st_nlink != 1)
            or after.st_size != opened.st_size
            or getattr(after, "st_mtime_ns", None)
            != getattr(opened, "st_mtime_ns", None)
        ):
            raise OSError("file changed while reading")
        return content
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _bundled_skill_source() -> Path:
    data_root = sysconfig.get_path("data")
    candidates: list[Path] = []
    if data_root:
        candidates.append(
            Path(data_root)
            / "share"
            / "codex-worker-dispatcher"
            / "skill"
            / _SKILL_NAME
        )
    candidates.append(Path(__file__).resolve().parents[2] / "skill" / _SKILL_NAME)

    for candidate in candidates:
        if candidate.is_dir() and (candidate / "SKILL.md").is_file():
            return _absolute_path(candidate)
    raise WorkerError(
        "skill_source_not_found",
        "The bundled worker skill could not be located",
        {"searched": [str(candidate) for candidate in candidates]},
    )


def _validate_source(source: Path) -> None:
    try:
        _, entries = _safe_tree_entries(
            source,
            code="skill_source_invalid",
            message="The bundled worker skill must not contain links or reparse points",
        )
    except WorkerError as error:
        if not _path_exists(source):
            raise WorkerError(
                "skill_source_not_found",
                "The bundled worker skill is incomplete",
                {"source": str(source)},
            ) from error
        raise
    skill_entries = {
        relative: result
        for relative, _path, result in entries
        if relative == "SKILL.md"
    }
    if "SKILL.md" not in skill_entries or not stat.S_ISREG(
        skill_entries["SKILL.md"].st_mode
    ):
        raise WorkerError(
            "skill_source_not_found",
            "The bundled worker skill is incomplete",
            {"source": str(source)},
        )


def _marker(source: Path) -> dict[str, str | int]:
    return {
        "schema_version": 1,
        "owner": _MARKER_OWNER,
        "version": __version__,
        "source": str(source),
    }


def _write_marker(
    directory: Path,
    marker: dict[str, str | int],
    *,
    expected_directory_identity: tuple[int, int, int],
) -> None:
    marker_path = directory / MARKER_NAME
    directory_before = _safe_directory_stat(
        directory,
        code="install_failed",
        message="The staged skill directory is unsafe while creating its marker",
    )
    directory_identity = _stat_identity(directory_before)
    if directory_identity != expected_directory_identity:
        raise WorkerError(
            "install_failed",
            "The staged skill directory changed before creating its marker",
            {"staging": str(directory), "marker": str(marker_path)},
        )
    payload = (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(marker_path, flags, 0o600)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(opened.st_mode)
            or _is_reparse_point(opened)
            or opened.st_nlink != 1
        ):
            raise OSError("ownership marker is not a private regular file")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("ownership marker write made no progress")
            offset += written
        os.fsync(descriptor)
        written_stat = os.fstat(descriptor)
        marker_entry = marker_path.lstat()
        if (
            _stat_identity(written_stat) != _stat_identity(opened)
            or _stat_identity(marker_entry) != _stat_identity(opened)
            or not stat.S_ISREG(marker_entry.st_mode)
            or stat.S_ISLNK(marker_entry.st_mode)
            or _is_reparse_point(marker_entry)
            or written_stat.st_nlink != 1
            or marker_entry.st_nlink != 1
            or written_stat.st_size != len(payload)
            or marker_entry.st_size != len(payload)
        ):
            raise OSError("ownership marker changed while it was being written")
        directory_after = _safe_directory_stat(
            directory,
            code="install_failed",
            message="The staged skill directory changed while creating its marker",
        )
        if _stat_identity(directory_after) != directory_identity:
            raise OSError("staged skill directory changed while creating its marker")
    except (OSError, WorkerError) as error:
        raise WorkerError(
            "install_failed",
            "Could not safely create the skill ownership marker",
            {"staging": str(directory), "marker": str(marker_path), "error": str(error)},
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_owned_marker(directory: Path) -> dict[str, object] | None:
    marker_path = directory / MARKER_NAME
    try:
        directory_before = _safe_directory_stat(
            directory,
            code="uninstall_refused",
            message="The skill directory is not a safe ordinary directory",
        )
        marker_before = marker_path.lstat()
    except (OSError, WorkerError):
        return None
    if (
        not stat.S_ISREG(marker_before.st_mode)
        or stat.S_ISLNK(marker_before.st_mode)
        or _is_reparse_point(marker_before)
        or marker_before.st_nlink != 1
    ):
        return None
    try:
        marker_bytes = _safe_file_bytes(
            marker_path,
            marker_before,
            require_single_link=True,
            max_bytes=_MAX_MARKER_BYTES,
        )
        directory_after = _safe_directory_stat(
            directory,
            code="uninstall_refused",
            message="The skill directory changed while reading its ownership marker",
        )
        if _stat_identity(directory_after) != _stat_identity(directory_before):
            return None
        value = json.loads(marker_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, WorkerError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema_version") != 1 or value.get("owner") != _MARKER_OWNER:
        return None
    if not isinstance(value.get("version"), str) or not isinstance(value.get("source"), str):
        return None
    return value


def _tree_snapshot(directory: Path, *, ignore_marker: bool) -> tuple[tuple[str, str], ...]:
    _, safe_entries = _safe_tree_entries(
        directory,
        code="unsafe_target",
        message="The skill tree must not contain links or reparse points",
    )
    entries: list[tuple[str, str]] = []
    for relative, entry, entry_stat in safe_entries:
        if ignore_marker and relative == MARKER_NAME:
            continue
        if stat.S_ISDIR(entry_stat.st_mode):
            entries.append((relative, "directory"))
        elif stat.S_ISREG(entry_stat.st_mode):
            digest = hashlib.sha256(
                _safe_file_bytes(
                    entry,
                    entry_stat,
                    require_single_link=False,
                )
            ).hexdigest()
            entries.append((relative, f"file:{digest}"))
        else:
            entries.append((relative, "other"))
    return tuple(entries)


def _is_identical_install(destination: Path, source: Path) -> bool:
    try:
        _safe_directory_stat(
            destination,
            code="unsafe_target",
            message="The skill target is not a safe ordinary directory",
        )
        if _read_owned_marker(destination) != _marker(source):
            return False
        return _tree_snapshot(destination, ignore_marker=True) == _tree_snapshot(
            source,
            ignore_marker=False,
        )
    except (OSError, UnicodeError, WorkerError):
        return False


def _capture_tree_snapshot(
    directory: Path,
    *,
    code: str,
    message: str,
) -> tuple[tuple[int, int, int], tuple[tuple[str, str], ...]]:
    try:
        before = _safe_directory_stat(
            directory,
            code=code,
            message=message,
        )
        identity = _stat_identity(before)
        snapshot = _tree_snapshot(directory, ignore_marker=False)
        after = _safe_directory_stat(
            directory,
            code=code,
            message=message,
        )
        if _stat_identity(after) != identity:
            raise WorkerError(code, message, {"path": str(directory)})
        return identity, snapshot
    except (OSError, UnicodeError, WorkerError) as error:
        if isinstance(error, WorkerError) and error.code == code:
            raise
        raise WorkerError(
            code,
            message,
            {"path": str(directory), "error": str(error)},
        ) from error


def _require_tree_snapshot(
    directory: Path,
    *,
    expected_identity: tuple[int, int, int],
    expected_snapshot: tuple[tuple[str, str], ...],
    code: str,
    message: str,
) -> None:
    current_identity, current_snapshot = _capture_tree_snapshot(
        directory,
        code=code,
        message=message,
    )
    if (
        current_identity != expected_identity
        or current_snapshot != expected_snapshot
    ):
        raise WorkerError(code, message, {"path": str(directory)})


def _backup_path(destination: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = destination.with_name(f"{destination.name}.backup-{timestamp}")
    if not _path_exists(candidate):
        return candidate
    return destination.with_name(
        f"{destination.name}.backup-{timestamp}-{uuid.uuid4().hex}"
    )


def _safe_existing_target_stat(path: Path) -> os.stat_result:
    try:
        result = path.lstat()
    except OSError as error:
        raise WorkerError(
            "unsafe_target",
            "Could not safely inspect the existing skill target",
            {"target": str(path), "error": str(error)},
        ) from error
    if stat.S_ISLNK(result.st_mode) or _is_reparse_point(result):
        raise WorkerError(
            "unsafe_target",
            "The skill target must not be a link or reparse point",
            {"target": str(path)},
        )
    if stat.S_ISDIR(result.st_mode):
        checked, _entries = _safe_tree_entries(
            path,
            code="unsafe_target",
            message="The skill target must not contain links or reparse points",
        )
        return checked
    if stat.S_ISREG(result.st_mode):
        return result
    raise WorkerError(
        "unsafe_target",
        "The skill target is not an ordinary file or directory",
        {"target": str(path)},
    )


def _remove_verified_directory(
    path: Path,
    *,
    expected_identity: tuple[int, int, int],
    tree_root: Path,
    expected_entries: dict[str, tuple[int, int, int]],
) -> None:
    current = _safe_directory_stat(
        path,
        code="unsafe_target",
        message="Refusing to remove a directory whose identity is unsafe",
    )
    if _stat_identity(current) != expected_identity:
        raise WorkerError(
            "unsafe_target",
            "Refusing to remove a directory whose identity changed",
            {"path": str(path)},
        )

    try:
        with os.scandir(path) as iterator:
            child_names = sorted(child.name for child in iterator)
    except OSError as error:
        raise WorkerError(
            "unsafe_target",
            "Could not enumerate a directory selected for safe removal",
            {"path": str(path), "error": str(error)},
        ) from error

    after_scan = _safe_directory_stat(
        path,
        code="unsafe_target",
        message="The directory changed while preparing safe removal",
    )
    if _stat_identity(after_scan) != expected_identity:
        raise WorkerError(
            "unsafe_target",
            "The directory changed while preparing safe removal",
            {"path": str(path)},
        )

    relative_directory = path.relative_to(tree_root).as_posix()
    if relative_directory == ".":
        relative_directory = ""
    expected_children: dict[str, tuple[int, int, int]] = {}
    for relative, identity in expected_entries.items():
        parent, _separator, name = relative.rpartition("/")
        if parent == relative_directory:
            expected_children[name] = identity
    if child_names != sorted(expected_children):
        raise WorkerError(
            "unsafe_target",
            "The directory entries changed after recursive-removal preflight",
            {"path": str(path)},
        )

    for child_name in child_names:
        child_path = path / child_name
        try:
            child = child_path.lstat()
        except OSError as error:
            raise WorkerError(
                "unsafe_target",
                "Could not inspect an entry selected for safe removal",
                {"path": str(child_path), "error": str(error)},
            ) from error
        child_identity = _stat_identity(child)
        if child_identity != expected_children[child_name]:
            raise WorkerError(
                "unsafe_target",
                "Refusing to remove an entry whose identity changed after preflight",
                {"path": str(child_path)},
            )
        if stat.S_ISLNK(child.st_mode) or _is_reparse_point(child):
            raise WorkerError(
                "unsafe_target",
                "Refusing to remove a link or reparse point",
                {"path": str(child_path)},
            )
        if stat.S_ISDIR(child.st_mode):
            _remove_verified_directory(
                child_path,
                expected_identity=child_identity,
                tree_root=tree_root,
                expected_entries=expected_entries,
            )
        elif stat.S_ISREG(child.st_mode):
            before_unlink = child_path.lstat()
            if (
                _stat_identity(before_unlink) != child_identity
                or not stat.S_ISREG(before_unlink.st_mode)
                or stat.S_ISLNK(before_unlink.st_mode)
                or _is_reparse_point(before_unlink)
            ):
                raise WorkerError(
                    "unsafe_target",
                    "Refusing to remove a file whose identity changed",
                    {"path": str(child_path)},
                )
            os.unlink(child_path)
        else:
            raise WorkerError(
                "unsafe_target",
                "Refusing to remove a non-regular filesystem entry",
                {"path": str(child_path)},
            )

    before_rmdir = _safe_directory_stat(
        path,
        code="unsafe_target",
        message="The directory changed before safe removal",
    )
    if _stat_identity(before_rmdir) != expected_identity:
        raise WorkerError(
            "unsafe_target",
            "The directory changed before safe removal",
            {"path": str(path)},
        )
    os.rmdir(path)


def _safe_rmtree(
    path: Path,
    *,
    expected_identity: tuple[int, int, int],
    expected_marker: dict[str, object] | None = None,
) -> None:
    current, entries = _safe_tree_entries(
        path,
        code="unsafe_target",
        message="Refusing to recursively remove an unsafe directory tree",
    )
    if _stat_identity(current) != expected_identity:
        raise WorkerError(
            "unsafe_target",
            "Refusing to recursively remove a directory whose identity changed",
            {"path": str(path)},
        )
    if expected_marker is not None and _read_owned_marker(path) != expected_marker:
        raise WorkerError(
            "unsafe_target",
            "Refusing to recursively remove a directory whose ownership marker changed",
            {"path": str(path), "marker": MARKER_NAME},
        )
    before_remove = _safe_directory_stat(
        path,
        code="unsafe_target",
        message="The directory changed before recursive removal",
    )
    if _stat_identity(before_remove) != expected_identity:
        raise WorkerError(
            "unsafe_target",
            "The directory changed before recursive removal",
            {"path": str(path)},
        )
    expected_entries = {
        relative: _stat_identity(result)
        for relative, _entry_path, result in entries
    }
    _remove_verified_directory(
        path,
        expected_identity=expected_identity,
        tree_root=path,
        expected_entries=expected_entries,
    )


def _remove_staging(
    path: Path,
    *,
    expected_identity: tuple[int, int, int],
) -> None:
    if not _path_exists(path):
        return
    _safe_rmtree(path, expected_identity=expected_identity)


def _restore_posix_staging_mode(
    path: Path,
    *,
    expected_identity: tuple[int, int, int],
) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        flags |= getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if (
            opened.st_dev != expected_identity[0]
            or opened.st_ino != expected_identity[1]
            or not stat.S_ISDIR(opened.st_mode)
            or _is_reparse_point(opened)
        ):
            raise OSError("staging directory changed while restoring its mode")
        os.fchmod(descriptor, stat.S_IMODE(expected_identity[2]))
        restored = os.fstat(descriptor)
        entry = path.lstat()
        if (
            _stat_identity(restored) != expected_identity
            or _stat_identity(entry) != expected_identity
            or not stat.S_ISDIR(entry.st_mode)
            or stat.S_ISLNK(entry.st_mode)
            or _is_reparse_point(entry)
        ):
            raise OSError("staging directory mode or identity could not be restored")
    except OSError as error:
        raise WorkerError(
            "install_failed",
            "Could not preserve the private staging directory mode",
            {"staging": str(path), "error": str(error)},
        ) from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def install_skill(
    target: Path | None = None,
    *,
    upgrade: bool = False,
) -> dict[str, object]:
    destination = _absolute_path(target if target is not None else default_skill_target())
    source = _absolute_path(_bundled_skill_source())
    _validate_source(source)
    source_identity, source_snapshot = _capture_tree_snapshot(
        source,
        code="skill_source_invalid",
        message="The bundled worker skill changed during validation",
    )
    source_marker = _marker(source)

    if destination == destination.parent:
        raise WorkerError(
            "invalid_arguments",
            "The skill target cannot be a filesystem root",
            {"target": str(destination)},
        )

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise WorkerError(
            "install_failed",
            "Could not create the skill target parent directory",
            {"target": str(destination), "error": str(error)},
        ) from error

    destination_exists = _path_exists(destination)
    destination_identity: tuple[int, int, int] | None = None
    if destination_exists:
        destination_identity = _stat_identity(_safe_existing_target_stat(destination))
    if destination_exists and _is_identical_install(destination, source):
        return {
            "action": "unchanged",
            "target": str(destination),
            "version": __version__,
        }
    if destination_exists and not upgrade:
        raise WorkerError(
            "install_conflict",
            "The skill target already exists and differs from the bundled skill",
            {"target": str(destination), "hint": "Re-run with --upgrade to keep a backup"},
        )

    backup: Path | None = None
    staging = destination.parent / f".{destination.name}.install-{uuid.uuid4().hex}"
    staging_identity: tuple[int, int, int] | None = None
    try:
        if destination_exists:
            current_destination = _safe_existing_target_stat(destination)
            if _stat_identity(current_destination) != destination_identity:
                raise WorkerError(
                    "unsafe_target",
                    "The skill target changed before it could be backed up",
                    {"target": str(destination)},
                )
            backup = _backup_path(destination)
            os.rename(destination, backup)
            moved_destination = _safe_existing_target_stat(backup)
            if _stat_identity(moved_destination) != destination_identity:
                raise WorkerError(
                    "unsafe_target",
                    "The skill target changed while it was being backed up",
                    {"target": str(destination), "backup": str(backup)},
                )
        staging.mkdir(mode=0o700)
        staging_stat = _safe_directory_stat(
            staging,
            code="install_failed",
            message="The staging directory is not a safe ordinary directory",
        )
        staging_identity = _stat_identity(staging_stat)
        _require_tree_snapshot(
            source,
            expected_identity=source_identity,
            expected_snapshot=source_snapshot,
            code="install_failed",
            message="The bundled worker skill changed before copying",
        )
        shutil.copytree(source, staging, dirs_exist_ok=True)
        _restore_posix_staging_mode(
            staging,
            expected_identity=staging_identity,
        )
        _require_tree_snapshot(
            source,
            expected_identity=source_identity,
            expected_snapshot=source_snapshot,
            code="install_failed",
            message="The bundled worker skill changed while copying",
        )
        copied_identity, copied_snapshot = _capture_tree_snapshot(
            staging,
            code="install_failed",
            message="The staged skill copy is not a safe ordinary directory tree",
        )
        if copied_identity != staging_identity or copied_snapshot != source_snapshot:
            raise WorkerError(
                "install_failed",
                "The staged skill copy does not match its validated source",
                {"target": str(destination), "staging": str(staging)},
            )
        _write_marker(
            staging,
            source_marker,
            expected_directory_identity=staging_identity,
        )
        staged_after_marker, _entries = _safe_tree_entries(
            staging,
            code="install_failed",
            message="The staged skill copy changed before installation",
        )
        if _stat_identity(staged_after_marker) != staging_identity:
            raise WorkerError(
                "install_failed",
                "The staged skill directory changed before installation",
                {"target": str(destination), "staging": str(staging)},
            )
        if _path_exists(destination):
            raise OSError("skill target appeared while installation was in progress")
        os.rename(staging, destination)
        installed = _safe_existing_target_stat(destination)
        if (
            _stat_identity(installed) != staging_identity
            or _read_owned_marker(destination) != source_marker
            or _tree_snapshot(destination, ignore_marker=True) != source_snapshot
        ):
            raise WorkerError(
                "install_failed",
                "The installed skill target changed during its final rename",
                {"target": str(destination)},
            )
    except Exception as error:
        rollback_errors: list[Exception] = []
        backup_restored = False
        if staging_identity is not None:
            try:
                _remove_staging(staging, expected_identity=staging_identity)
            except Exception as caught:
                rollback_errors.append(caught)
        try:
            if (
                backup is not None
                and _path_exists(backup)
                and not _path_exists(destination)
            ):
                current_backup = _safe_existing_target_stat(backup)
                if (
                    destination_identity is None
                    or _stat_identity(current_backup) != destination_identity
                ):
                    raise WorkerError(
                        "unsafe_target",
                        "The skill backup changed before rollback",
                        {"target": str(destination), "backup": str(backup)},
                    )
                os.rename(backup, destination)
                restored = _safe_existing_target_stat(destination)
                if _stat_identity(restored) != destination_identity:
                    raise WorkerError(
                        "unsafe_target",
                        "The original skill target could not be verified after rollback",
                        {"target": str(destination), "backup": str(backup)},
                    )
                backup_restored = True
        except Exception as caught:
            rollback_errors.append(caught)
        details: dict[str, object] = {
            "target": str(destination),
            "error": str(error),
        }
        if backup is not None:
            details["backup"] = str(backup)
            details["backup_restored"] = backup_restored
        if rollback_errors:
            details["rollback_errors"] = [str(caught) for caught in rollback_errors]
        raise WorkerError(
            "install_failed",
            "Could not install the bundled worker skill",
            details,
        ) from error

    result: dict[str, object] = {
        "action": "upgraded" if backup is not None else "installed",
        "target": str(destination),
        "version": __version__,
    }
    if backup is not None:
        result["backup"] = str(backup)
    return result


def uninstall_skill(target: Path | None = None) -> dict[str, object]:
    destination = _absolute_path(target if target is not None else default_skill_target())
    if destination == destination.parent:
        raise WorkerError(
            "uninstall_refused",
            "Refusing to remove a filesystem root",
            {"target": str(destination)},
        )
    if not _path_exists(destination):
        raise WorkerError(
            "skill_not_installed",
            "The worker skill is not installed at the target",
            {"target": str(destination)},
        )
    try:
        destination_stat, _entries = _safe_tree_entries(
            destination,
            code="uninstall_refused",
            message="Refusing to remove an unsafe skill directory tree",
        )
    except WorkerError as error:
        raise WorkerError(
            "uninstall_refused",
            "Refusing to remove a directory not owned by codex-worker-dispatcher",
            {"target": str(destination), "marker": MARKER_NAME},
        ) from error
    destination_identity = _stat_identity(destination_stat)
    owned_marker = _read_owned_marker(destination)
    if owned_marker is None:
        raise WorkerError(
            "uninstall_refused",
            "Refusing to remove a directory not owned by codex-worker-dispatcher",
            {"target": str(destination), "marker": MARKER_NAME},
        )

    tombstone = destination.parent / f".{destination.name}.remove-{uuid.uuid4().hex}"
    try:
        os.rename(destination, tombstone)
        moved_stat, _entries = _safe_tree_entries(
            tombstone,
            code="uninstall_refused",
            message="The skill directory changed while preparing its removal",
        )
        if _stat_identity(moved_stat) != destination_identity:
            raise WorkerError(
                "uninstall_refused",
                "The skill directory changed while preparing its removal",
                {"target": str(destination), "tombstone": str(tombstone)},
            )
        if _read_owned_marker(tombstone) != owned_marker:
            raise WorkerError(
                "uninstall_refused",
                "The ownership marker changed while preparing skill removal",
                {
                    "target": str(destination),
                    "tombstone": str(tombstone),
                    "marker": MARKER_NAME,
                },
            )
        _safe_rmtree(
            tombstone,
            expected_identity=destination_identity,
            expected_marker=owned_marker,
        )
    except Exception as error:
        rollback_error: Exception | None = None
        tombstone_restored = False
        try:
            if _path_exists(tombstone) and not _path_exists(destination):
                current_tombstone = _safe_existing_target_stat(tombstone)
                current_identity = _stat_identity(current_tombstone)
                if current_identity != destination_identity:
                    raise WorkerError(
                        "unsafe_target",
                        "The uninstall tombstone identity changed before rollback",
                        {"target": str(destination), "tombstone": str(tombstone)},
                    )
                if _read_owned_marker(tombstone) != owned_marker:
                    raise WorkerError(
                        "unsafe_target",
                        "The uninstall tombstone ownership marker changed before rollback",
                        {
                            "target": str(destination),
                            "tombstone": str(tombstone),
                            "marker": MARKER_NAME,
                        },
                    )
                os.rename(tombstone, destination)
                restored = _safe_existing_target_stat(destination)
                if (
                    _stat_identity(restored) != destination_identity
                    or _read_owned_marker(destination) != owned_marker
                ):
                    raise WorkerError(
                        "unsafe_target",
                        "The retained skill directory changed during rollback",
                        {"target": str(destination), "tombstone": str(tombstone)},
                    )
                tombstone_restored = True
        except Exception as caught:
            rollback_error = caught
        details: dict[str, object] = {
            "target": str(destination),
            "error": str(error),
            "tombstone_restored": tombstone_restored,
        }
        if rollback_error is not None:
            details["rollback_error"] = str(rollback_error)
        if _path_exists(tombstone):
            details["tombstone"] = str(tombstone)
        raise WorkerError(
            "uninstall_failed",
            "Could not remove the installed worker skill",
            details,
        ) from error

    return {"action": "uninstalled", "target": str(destination)}


__all__ = [
    "MARKER_NAME",
    "default_skill_target",
    "install_skill",
    "uninstall_skill",
]
