"""Explicit local progress CLI. No network, model calls, Git writes or background work."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import git_state
import progress as p
import storage as fs

SECRET = re.compile(
    r"(?i)(?:api[ _-]?key|password|passwd|secret|token|cookie|authorization|bearer)[\"']?\s*[:=]\s*[\"']?\S+"
    r"|(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    r"|(?<![A-Za-z0-9_-])(?:AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})|-----BEGIN [^-]*PRIVATE KEY-----"
    r"|(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
)


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def scan(document):
    p.require(not SECRET.search(document), "refusing possible sensitive content")
    p.require(len(document.encode("utf-8")) <= p.MAX_BYTES, "document exceeds 64 KiB; no content was truncated")


def split(document):
    scan(document)
    lines = document.splitlines(keepends=True)
    p.require(lines and lines[0].strip() == "---", "missing front matter")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    p.require(end is not None, "unterminated front matter")
    meta = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        p.require(not line[0].isspace() and ":" in line, "only flat front matter supported")
        key, value = line.rstrip("\r\n").split(":", 1)
        p.require(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key) and key not in meta, "invalid or duplicate metadata")
        meta[key] = value.strip()
    p.require(meta.get("schema") in ("project-continuity/v1", p.SCHEMA), "unsupported schema")
    p.require(re.fullmatch(r"[0-9]+", meta.get("revision", "")), "invalid revision")
    p.identifier(meta.get("project_id"))
    p.require(meta.get("status") in ("active", "paused", "blocked", "complete"), "invalid metadata status")
    p.timestamp(meta.get("updated_at"))
    writer, until = meta.get("writer"), meta.get("lease_until")
    p.require(writer is not None and until is not None, "missing lease fields")
    if writer != "null":
        p.identifier(writer)
        p.timestamp(until)
    else:
        p.require(until == "null", "lease without writer")
    baseline(meta)
    return lines[1:end], "".join(lines[end + 1:]), meta


def metadata(lines, changes):
    out, seen = [], set()
    for line in lines:
        key = line.split(":", 1)[0]
        if key in changes:
            out.append(f"{key}: {changes[key]}\n")
            seen.add(key)
        else:
            out.append(line)
    out.extend(f"{key}: {value}\n" for key, value in changes.items() if key not in seen)
    return "---\n" + "".join(out) + "---\n"


def parsed_body(body):
    state, matches = p.parse_body(body)
    # Raw JSON may encode ASCII secrets as Unicode escapes; inspect decoded
    # values (including arbitrary extensions) before returning or rendering them.
    scan(json.dumps(state, ensure_ascii=False))
    if "recovery_log" in state["extensions"]:
        p.require(isinstance(state["extensions"]["recovery_log"], list),
                  "reserved recovery_log must be a list")
    return state, matches


def read(root):
    current = fs.child(root, ".relay", "CURRENT.md", exists=True)
    document = fs.read(current)
    lines, body, meta = split(document)
    if meta["schema"] == p.SCHEMA:
        state, matches = parsed_body(body)
        p.require(state["project"]["status"] == meta["status"], "metadata/project status conflict")
    else:
        state, matches = None, True
    return document, lines, body, meta, state, matches


def lease_active(meta):
    return meta["writer"] != "null" and datetime.fromisoformat(meta["lease_until"].replace("Z", "+00:00")) > now()


def baseline(meta):
    if "git_baseline" in meta:
        value = p.loads(meta["git_baseline"])
        p.require(isinstance(value, dict), "invalid Git baseline")
        scan(json.dumps(value, ensure_ascii=False))
        return value
    return {"kind": "legacy", "branch": meta.get("branch"), "head": meta.get("base_commit")}


def git_fields(value):
    return {"branch": value.get("branch") or "null", "base_commit": value.get("head") or "null",
            "git_baseline": json.dumps(value, ensure_ascii=True, separators=(",", ":"))}


def checked(document):
    scan(document)
    _, body, meta = split(document)
    if meta["schema"] == p.SCHEMA:
        _, matches = parsed_body(body)
        p.require(matches, "generated view mismatch")
    return document


def initialize(args, root):
    project_id = args.project_id if args.project_id is not None else str(uuid.uuid4())
    p.identifier(project_id)
    state = p.empty_state(args.name or "Project")
    current = fs.child(root, ".relay", "CURRENT.md")
    relay = fs.child(root, ".relay")
    current_git = git_state.capture(root)
    p.require(current_git["kind"] != "error", "Git inspection failed")
    doc = checked(metadata([], {"schema": p.SCHEMA, "project_id": project_id, "revision": "0",
                  "updated_at": iso(now()), "writer": "null", "lease_until": "null", "status": "active",
                  **git_fields(current_git)}) + p.render_body(state))
    relay.mkdir(mode=0o700, exist_ok=True)
    with fs.locked(fs.child(root, ".relay", "CURRENT.md.lock")):
        p.require(not current.exists(), "CURRENT.md already exists")
        for name in ("history", "inbox"):
            fs.child(root, ".relay", name).mkdir(mode=0o700, exist_ok=True)
        ignore = fs.child(root, ".relay", ".gitignore")
        if not ignore.exists():
            fs.atomic(ignore, "# Local by default. To share, replace * with /history/, /inbox/, /*.lock, /.*.tmp\n*\n")
        warnings = fs.atomic(current, doc)
    return {"revision": 0, "schema": p.SCHEMA, "warnings": warnings}


def status(args, root):
    path = fs.child(root, ".relay", "CURRENT.md")
    if not path.exists():
        return {"exists": False, "verification": "UNVERIFIED"}
    doc, _, _, meta, state, matches = read(root)
    git = git_state.capture(root)
    result = {"exists": True, "schema": meta["schema"], "revision": int(meta["revision"]),
              "status": meta["status"], "writer": meta["writer"], "lease_until": meta["lease_until"],
              "lease_active": lease_active(meta), "git": git, "drift": baseline(meta) != git,
              "verification": "CONFLICT" if not matches else "UNVERIFIED" if git["kind"] != "git" else "structurally_valid",
              "bytes": len(doc.encode()), "near_limit": len(doc.encode()) >= int(p.MAX_BYTES * .8)}
    inbox = fs.child(root, ".relay", "inbox")
    result["pending_inbox"] = len(list(inbox.iterdir())) if inbox.exists() else 0
    if state:
        result.update(p.summary(state, git))
        migration_info = state["extensions"].get("migration", {})
        pending = isinstance(migration_info, dict) and migration_info.get("mapping_review_required", False)
        result["mapping_review_required"] = bool(pending)
        if pending and matches:
            result["verification"] = "UNVERIFIED"
    else:
        result["migration_required"] = True
    if args.command == "validate":
        p.require(matches, "derived view conflict")
    return result


def migration(document, body, meta):
    p.require(meta["schema"] == "project-continuity/v1", "migration requires v1")
    state = p.empty_state("Migrated project")
    # Never infer a business lifecycle or verification claim from legacy prose.
    state["project"]["status"] = "paused"
    state["project"]["next_step"] = "Review preserved v1 sections and explicitly map current tasks."
    state["extensions"]["migration"] = {"source_sha256": hashlib.sha256(document.encode()).hexdigest(),
                                          "source_status": meta["status"], "mapping_review_required": True}
    return state, body.rstrip("\n") + "\n\n" + p.render_body(state)


def input_patch(args):
    if not getattr(args, "input", None):
        return {}
    if args.input == "-":
        raw = sys.stdin.buffer.read(p.MAX_BYTES + 1)
        p.require(len(raw) <= p.MAX_BYTES, "input exceeds 64 KiB")
        data = raw.decode("utf-8")
    else:
        data = fs.read(Path(args.input).expanduser().absolute())
    scan(data)
    patch = p.loads(data)
    scan(json.dumps(patch, ensure_ascii=False))
    return patch


def mutate(args, root):
    patch = input_patch(args)
    p.identifier(args.writer)
    p.identifier(args.operation_id)
    p.require(args.expected_revision is not None and args.expected_revision >= 0, "expected revision required")
    p.require(0 < args.lease_minutes <= 1440, "lease must be 1 to 1440 minutes")
    operation_hash = p.digest({"command": args.command, "writer": args.writer, "patch": patch,
                               "expected_revision": args.expected_revision, "lease_minutes": args.lease_minutes,
                               "allow_drift": args.allow_drift, "reason": getattr(args, "reason", None),
                               "snapshot": getattr(args, "snapshot", None), "source_sha256": getattr(args, "source_sha256", None)})
    # Never create relay or lock if CURRENT is absent.
    fs.child(root, ".relay", "CURRENT.md", exists=True)
    with fs.locked(fs.child(root, ".relay", "CURRENT.md.lock")):
        old, lines, body, meta, state, matches = read(root)
        if state:
            for operation in state["operations"]:
                if operation["id"] == args.operation_id:
                    p.require(operation["hash"] == operation_hash, "operation ID reused with different input")
                    return {"revision": operation["revision"], "current_revision": int(meta["revision"]), "replayed": True}
        p.require(int(meta["revision"]) == args.expected_revision, "revision conflict")
        p.require(matches or args.command == "recover", "derived view conflict; review direct edits before writing")
        git = git_state.capture(root)
        p.require(git["kind"] != "error", "Git inspection failed")
        old_git = baseline(meta)
        changed_branch = old_git.get("kind") == "git" and (git.get("kind") != "git" or old_git.get("branch") != git.get("branch"))
        if (args.command == "resume" and old_git != git) or changed_branch:
            p.require(args.allow_drift, "Git drift detected; review before --allow-drift")
        if args.command == "migrate":
            p.require(not lease_active(meta), "active lease prevents migration")
            p.require(args.source_sha256 == hashlib.sha256(old.encode()).hexdigest(), "migration source hash changed or missing")
            state, body = migration(old, body, meta)
        else:
            p.require(state is not None, "v1 is read-only; run migrate preview and explicit --apply")
        if args.command == "resume":
            p.require(not lease_active(meta) or meta["writer"] == args.writer, "lease conflict")
            if state["project"]["status"] == "paused":
                state["project"]["status"] = "active"
        elif args.command in ("update", "save"):
            p.require(meta["writer"] == args.writer and lease_active(meta), "missing, expired or conflicting lease; resume first")
            state = p.apply(state, patch, git)
            if args.command == "save" and "status" not in patch.get("project", {}) and state["project"]["status"] == "active":
                state["project"]["status"] = "paused"
        elif args.command == "recover":
            p.require(not lease_active(meta), "active lease prevents recovery")
            p.text(args.reason, "recovery reason")
            receipts = copy.deepcopy(state["operations"])
            current_extensions = copy.deepcopy(state["extensions"])
            if args.snapshot:
                p.require(re.fullmatch(r"r[0-9]+-[0-9a-f]{64}\.md", args.snapshot), "invalid snapshot filename")
                snap = fs.read(fs.child(root, ".relay", "history", args.snapshot, exists=True))
                p.require(hashlib.sha256(snap.encode()).hexdigest() == args.snapshot.split("-", 1)[1][:-3], "snapshot hash mismatch")
                snap_lines, snap_body, snap_meta = split(snap)
                p.require(snap_meta["project_id"] == meta["project_id"] and snap_meta["schema"] == p.SCHEMA, "snapshot project or schema mismatch")
                state, snap_matches = parsed_body(snap_body)
                p.require(snap_matches, "snapshot view conflict")
                # Preserve current user extensions/metadata text, replace only managed progress.
                state["operations"] = receipts
                state["extensions"].update(current_extensions)
            state["extensions"].setdefault("recovery_log", []).append({"reason": args.reason, "from_revision": int(meta["revision"]), "snapshot": args.snapshot})
        revision = int(meta["revision"]) + 1
        state["operations"].append({"id": args.operation_id, "hash": operation_hash, "revision": revision})
        keep_lease = args.command in ("resume", "update")
        updates = {"schema": p.SCHEMA, "revision": str(revision), "updated_at": iso(now()),
                   "writer": args.writer if keep_lease else "null",
                   "lease_until": iso(now() + timedelta(minutes=args.lease_minutes)) if keep_lease else "null",
                   "status": state["project"]["status"], **git_fields(git)}
        new = checked(metadata(lines, updates) + p.render_body(state, body))
        fs.child(root, ".relay", "history").mkdir(mode=0o700, exist_ok=True)
        result = fs.commit(fs.child(root, ".relay", "CURRENT.md", exists=True),
                           fs.child(root, ".relay", "history", exists=True), old, new, int(meta["revision"]))
        # Do not disclose a user's absolute path or reject a successful commit
        # merely because their directory name resembles sensitive content.
        result["history"] = Path(result["history"]).name
        return {**result, "revision": revision, "writer": updates["writer"], "lease_until": updates["lease_until"], "replayed": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for command in ("init", "status", "validate", "resume", "update", "save", "migrate", "recover"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--root", default=".")
        if command == "init":
            cmd.add_argument("--project-id")
            cmd.add_argument("--name")
        if command in ("resume", "update", "save", "migrate", "recover"):
            required = command != "migrate"
            cmd.add_argument("--writer", required=required)
            cmd.add_argument("--expected-revision", type=int, required=required)
            cmd.add_argument("--operation-id", required=required)
            cmd.add_argument("--lease-minutes", type=int, default=30)
            cmd.add_argument("--allow-drift", action="store_true")
        if command in ("update", "save"):
            cmd.add_argument("--input", help="JSON change file, or - for standard input")
        if command == "migrate":
            cmd.add_argument("--apply", action="store_true")
            cmd.add_argument("--source-sha256")
        if command == "recover":
            cmd.add_argument("--reason", required=True)
            cmd.add_argument("--snapshot")
    return result


def main(argv=None):
    try:
        args = parser().parse_args(argv)
        root = fs.root_path(args.root)
        # Successful writes report a history path; reject sensitive roots before
        # creating any state, rather than discovering unsafe output postcommit.
        scan(str(root))
        if args.command == "init":
            result = initialize(args, root)
        elif args.command in ("status", "validate"):
            result = status(args, root)
        elif args.command == "migrate" and not args.apply:
            old, lines, body, meta, state, _ = read(root)
            if state is not None:
                result = {"migration_required": False, "revision": int(meta["revision"])}
            else:
                state, body = migration(old, body, meta)
                candidate = checked(metadata(lines, {"schema": p.SCHEMA, "status": "paused"}) + body)
                result = {"dry_run": True, "source_sha256": hashlib.sha256(old.encode()).hexdigest(),
                          "revision": int(meta["revision"]), "candidate_bytes": len(candidate.encode()),
                          "mapping_review_required": True}
        else:
            result = mutate(args, root)
        output = json.dumps(result, ensure_ascii=False)
        scan(output)
        print(output)
        return 0
    except (fs.Error, p.Invalid) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        # Do not leak arbitrary filenames, subprocess output, or input fragments.
        print('{"error":"invalid input or unavailable local resource"}', file=sys.stderr)
        return 2
