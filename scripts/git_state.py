"""Read-only Git/index/raw-file identity. Never invoke worktree clean filters."""
from __future__ import annotations

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import storage

MAX_FINGERPRINT_BYTES = 64 * 1024 * 1024


def capture(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env.update({'GIT_OPTIONAL_LOCKS': '0', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull})

    def run(*args):
        return subprocess.run(['git', '--no-pager', '-c', 'core.fsmonitor=false', '-C', str(root), *args],
                              capture_output=True, env=env, timeout=15)

    def error(reason):
        return {'kind': 'error', 'reason': reason}

    try:
        probe = run('rev-parse', '--show-toplevel')
        if probe.returncode:
            return {'kind': 'none'} if b'not a git repository' in probe.stderr else error('Git repository probe failed')
        top = Path(os.fsdecode(probe.stdout.strip())).resolve()
        if top != Path(root).resolve():
            return error('root is not Git worktree root')
        head = run('rev-parse', '--verify', 'HEAD')
        branch = run('symbolic-ref', '--quiet', '--short', 'HEAD')
        if head.returncode and branch.returncode:
            return error('cannot resolve Git HEAD')
        indexed = run('ls-files', '--stage', '-z', '--', '.', ':(exclude).relay')
        others = run('ls-files', '--others', '--exclude-standard', '-z', '--', '.', ':(exclude).relay')
        tree = run('ls-tree', '-r', '-z', '--full-tree', 'HEAD') if not head.returncode else None
        fmt = run('rev-parse', '--show-object-format')
        if any(r.returncode for r in (indexed, others, fmt)) or (tree and tree.returncode):
            return error('cannot inspect Git index/tree')
        algorithm = fmt.stdout.decode().strip()
        if algorithm not in ('sha1', 'sha256'):
            return error('unsupported Git object format')
        index = {}
        for entry in indexed.stdout.split(b'\0'):
            if not entry:
                continue
            fields, name = entry.split(b'\t', 1)
            mode, object_id, stage = fields.split()
            if stage != b'0':
                return error('unmerged Git index')
            index[name] = (mode, object_id)
        committed = {}
        if tree:
            for entry in tree.stdout.split(b'\0'):
                if not entry:
                    continue
                fields, name = entry.split(b'\t', 1)
                if name == b'.relay' or name.startswith(b'.relay/'):
                    continue
                mode, _, object_id = fields.split()
                committed[name] = (mode, object_id)
        untracked = [name for name in others.stdout.split(b'\0') if name]
        dirty = index != committed or bool(untracked)
        fingerprint = hashlib.sha256(indexed.stdout + b'\0' + others.stdout)
        total = 0
        for name in sorted(set(index) | set(untracked)):
            path = storage.child(top, os.fsdecode(name))
            fingerprint.update(name + b'\0')
            try:
                before = path.lstat()
            except FileNotFoundError:
                fingerprint.update(b'missing\0')
                dirty = True
                continue
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return error('raw fingerprint requires regular nonlinked files; submodules are unsupported')
            total += before.st_size
            if total > MAX_FINGERPRINT_BYTES:
                return error('raw fingerprint exceeds 64 MiB budget')
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
            fd = os.open(path, flags)
            with os.fdopen(fd, 'rb') as handle:
                opened = os.fstat(handle.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino) or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                    return error('file changed while opening fingerprint')
                blob = hashlib.new(algorithm)
                blob.update(b'blob ' + str(opened.st_size).encode() + b'\0')
                seen = 0
                while True:
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    seen += len(chunk)
                    if seen > before.st_size:
                        return error('file changed while fingerprinting')
                    blob.update(chunk)
                    fingerprint.update(chunk)
                after = os.fstat(handle.fileno())
                storage.child(top, os.fsdecode(name), exists=True)
                final = path.lstat()
                if seen != before.st_size or (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (before.st_size, before.st_mtime_ns, before.st_ctime_ns) or (final.st_dev, final.st_ino) != (before.st_dev, before.st_ino):
                    return error('file changed while fingerprinting')
                fingerprint.update(b'\0')
                if name in index:
                    mode, expected = index[name]
                    executable = os.name != 'nt' and bool(before.st_mode & stat.S_IXUSR)
                    if blob.hexdigest().encode() != expected or (os.name != 'nt' and executable != (mode == b'100755')):
                        dirty = True
        return {'kind': 'git', 'branch': os.fsdecode(branch.stdout.strip()) if not branch.returncode else 'detached',
                'head': head.stdout.decode().strip() if not head.returncode else None,
                'dirty': dirty, 'untracked': len(untracked), 'fingerprint': fingerprint.hexdigest()}
    except (OSError, ValueError, subprocess.SubprocessError, storage.Error):
        return error('Git inspection unavailable')
