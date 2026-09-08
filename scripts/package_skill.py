#!/usr/bin/env python3
"""Create a deterministic, allowlisted source archive and SHA-256 sidecar."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import zipfile
from pathlib import Path

VERSION = "0.2.0"
ARCHIVE_ROOT = "project-continuity"
FILES = (
    ".gitignore",
    ".github/workflows/ci.yml",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "NOTICE",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "SKILL.md",
    "agents/openai.yaml",
    "examples/README.md",
    "examples/change.json",
    "references/commands.md",
    "references/protocol.md",
    "scripts/cli_v2.py",
    "scripts/git_state.py",
    "scripts/package_skill.py",
    "scripts/progress.py",
    "scripts/storage.py",
    "scripts/write_current.py",
    "tests/test_package.py",
    "tests/test_capacity.py",
    "tests/test_git_state.py",
    "tests/test_progress.py",
    "tests/test_reliability.py",
    "tests/test_security_regressions.py",
    "tests/test_storage.py",
    "tests/test_write_current.py",
)


def package(destination: Path, root: Path | None = None) -> tuple[Path, Path, str]:
    root = (root or Path(__file__).resolve().parents[1]).resolve()
    destination = destination.expanduser().resolve()
    checksum = destination.with_name(destination.name + ".sha256")
    if destination.suffix != ".zip":
        raise ValueError("destination must end in .zip")
    if destination.exists() or checksum.exists():
        raise FileExistsError("refusing to overwrite archive or checksum")
    missing = [name for name in FILES if not (root / name).is_file() or (root / name).is_symlink()]
    if missing:
        raise FileNotFoundError("allowlisted source file is missing or a symlink: " + missing[0])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    created_archive = False
    try:
        fd, raw = tempfile.mkstemp(prefix=".project-continuity-", suffix=".zip.tmp", dir=destination.parent)
        os.close(fd)
        temporary = Path(raw)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(FILES):
                info = zipfile.ZipInfo(f"{ARCHIVE_ROOT}/{name}", date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                mode = 0o755 if name in {"scripts/package_skill.py", "scripts/write_current.py"} else 0o644
                info.external_attr = mode << 16
                info.flag_bits |= 0x800
                archive.writestr(info, (root / name).read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        digest = hashlib.sha256(temporary.read_bytes()).hexdigest()
        # A hard link publishes atomically and fails rather than overwriting a
        # destination created after the initial check.
        os.link(temporary, destination)
        created_archive = True
        temporary.unlink()
        temporary = None
        # Exclusive creation preserves the no-overwrite contract if another process raced us.
        with checksum.open("x", encoding="ascii", newline="\n") as handle:
            handle.write(f"{digest}  {destination.name}\n")
        return destination, checksum, digest
    except Exception:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        # Avoid leaving an archive without its required checksum.
        if created_archive and destination.exists() and not checksum.exists():
            destination.unlink()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path, help="new .zip path; existing outputs are refused")
    args = parser.parse_args(argv)
    try:
        archive, checksum, digest = package(args.destination)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(f"archive={archive}")
    print(f"sha256={digest}")
    print(f"checksum={checksum}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
