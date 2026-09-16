"""Bounded, fail-closed relay file storage using only the standard library.

Callers serialize read/modify/commit with ``locked``. Path and inode checks
protect against accidental links and detectable replacement races; this is not
an adversarial same-UID security boundary. Directory fsync is best effort on
platforms that cannot provide it, and warnings never undo an already committed
atomic replacement.
"""
from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import tempfile
import time
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


# Bounded, explicit waiting for a publisher that is mid-publication.  The window
# is tiny and self-healing: after the creator unlinks its temporary name the
# target is a single-link file again.
PUBLISH_WAIT_SECONDS = 2.0
PUBLISH_WAIT_SLICE = 0.002
PUBLISH_WAIT_ATTEMPTS = 3


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


def _regular(info):
    if info is None or not stat.S_ISREG(info.st_mode):
        raise Error("expected a regular file")
    _validate(info)


def _final_info(path: Path, missing=False):
    """Validate every ancestor, then lstat the final component unjudged.

    The final entry mode and link count are deliberately *not* validated here
    so callers can tell this protocol's own publication window apart from a
    foreign link.  Symlinked or non-directory ancestors are refused exactly as
    `_check` refuses them.
    """
    path = _absolute(path)
    cursor = Path(path.anchor)
    info = cursor.lstat()
    _validate(info, directory=True)
    parts = path.parts[1:]
    for index, part in enumerate(parts):
        cursor /= part
        last = index == len(parts) - 1
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            if missing:
                return None
            raise
        if not last:
            _validate(info, directory=True)
    return info


def _ls_final(path: Path, missing=False):
    """`_final_info` plus symlink and mode validation, link count aside."""
    info = _final_info(path, missing=missing)
    if info is None:
        return None
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & reparse:
        raise Error("symlink or reparse point is not allowed")
    if not stat.S_ISREG(info.st_mode):
        raise Error("expected a regular file")
    return info


def _require_live_window(path: Path, info, temporary_pattern=None) -> None:
    """Refuse a second link unless it is this protocol's own live window.

    Returns normally only when this protocol's own temporary name holds the extra
    link (same directory, matching `.<name>.*.tmp` pattern or the caller pattern,
    and the same inode), or when that name disappeared between the lstat and the
    directory scan -- the window closing right now, which the caller waits out
    and re-stats.  Every other second link is refused by name: a foreign hard
    link, a mismatched name, a different inode, or a file that was replaced while
    it was being classified (`file changed while opening or acquiring lock`).
    """
    if _publish_window(path, info, temporary_pattern):
        return
    fresh = _ls_final(path, missing=True)
    if fresh is not None and (fresh.st_dev, fresh.st_ino) != (info.st_dev, info.st_ino):
        raise Error("file changed while opening or acquiring lock")
    if fresh is None or fresh.st_nlink == 1:
        return
    if _publish_window(path, fresh, temporary_pattern):
        return
    raise Error("file must be regular and have exactly one link")


def _publish_window(path: Path, info, temporary_pattern=None) -> bool:
    """True when the only extra link belongs to this directory's publish window."""
    if info.st_nlink <= 1:
        return False
    if os.name == "nt":
        return False
    pattern = temporary_pattern or ("." + path.name + ".*.tmp")
    try:
        siblings = os.listdir(path.parent)
    except OSError:
        return False
    identity = (info.st_dev, info.st_ino)
    for name in siblings:
        if name == path.name:
            continue
        if not fnmatch.fnmatch(name, pattern):
            continue
        try:
            other = os.lstat(path.parent / name)
        except OSError:
            continue
        if (other.st_dev, other.st_ino) == identity:
            return True
    return False


def _read_file_once(path: Path, max_bytes: int, temporary_pattern=None):
    """One read attempt.

    Returns the payload bytes, or ``None`` while this protocol's own publication
    window is open on the file: the extra temporary link means the publisher has
    linked the target but not yet unlinked its own name, so the caller waits
    under a deadline and re-stats instead of reading or failing.  Every other
    condition is named here: a foreign hard link, a mismatched name, a different
    inode, a symlink, a mode mismatch or an oversized file is refused.
    """
    path = _absolute(path)
    info = _ls_final(path, missing=False)
    if info.st_nlink != 1:
        _require_live_window(path, info, temporary_pattern)
        return None
    fd = os.open(path, _flags(os.O_RDONLY))
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise Error("expected a regular file")
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise Error("file changed while opening or acquiring lock")
        if opened.st_nlink != 1:
            _require_live_window(path, opened, temporary_pattern)
            return None
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
        after = os.fstat(fd)
        if (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise Error("file changed while reading")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_bytes(path: Path, max_bytes=65536, temporary_pattern=None) -> bytes:
    """Read a file, waiting out a bounded publication window when one is open.

    A file that currently carries this protocol's own temporary link is not read
    yet: the caller waits, re-stats and re-validates the identity on every
    attempt until the publisher unlinks its temporary name, or until
    ``PUBLISH_WAIT_SECONDS`` expires.  A window that never closes -- a crash
    that left the temporary link behind, for example -- is refused by name when
    the deadline expires, exactly like a foreign link, so the reader never turns
    a permanent defect into a silent success.
    """
    if not isinstance(max_bytes, int) or max_bytes < 0:
        raise Error("max_bytes must be a nonnegative integer")
    target = _absolute(path)
    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    attempts = 0
    while True:
        attempts += 1
        try:
            payload = _read_file_once(target, max_bytes, temporary_pattern)
        except (OSError, UnicodeError, ValueError) as exc:
            raise Error("cannot read relay file") from exc
        if payload is not None:
            return payload
        if time.monotonic() >= deadline and attempts >= PUBLISH_WAIT_ATTEMPTS:
            raise Error(
                "file is still inside a publication window after %d attempts "
                "(%.1fs)" % (attempts, PUBLISH_WAIT_SECONDS))
        time.sleep(PUBLISH_WAIT_SLICE)


def _check(path: Path, *, missing=False):
    """Check each existing component without resolving away child links.

    The final component is validated exactly as strictly as it always was
    (regular file, exactly one link, no symlink).  Callers that must tolerate
    this protocol's own publication window use ``_ls_final`` and classify the
    extra link themselves; no other call site is relaxed.
    """
    info = _final_info(path, missing=missing)
    if info is None:
        return None
    _validate(info, directory=False)
    return info


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



def read(path: Path, max_bytes=65536, temporary_pattern=None) -> str:
    try:
        return read_bytes(path, max_bytes=max_bytes,
                          temporary_pattern=temporary_pattern).decode("utf-8")
    except UnicodeError as exc:
        raise Error("cannot read relay file") from exc


def private_dir(path: Path) -> bool:
    """Create a private (0700) directory once; return True when it was created.

    Existing components are re-validated as real directories, so a symlinked
    shard can never redirect an object write outside the relay.
    """
    path = _absolute(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        private_dir(path.parent)
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            return private_dir(path)
        fsync_directory(path.parent)
        fsync_directory(path)
        return True
    _validate(info, directory=True)
    return False


def fsync_directory(path: Path) -> list:
    """Best-effort directory fsync used after publishing immutable objects."""
    if os.name == "nt":
        return ["durability: directory fsync is unavailable on this platform"]
    fd = None
    try:
        fd = os.open(_absolute(path), _flags(os.O_RDONLY) | getattr(os, "O_DIRECTORY", 0))
        os.fsync(fd)
        return []
    except OSError:
        return ["durability: directory fsync failed"]
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


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


_MISSING = object()


def _existing_outcome(path: Path, payload: bytes):
    """Classify an existing publication target without ever relaxing a link.

    Returns ``_MISSING`` when nothing is published yet (the caller may create
    it), ``None`` when the only obstacle is this protocol's own live publication
    window (the caller waits under a deadline and re-stats), ``[]`` for an
    identical target, and a named ``Error`` for a foreign extra link, a symlink
    or conflicting content.  The window test is the same two-condition test the
    read path uses: same directory, matching temporary name, same inode.
    """
    info = _ls_final(path, missing=True)
    if info is None:
        return _MISSING
    if info.st_nlink != 1:
        _require_live_window(path, info)
        return None
    if read_bytes(path, max_bytes=max(len(payload), 1)) != payload:
        raise Error("immutable file conflicts with existing content")
    return []


def immutable_bytes(path: Path, payload: bytes) -> list[str]:
    """Publish exactly once, waiting out a bounded publication window.

    A competing creator holds one extra link (its own temporary name) until it
    unlinks that name.  Every attempt re-stats the target and re-verifies the
    identity of the extra link; a window that does not close within
    ``PUBLISH_WAIT_SECONDS`` is refused by name instead of being retried
    forever, so a crash that leaves a temporary link behind fails closed.
    """
    deadline = time.monotonic() + PUBLISH_WAIT_SECONDS
    attempts = 0
    while True:
        attempts += 1
        outcome = _immutable_attempt(path, payload)
        if outcome is not None:
            return outcome
        if time.monotonic() >= deadline and attempts >= PUBLISH_WAIT_ATTEMPTS:
            raise Error(
                "immutable publish did not settle within the publication window "
                "after %d attempts (%.1fs)" % (attempts, PUBLISH_WAIT_SECONDS))
        time.sleep(PUBLISH_WAIT_SLICE)


def _immutable_attempt(path: Path, payload: bytes):
    """Create *path* exactly once, or verify an identical existing file.

    Receipt segments and objects are content addressed.  Unlike ``atomic``
    this helper never replaces an existing target, which makes retries and
    races safe.  A competing creator may briefly hold a second name for the
    same inode (its own temporary link).  That window is classified explicitly
    by ``_existing_outcome`` and waited out under a deadline; a window that
    never closes is a named failure rather than an endless retry.
    """
    temporary = None
    parent_fd = None
    try:
        path = _absolute(path)
        parent_before = _check(path.parent)
        _validate(parent_before, directory=True)
        outcome = _existing_outcome(path, payload)
        if outcome is not _MISSING:
            return outcome
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
            # Another creator won the link race.  Its target is complete (the
            # payload was fsynced before the link), but the winner may still be
            # holding its own temporary name: ``_existing_outcome`` classifies
            # that live window as a transient retry and refuses everything else.
            outcome = _existing_outcome(path, payload)
            if outcome is not _MISSING:
                return outcome
            return None
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


def immutable(path: Path, document: str) -> list[str]:
    """UTF-8 wrapper kept for receipt segments that already speak text."""
    return immutable_bytes(path, document.encode("utf-8"))


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
