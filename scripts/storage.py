"""Bounded, fail-closed relay file storage using only the standard library.

Callers serialize read/modify/commit with ``locked``. Path and inode checks
protect against accidental links and detectable replacement races; this is not
an adversarial same-UID security boundary. Directory fsync is best effort on
platforms that cannot provide it, and warnings never undo an already committed
atomic replacement.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path


class Error(RuntimeError):
    pass


def _absolute(path: Path) -> Path:
    path = Path(path).absolute()
    if ".." in path.parts:
        raise Error("parent traversal is not allowed")
    return path


def _identity(info):
    return info.st_dev, info.st_ino


def _validate(info, directory=False):
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
        raise Error("symlink or reparse point is not allowed")
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise Error("ancestor is not a directory")
    elif not stat.S_ISDIR(info.st_mode):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise Error("file must be regular and have exactly one link")


def _check(path: Path, *, missing=False):
    """Check each existing component without resolving away child links."""
    path = _absolute(path)
    cursor = Path(path.anchor)
    info = cursor.lstat()
    _validate(info, directory=True)
    for index, part in enumerate(path.parts[1:]):
        cursor /= part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise
        _validate(info, directory=index < len(path.parts) - 2)
    return info


def _regular(info):
    if info is None or not stat.S_ISREG(info.st_mode):
        raise Error("expected a regular file")
    _validate(info)


def root_path(raw) -> Path:
    try:
        path = Path(raw).expanduser().resolve(strict=True)
        if not path.is_dir():
            raise Error("project root is not a directory")
        return path
    except (OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, Error):
            raise
        raise Error("invalid project root") from exc


def child(root: Path, *parts: str, exists=False) -> Path:
    try:
        root = _absolute(root)
        path = root
        for part in parts:
            item = Path(part)
            if item.is_absolute() or ".." in item.parts:
                raise Error("child path escapes project root")
            path /= item
        path.relative_to(root)
        _check(path, missing=not exists)
        return path
    except (OSError, ValueError) as exc:
        raise Error("invalid child path") from exc


def _flags(base):
    return base | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


def _check_open(path, fd, before):
    opened = os.fstat(fd)
    _regular(opened)
    after = _check(path)
    _regular(after)
    if _identity(opened) != _identity(after) or (before is not None and _identity(before) != _identity(opened)):
        raise Error("file changed while opening or acquiring lock")
    return opened


def read(path: Path, max_bytes=65536) -> str:
    fd = None
    try:
        if not isinstance(max_bytes, int) or max_bytes < 0:
            raise Error("max_bytes must be a nonnegative integer")
        path = _absolute(path)
        before = _check(path)
        _regular(before)
        fd = os.open(path, _flags(os.O_RDONLY))
        opened = _check_open(path, fd, before)
        if opened.st_size > max_bytes:
            raise Error(f"file exceeds {max_bytes} bytes")
        chunks, size = [], 0
        while size <= max_bytes:
            chunk = os.read(fd, min(8192, max_bytes + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > max_bytes:
            raise Error(f"file exceeds {max_bytes} bytes")
        after = _check_open(path, fd, before)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Error("file changed while reading")
        return b"".join(chunks).decode("utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        raise Error("cannot read relay file") from exc
    finally:
        if fd is not None:
            os.close(fd)


def _same_target(path, expected):
    actual = _check(path, missing=True)
    if actual is not None:
        _regular(actual)
    if (actual is None) != (expected is None) or (actual is not None and _identity(actual) != _identity(expected)):
        raise Error("target changed before replacement")


def atomic(path: Path, document: str) -> list[str]:
    temporary = None
    parent_fd = None
    committed = False
    warnings = []
    try:
        path = _absolute(path)
        payload = document.encode("utf-8")
        before = _check(path, missing=True)
        if before is not None:
            _regular(before)
        parent_before = _check(path.parent)
        _validate(parent_before, directory=True)
        # A directory descriptor anchors POSIX replacement even if a parent is
        # subsequently renamed. Windows uses repeated component/inode checks.
        anchored = os.name != "nt" and os.replace in os.supports_dir_fd
        # CPython exposes rename (not always its replace alias) in supports_dir_fd.
        anchored = anchored or (os.name != "nt" and os.rename in os.supports_dir_fd)
        if os.name != "nt":
            parent_fd = os.open(path.parent, _flags(os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0))
            if _identity(os.fstat(parent_fd)) != _identity(parent_before):
                raise Error("parent directory changed while opening")
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            _check_open(temporary, handle.fileno(), None)
            if _identity(_check(path.parent)) != _identity(parent_before):
                raise Error("parent directory changed before writing")
            if before is not None:
                if hasattr(os, "fchmod"):
                    os.fchmod(handle.fileno(), stat.S_IMODE(before.st_mode))
                else:
                    os.chmod(temporary, stat.S_IMODE(before.st_mode))
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _same_target(path, before)
        if _identity(_check(path.parent)) != _identity(parent_before):
            raise Error("parent directory changed before replacement")
        _regular(_check(temporary))
        if anchored:
            os.replace(temporary.name, path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        else:
            os.replace(temporary, path)
        committed = True
        temporary = None
        try:
            if parent_fd is None:
                warnings.append("durability: parent directory fsync is unavailable on this platform")
            else:
                os.fsync(parent_fd)
        except OSError as exc:
            warnings.append("durability: replacement committed but parent fsync failed")
        return warnings
    except (OSError, UnicodeError, ValueError) as exc:
        raise Error("atomic write failed before commit") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                # Do not replace the primary precommit failure with cleanup failure.
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError as exc:
                if committed:
                    warnings.append("durability: replacement committed but directory close failed")


def immutable(path: Path, document: str) -> list[str]:
    """Create *path* exactly once, or verify an identical existing file.

    Receipt segments are content addressed.  Unlike ``atomic`` this helper
    never replaces an existing target, which makes retries and races safe.
    """
    temporary = None
    parent_fd = None
    try:
        path = _absolute(path)
        payload = document.encode("utf-8")
        parent_before = _check(path.parent)
        _validate(parent_before, directory=True)
        existing = _check(path, missing=True)
        if existing is not None:
            _regular(existing)
            if read(path, max_bytes=max(len(payload), 1)) != document:
                raise Error("immutable file conflicts with existing content")
            return []
        if os.name != "nt":
            parent_fd = os.open(path.parent, _flags(os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0))
            if _identity(os.fstat(parent_fd)) != _identity(parent_before):
                raise Error("parent directory changed while opening")
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw)
        with os.fdopen(fd, "wb") as handle:
            _check_open(temporary, handle.fileno(), None)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if _identity(_check(path.parent)) != _identity(parent_before):
            raise Error("parent directory changed before publishing")
        try:
            # Hard-link publication is exclusive on POSIX and Windows NTFS;
            # a competing creator therefore cannot be overwritten.
            os.link(temporary, path)
        except FileExistsError:
            if read(path, max_bytes=max(len(payload), 1)) != document:
                raise Error("immutable file conflicts with existing content")
            return []
        temporary.unlink()
        temporary = None
        warnings = []
        try:
            if parent_fd is None:
                warnings.append("durability: parent directory fsync is unavailable on this platform")
            else:
                os.fsync(parent_fd)
        except OSError:
            warnings.append("durability: immutable file committed but parent fsync failed")
        return warnings
    except (OSError, UnicodeError, ValueError) as exc:
        raise Error("immutable write failed before commit") from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


@contextmanager
def locked(path: Path):
    fd = None
    acquired = False
    try:
        path = _absolute(path)
        before = _check(path, missing=True)
        if before is not None:
            _regular(before)
        fd = os.open(path, _flags(os.O_CREAT | os.O_RDWR), 0o600)
        _check_open(path, fd, before)
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            # LK_LOCK retries contention once per second, up to ten attempts;
            # exhaustion fails closed. A caller may retry the whole operation
            # with the same operation ID after the competing writer finishes.
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX)
        acquired = True
        _check_open(path, fd, before)
    except (OSError, ValueError) as exc:
        if fd is not None:
            os.close(fd)
            fd = None
        raise Error("cannot acquire relay lock") from exc
    except BaseException:
        if fd is not None:
            os.close(fd)
            fd = None
        raise
    try:
        yield
    finally:
        if fd is not None:
            try:
                if acquired:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def commit(current: Path, history: Path, old_text: str, new_text: str, old_revision: int) -> dict:
    """Archive the previous committed bytes, then replace CURRENT under a caller lock."""
    try:
        current, history = _absolute(current), _absolute(history)
        if not isinstance(old_revision, int) or isinstance(old_revision, bool) or old_revision < 0:
            raise Error("old_revision must be a nonnegative integer")
        if read(current) != old_text:
            raise Error("CURRENT changed since it was read")
        existing = _check(history, missing=True)
        if existing is not None:
            _validate(existing, directory=True)
        else:
            history.mkdir(mode=0o700)
            _validate(_check(history), directory=True)
        digest = hashlib.sha256(old_text.encode("utf-8")).hexdigest()
        snapshot = child(history, f"r{old_revision}-{digest}.md")
        warnings = []
        if snapshot.exists():
            if read(snapshot) != old_text:
                raise Error("immutable history snapshot conflicts with committed document")
        else:
            warnings.extend(atomic(snapshot, old_text))
        # Verify CURRENT again after snapshot I/O before replacing it.
        if read(current) != old_text:
            raise Error("CURRENT changed while archiving history")
        warnings.extend(atomic(current, new_text))
        return {"committed": True, "history": str(snapshot), "warnings": warnings}
    except (OSError, UnicodeError, ValueError) as exc:
        raise Error("cannot commit relay state") from exc
