"""Explicit local progress CLI. No network, model calls, Git writes or background work."""
from __future__ import annotations

import argparse
import copy
from contextlib import nullcontext
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import uuid
import zipfile
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

ARCHIVE_DIR = "receipts"
ARCHIVE_SUFFIX = ".jsonl"
BUNDLE_MAX_BYTES = 64 * 1024 * 1024


def now():
    return datetime.now(timezone.utc)


def iso(value):
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def scan(document, enforce_limit=True):
    p.require(not SECRET.search(document), "refusing possible sensitive content")
    if enforce_limit:
        p.require(len(document.encode("utf-8")) <= p.MAX_BYTES,
                  "document exceeds 64 KiB; no content was truncated")


def split(document, enforce_limit=True):
    scan(document, enforce_limit=enforce_limit)
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
    p.require(meta.get("schema") in ("project-continuity/v1", *p.SUPPORTED_SCHEMAS), "unsupported schema")
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
    scan(json.dumps(state, ensure_ascii=False), enforce_limit=False)
    if "recovery_log" in state["extensions"]:
        p.require(isinstance(state["extensions"]["recovery_log"], list),
                  "reserved recovery_log must be a list")
    return state, matches


def read(root):
    current = fs.child(root, ".relay", "CURRENT.md", exists=True)
    document = fs.read(current)
    lines, body, meta = split(document)
    if meta["schema"] in p.SUPPORTED_SCHEMAS:
        state, matches = parsed_body(body)
        compaction = state["extensions"].get("compaction")
        p.require((meta["schema"] == p.SCHEMA_V3) == (compaction is not None),
                  "receipt archive metadata/schema mismatch")
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


def _archive_name(value):
    p.require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}\.jsonl", value),
              "invalid receipt archive name")
    return value


def _receipt(value):
    p.require(isinstance(value, dict), "invalid operation receipt")
    p.require(set(value) == {"id", "hash", "revision"}, "invalid operation receipt")
    p.identifier(value["id"])
    p.require(isinstance(value["hash"], str) and re.fullmatch(r"[0-9a-f]{64}", value["hash"]),
              "invalid operation receipt")
    p.require(type(value["revision"]) is int and value["revision"] >= 0, "invalid operation receipt")
    return value


def _archive_payload(project_id, previous, records):
    header = {"schema": p.RECEIPT_SCHEMA, "project_id": project_id,
              "previous": previous, "count": len(records)}
    lines = [json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":"))]
    lines.extend(json.dumps(_receipt(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                 for record in records)
    return "\n".join(lines) + "\n"


def _archive_chain(root, state, meta):
    """Return all committed archived receipts, or fail closed on corruption."""
    compaction = state["extensions"].get("compaction")
    if compaction is None:
        return []
    p.require(meta["schema"] == p.SCHEMA_V3, "receipt archive requires project-continuity/v3")
    p.validate(state)
    head = compaction["head"]
    if head is None:
        p.require(compaction["count"] == 0, "receipt archive count mismatch")
        return []
    directory = fs.child(root, ".relay", ARCHIVE_DIR, exists=True)
    seen_files, seen_ids, records = set(), set(), []
    while head is not None:
        _archive_name(head)
        p.require(head not in seen_files, "receipt archive cycle")
        seen_files.add(head)
        path = fs.child(directory, head, exists=True)
        raw = fs.read(path)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest() + ARCHIVE_SUFFIX
        p.require(digest == head, "receipt archive hash mismatch")
        lines = raw.splitlines()
        p.require(lines, "empty receipt archive")
        header = p.loads(lines[0])
        p.require(isinstance(header, dict) and set(header) == {"schema", "project_id", "previous", "count"},
                  "invalid receipt archive header")
        p.require(header["schema"] == p.RECEIPT_SCHEMA and header["project_id"] == meta["project_id"],
                  "receipt archive identity mismatch")
        previous = header["previous"]
        p.require(previous is None or isinstance(previous, str), "invalid receipt archive link")
        if previous is not None:
            _archive_name(previous)
        p.require(type(header["count"]) is int and 0 < header["count"] <= p.RECEIPT_SEGMENT_MAX and
                  header["count"] == len(lines) - 1,
                  "receipt archive count mismatch")
        for line in lines[1:]:
            record = _receipt(p.loads(line))
            p.require(record["id"] not in seen_ids, "duplicate archived operation")
            seen_ids.add(record["id"])
            records.append(record)
        head = previous
    p.require(compaction["count"] == len(records), "receipt archive total mismatch")
    current_ids = {record["id"] for record in state["operations"]}
    p.require(not current_ids.intersection(seen_ids), "operation receipt appears in current and archive")
    return records


def _archive_info(root, state, meta):
    compaction = state["extensions"].get("compaction") if state else None
    if compaction is None:
        current = len(state["operations"]) if state else 0
        return {"integrity": "not_configured", "head": None, "archived": 0,
                "current": current, "retained": current, "reclaimable_bytes": 0}
    try:
        records = _archive_chain(root, state, meta)
    except (fs.Error, p.Invalid, OSError, UnicodeError, ValueError, TypeError, KeyError):
        current = len(state["operations"])
        return {"integrity": "invalid", "head": compaction.get("head"),
                "archived": compaction.get("count", 0), "current": current, "retained": current,
                "reclaimable_bytes": 0}
    current = len(state["operations"])
    return {"integrity": "ok", "head": compaction["head"], "archived": len(records),
            "current": current, "retained": current, "reclaimable_bytes": 0}


def _plan_compaction(state, project_id, force=False):
    """Build a deterministic archive plan without touching the filesystem."""
    operations = list(state["operations"])
    move_count = max(0, len(operations) - p.RECEIPT_KEEP)
    records = operations[:move_count]
    compaction = state["extensions"].get("compaction")
    previous = compaction["head"] if compaction else None
    archived_count = compaction["count"] if compaction else 0
    segments, head = [], previous
    for index in range(0, len(records), p.RECEIPT_SEGMENT_MAX):
        chunk = records[index:index + p.RECEIPT_SEGMENT_MAX]
        payload = _archive_payload(project_id, head, chunk)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest() + ARCHIVE_SUFFIX
        segments.append((digest, payload))
        head = digest
        archived_count += len(chunk)
    if not records and not force and compaction is None:
        return copy.deepcopy(state), segments, False
    out = copy.deepcopy(state)
    out["operations"] = operations[move_count:]
    out["extensions"]["compaction"] = {"schema": p.RECEIPT_SCHEMA, "head": head,
                                          "count": archived_count, "retained": p.RECEIPT_KEEP}
    p.validate(out)
    return out, segments, bool(records or force or compaction is not None)


def _store_archive_segments(root, segments):
    if not segments:
        return []
    directory = fs.child(root, ".relay", ARCHIVE_DIR)
    if not directory.exists():
        directory.mkdir(mode=0o700)
    warnings = []
    for name, payload in segments:
        path = fs.child(directory, name)
        warnings.extend(fs.immutable(path, payload))
        p.require(fs.read(path) == payload, "receipt archive readback mismatch")
    return warnings


def _candidate_bytes(document):
    # Parse all managed content and scan secrets, but defer the hard size check
    # until automatic compaction has had one deterministic chance to run.
    lines, body, meta = split(document, enforce_limit=False)
    if meta["schema"] in p.SUPPORTED_SCHEMAS:
        parsed_body(body)
    return len(document.encode("utf-8"))


def _plan_candidate(lines, body, state, updates, project_id, *, force=False, automatic=True):
    """Pure budget/render plan; caller supplies complete transaction metadata."""
    candidate = metadata(lines, updates) + p.render_body(state, body)
    ungoverned_bytes = _candidate_bytes(candidate)
    segments, compacted = [], False
    if updates["schema"] == p.SCHEMA_V3 and (force or
            (automatic and ungoverned_bytes >= p.COMPACTION_TRIGGER_BYTES)):
        state, segments, compacted = _plan_compaction(state, project_id, force=True)
        candidate = metadata(lines, updates) + p.render_body(state, body)
        if len(candidate.encode("utf-8")) > p.COMPACTION_TARGET_BYTES:
            candidate = metadata(lines, updates) + p.render_body(state, body, compact=True)
    final_bytes = _candidate_bytes(candidate)
    return state, candidate, segments, compacted, ungoverned_bytes, final_bytes


def _capacity_sections(document, body):
    """Report approximate UTF-8 byte ownership without parsing Markdown prose."""
    data_a = body.find(p.DATA_START)
    data_b = body.find(p.DATA_END)
    view_a = body.find(p.VIEW_START)
    view_b = body.find(p.VIEW_END)
    structured = len(body[data_a + len(p.DATA_START):data_b].encode("utf-8")) if data_a >= 0 and data_b >= data_a else 0
    view = len(body[view_a + len(p.VIEW_START):view_b].encode("utf-8")) if view_a >= 0 and view_b >= view_a else 0
    total = len(document.encode("utf-8"))
    managed = structured + view
    return {"front_matter_and_custom_markdown": max(0, total - managed),
            "structured_json": structured, "derived_view": view, "total": total}


def _reachable_archive_names(root, state, meta):
    """Return validated receipt segment names reachable from the current head."""
    if state["extensions"].get("compaction") is None:
        return []
    _archive_chain(root, state, meta)
    names, seen = [], set()
    head = state["extensions"]["compaction"]["head"]
    if head is None:
        return []
    directory = fs.child(root, ".relay", ARCHIVE_DIR, exists=True)
    while head is not None:
        _archive_name(head)
        p.require(head not in seen, "receipt archive cycle")
        seen.add(head)
        names.append(head)
        raw = fs.read(fs.child(directory, head, exists=True))
        header = p.loads(raw.splitlines()[0])
        head = header["previous"]
    return names


def _bundle_payload(root):
    old, _, _, meta, state, matches = read(root)
    p.require(state is not None, "v1 is read-only; migrate before exporting a handoff bundle")
    p.require(matches, "derived view conflict; review direct edits before exporting")
    names = _reachable_archive_names(root, state, meta)
    files = {"CURRENT.md": old}
    directory = fs.child(root, ".relay", ARCHIVE_DIR, exists=True) if names else None
    for name in names:
        files[f"receipts/{name}"] = fs.read(fs.child(directory, name, exists=True))
    manifest = {"format": "project-continuity/handoff/v1", "schema": meta["schema"],
                "project_id": meta["project_id"],
                "current_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                "receipts": names}
    files["MANIFEST.json"] = json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":")) + "\n"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o600 << 16
            info.flag_bits |= 0x800
            archive.writestr(info, files[name].encode("utf-8"),
                             compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    payload = out.getvalue()
    p.require(sum(len(value.encode("utf-8")) for value in files.values()) <= BUNDLE_MAX_BYTES,
              "handoff bundle exceeds uncompressed size budget")
    p.require(len(payload) <= BUNDLE_MAX_BYTES, "handoff bundle exceeds size budget")
    return payload, manifest, len(files)


def _publish_bundle(path, payload):
    """Publish a handoff bundle without replacing an existing target."""
    path = Path(path).expanduser().absolute()
    p.require(path.suffix == ".zip", "bundle output must end in .zip")
    parent = path.parent
    try:
        info = fs._check(parent)
        fs._validate(info, directory=True)
        existing = fs._check(path, missing=True)
        if existing is not None:
            raise fs.Error("refusing to overwrite existing bundle")
        fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=parent)
        temporary = Path(raw)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise fs.Error("refusing to overwrite existing bundle") from exc
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, UnicodeError, ValueError) as exc:
        raise fs.Error("cannot publish handoff bundle") from exc
    return path


def export_bundle(args, root):
    payload, manifest, files = _bundle_payload(root)
    destination = _publish_bundle(args.output, payload)
    return {"exported": True, "bundle": destination.name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "schema": manifest["schema"], "receipts": len(manifest["receipts"]),
            "files": files}


def verify_bundle(args, root):
    path = Path(args.bundle).expanduser().absolute()
    info = fs._check(path, missing=False)
    fs._validate(info)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = archive.namelist()
            p.require(len(names) == len(set(names)), "duplicate bundle entry")
            p.require("CURRENT.md" in names and "MANIFEST.json" in names, "bundle manifest is incomplete")
            total_size = 0
            for name in names:
                info = archive.getinfo(name)
                p.require(info.file_size <= (BUNDLE_MAX_BYTES if name == "MANIFEST.json" else p.MAX_BYTES), "bundle entry exceeds size budget")
                total_size += info.file_size
                p.require(total_size <= BUNDLE_MAX_BYTES, "bundle exceeds size budget")
                file_mode = (info.external_attr >> 16) & 0o170000
                p.require(file_mode in (0, 0o100000) and "\\" not in name and
                          name == name.strip() and not name.startswith("/") and ".." not in Path(name).parts,
                          "invalid bundle path")
                p.require(name in {"CURRENT.md", "MANIFEST.json"} or
                          re.fullmatch(r"receipts/[0-9a-f]{64}\.jsonl", name), "unexpected bundle entry")
            manifest_raw = archive.read("MANIFEST.json").decode("utf-8")
            scan(manifest_raw, enforce_limit=False)
            manifest = p.loads(manifest_raw)
            p.require(isinstance(manifest, dict) and set(manifest) ==
                      {"format", "schema", "project_id", "current_sha256", "receipts"},
                      "invalid bundle manifest")
            p.require(manifest["format"] == "project-continuity/handoff/v1" and
                      manifest["schema"] in p.SUPPORTED_SCHEMAS, "unsupported bundle format")
            p.identifier(manifest["project_id"])
            p.require(isinstance(manifest["current_sha256"], str) and
                      re.fullmatch(r"[0-9a-f]{64}", manifest["current_sha256"]), "invalid bundle checksum")
            receipt_names = manifest["receipts"]
            p.require(isinstance(receipt_names, list) and len(receipt_names) == len(set(receipt_names)),
                      "invalid bundle receipts")
            expected = {f"receipts/{_archive_name(name)}" for name in receipt_names}
            p.require(expected == {name for name in names if name.startswith("receipts/")},
                      "bundle receipt manifest mismatch")
            receipt_payloads = {name: archive.read(f"receipts/{name}") for name in receipt_names}
            current = archive.read("CURRENT.md").decode("utf-8")
            p.require(hashlib.sha256(current.encode("utf-8")).hexdigest() == manifest["current_sha256"],
                      "bundle current checksum mismatch")
    except (KeyError, OSError, UnicodeError, ValueError, zipfile.BadZipFile) as exc:
        raise fs.Error("invalid handoff bundle") from exc
    # Reuse the normal parser and archive-chain validator in an isolated,
    # throwaway directory; nothing from a bundle is written to the project.
    with tempfile.TemporaryDirectory() as raw:
        isolated = Path(raw).resolve()
        relay = isolated / ".relay"
        relay.mkdir(mode=0o700)
        (relay / "CURRENT.md").write_bytes(current.encode("utf-8"))
        if receipt_names:
            directory = relay / ARCHIVE_DIR
            directory.mkdir(mode=0o700)
            for name, payload in receipt_payloads.items():
                (directory / name).write_bytes(payload)
        doc, _, _, meta, state, matches = read(isolated)
        p.require(matches and meta["project_id"] == manifest["project_id"], "bundle state mismatch")
        p.require(meta["schema"] == manifest["schema"], "bundle schema mismatch")
        p.require(set(_reachable_archive_names(isolated, state, meta)) == set(receipt_names),
                  "bundle contains unreachable receipts")
        if state["extensions"].get("compaction") is not None:
            _archive_chain(isolated, state, meta)
    return {"valid": True, "schema": manifest["schema"], "project_id": manifest["project_id"],
            "receipts": len(receipt_names), "bytes": len(current.encode("utf-8"))}


def checked(document):
    scan(document)
    _, body, meta = split(document)
    if meta["schema"] in p.SUPPORTED_SCHEMAS:
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
            fs.atomic(ignore, "# Local by default. To share CURRENT.md only, replace * with /history/, /receipts/, /inbox/, /*.lock, /.*.tmp\n*\n")
        warnings = fs.atomic(current, doc)
    return {"revision": 0, "schema": p.SCHEMA, "warnings": warnings}


def status(args, root):
    path = fs.child(root, ".relay", "CURRENT.md")
    if not path.exists():
        return {"exists": False, "verification": "UNVERIFIED"}
    doc, lines, body, meta, state, matches = read(root)
    git = git_state.capture(root)
    size = len(doc.encode("utf-8"))
    archive = _archive_info(root, state, meta)
    result = {"exists": True, "schema": meta["schema"], "revision": int(meta["revision"]),
              "status": meta["status"], "writer": meta["writer"], "lease_until": meta["lease_until"],
              "lease_active": lease_active(meta), "git": git, "drift": baseline(meta) != git,
              "verification": "CONFLICT" if not matches else
              "DEGRADED" if archive["integrity"] == "invalid" else
              "UNVERIFIED" if git["kind"] != "git" else "structurally_valid",
              "bytes": size, "near_limit": size >= p.COMPACTION_TRIGGER_BYTES,
              "capacity": {"bytes": size, "max_bytes": p.MAX_BYTES,
                           "ratio": round(size / p.MAX_BYTES, 4),
                           "trigger_bytes": p.COMPACTION_TRIGGER_BYTES,
                           "target_bytes": p.COMPACTION_TARGET_BYTES,
                           "sections": _capacity_sections(doc, body)},
              "receipts": archive,
              "archive_integrity": archive["integrity"],
              "current_receipts": archive["current"],
              "archived_receipts": archive["archived"],
              "document_valid": bool(matches),
              "migration_required": meta["schema"] not in p.SUPPORTED_SCHEMAS,
              "requires_drift_review": baseline(meta) != git,
              "git_check_status": git["kind"],
              "capacity_status": "near_limit" if size >= p.COMPACTION_TRIGGER_BYTES else "available",
              "write_ready": bool(state) and matches and git["kind"] != "error" and
                             baseline(meta) == git and archive["integrity"] != "invalid" and size <= p.MAX_BYTES,
              "warnings": []}
    inbox = fs.child(root, ".relay", "inbox")
    result["pending_inbox"] = len(list(inbox.iterdir())) if inbox.exists() else 0
    if state:
        result.update(p.summary(state, git))
        preview_state, planned, would_compact = _plan_compaction(state, meta["project_id"])
        if would_compact:
            preview = metadata(lines, {"schema": p.SCHEMA_V3}) + p.render_body(preview_state, body)
            projected = len(preview.encode("utf-8"))
        else:
            projected = size
        result["receipts"].update({"reclaimable_bytes": max(0, size - projected),
                                   "projected_bytes": projected,
                                   "planned_segments": len(planned)})
        result["compact_recommended"] = bool(size >= p.COMPACTION_TRIGGER_BYTES or planned)
        result["compact_fits"] = projected <= p.MAX_BYTES
        result["write_ready"] = result["write_ready"] and result["compact_fits"]
        if result["compact_recommended"]:
            result["warnings"].append("capacity governance recommended; v2 requires explicit migration, v3 governs eligible writes")
        if not result["compact_fits"]:
            result["warnings"].append("receipt compaction cannot fit the current business content under 64 KiB")
        migration_info = state["extensions"].get("migration", {})
        pending = isinstance(migration_info, dict) and migration_info.get("mapping_review_required", False)
        result["mapping_review_required"] = bool(pending)
        if pending and matches:
            result["verification"] = "UNVERIFIED"
    else:
        result["migration_required"] = True
    if args.command == "validate":
        p.require(matches, "derived view conflict")
        if state and _archive_info(root, state, meta)["integrity"] == "invalid":
            p.require(False, "receipt archive integrity check failed")
    return result


def migration(document, body, meta, to_v3=False, state=None):
    if to_v3:
        p.require(meta["schema"] == p.SCHEMA_V2 and state is not None, "v2 to v3 migration requires a v2 document")
        out = copy.deepcopy(state)
        compaction = out["extensions"].get("compaction")
        # Leave an over-retained v2 operation list unmarked for the caller's
        # normal post-revision compaction pass; marking it first would make the
        # v3 validator reject the candidate before it can archive anything.
        if compaction is None and len(out["operations"]) <= p.RECEIPT_KEEP:
            out["extensions"]["compaction"] = {"schema": p.RECEIPT_SCHEMA, "head": None,
                                                   "count": 0, "retained": p.RECEIPT_KEEP}
        p.validate(out)
        return out, body, True
    p.require(meta["schema"] == "project-continuity/v1", "migration requires v1")
    state = p.empty_state("Migrated project")
    # Never infer a business lifecycle or verification claim from legacy prose.
    state["project"]["status"] = "paused"
    state["project"]["next_step"] = "Review preserved v1 sections and explicitly map current tasks."
    state["extensions"]["migration"] = {"source_sha256": hashlib.sha256(document.encode()).hexdigest(),
                                          "source_status": meta["status"], "mapping_review_required": True}
    return state, body.rstrip("\n") + "\n\n" + p.render_body(state), False


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


def mutate(args, root, dry_run=False):
    patch = input_patch(args) if args.command in ("update", "save") else {}
    p.require(args.expected_revision is not None and args.expected_revision >= 0, "expected revision required")
    p.identifier(args.writer)
    p.identifier(args.operation_id)
    p.require(0 < args.lease_minutes <= 1440, "lease must be 1 to 1440 minutes")
    hash_input = {"command": args.command, "writer": args.writer, "patch": patch,
                  "expected_revision": args.expected_revision, "lease_minutes": args.lease_minutes,
                  "allow_drift": args.allow_drift, "reason": getattr(args, "reason", None),
                  "snapshot": getattr(args, "snapshot", None), "source_sha256": getattr(args, "source_sha256", None)}
    # Preserve the v2 receipt hash for existing commands.  The opt-out is a
    # new input only when explicitly requested; compact has no v2 equivalent.
    if getattr(args, "no_auto_compact", False) or args.command == "compact":
        hash_input["no_auto_compact"] = bool(getattr(args, "no_auto_compact", False))
    if getattr(args, "to_v3", False):
        hash_input["to_v3"] = True
    operation_hash = p.digest(hash_input)
    # Never create relay or lock if CURRENT is absent.
    fs.child(root, ".relay", "CURRENT.md", exists=True)
    with (nullcontext() if dry_run else fs.locked(fs.child(root, ".relay", "CURRENT.md.lock"))):
        old, lines, body, meta, state, matches = read(root)
        archived = []
        if state:
            # A receipt still inline is sufficient to answer an idempotent
            # retry even if an unrelated old archive is degraded.
            for operation in state["operations"]:
                if operation["id"] == args.operation_id:
                    p.require(operation["hash"] == operation_hash, "operation ID reused with different input")
                    return {"revision": operation["revision"], "current_revision": int(meta["revision"]), "replayed": True}
        if state is not None and state["extensions"].get("compaction") is not None:
            # A damaged archive may still be displayed, but no mutation can
            # proceed because idempotent retry cannot be proven safely.
            try:
                archived = _archive_chain(root, state, meta)
            except (fs.Error, p.Invalid, OSError, UnicodeError, ValueError, TypeError, KeyError):
                p.require(False, "receipt archive integrity check failed; read-only recovery required")
        if state:
            for operation in archived:
                if operation["id"] == args.operation_id:
                    p.require(operation["hash"] == operation_hash, "operation ID reused with different input")
                    return {"revision": operation["revision"], "current_revision": int(meta["revision"]),
                            "replayed": True, "archived": True}
        p.require(int(meta["revision"]) == args.expected_revision, "revision conflict")
        p.require(matches or args.command == "recover", "derived view conflict; review direct edits before writing")
        git = git_state.capture(root)
        p.require(git["kind"] != "error", "Git inspection failed")
        old_git = baseline(meta)
        changed_branch = old_git.get("kind") == "git" and (git.get("kind") != "git" or old_git.get("branch") != git.get("branch"))
        if ((args.command == "resume" or (args.command == "migrate" and meta["schema"] in p.SUPPORTED_SCHEMAS)) and old_git != git) or changed_branch:
            p.require(args.allow_drift, "Git drift detected; review before --allow-drift")
        migrate_to_v3 = args.command == "migrate" and getattr(args, "to_v3", False)
        if args.command == "migrate":
            p.require(not lease_active(meta), "active lease prevents migration")
            p.require(args.source_sha256 == hashlib.sha256(old.encode()).hexdigest(), "migration source hash changed or missing")
            state, body, migrated_to_v3 = migration(old, body, meta, to_v3=migrate_to_v3, state=state)
            migrate_to_v3 = migrate_to_v3 or migrated_to_v3
        else:
            p.require(state is not None, "v1 is read-only; run migrate preview and explicit --apply")
        if args.command == "resume":
            p.require(not lease_active(meta) or meta["writer"] == args.writer, "lease conflict")
            if state["project"]["status"] == "paused":
                state["project"]["status"] = "active"
        elif args.command in ("update", "save", "compact"):
            p.require(meta["writer"] == args.writer and lease_active(meta), "missing, expired or conflicting lease; resume first")
            if args.command in ("update", "save"):
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
                p.require(snap_meta["project_id"] == meta["project_id"] and
                          snap_meta["schema"] in p.SUPPORTED_SCHEMAS, "snapshot project or schema mismatch")
                state, snap_matches = parsed_body(snap_body)
                p.require(snap_matches, "snapshot view conflict")
                # Preserve current user extensions/metadata text, replace only managed progress.
                state["operations"] = receipts
                state["extensions"].update(current_extensions)
            state["extensions"].setdefault("recovery_log", []).append({"reason": args.reason, "from_revision": int(meta["revision"]), "snapshot": args.snapshot})
        revision = int(meta["revision"]) + 1
        state["operations"].append({"id": args.operation_id, "hash": operation_hash, "revision": revision})
        before_bytes = len(old.encode("utf-8"))
        auto_enabled = not getattr(args, "no_auto_compact", False)
        compacted = False
        segments = []
        eligible = meta["schema"] == p.SCHEMA_V3 or migrate_to_v3
        if args.command == "compact":
            p.require(eligible, "v2 requires explicit migrate --to-v3 before compact")
        schema = p.SCHEMA_V3 if eligible else p.SCHEMA
        keep_lease = args.command in ("resume", "update", "compact")
        timestamp = now()
        updates = {"schema": schema, "revision": str(revision), "updated_at": iso(timestamp),
                   "writer": args.writer if keep_lease else "null",
                   "lease_until": iso(timestamp + timedelta(minutes=args.lease_minutes)) if keep_lease else "null",
                   "status": state["project"]["status"], **git_fields(git)}
        state, candidate, segments, compacted, preview_size, final_bytes = _plan_candidate(
            lines, body, state, updates, meta["project_id"],
            force=args.command == "compact" or migrate_to_v3, automatic=auto_enabled)
        if final_bytes > p.MAX_BYTES:
            hint = "explicit migrate --to-v3 is required" if schema == p.SCHEMA_V2 else "business capacity requires review"
            p.require(False, f"document exceeds 64 KiB; candidate={preview_size}, after={final_bytes}, "
                      f"excess={final_bytes - p.MAX_BYTES}; {hint}; no content was truncated")
        new = checked(candidate)
        if dry_run:
            return {"dry_run": True, "estimate_only": False, "would_fit": True,
                    "source_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                    "revision": int(meta["revision"]), "candidate_revision": revision,
                    "candidate_bytes": final_bytes, "ungoverned_candidate_bytes": preview_size,
                    "archived_segments": len(segments), "schema": schema,
                    "recheck_on_apply": True}
        # Archive segments are published before the history snapshot and the
        # CURRENT replacement.  Unreachable files after a precommit failure are
        # harmless and are never treated as committed receipts.
        archive_warnings = _store_archive_segments(root, segments)
        fs.child(root, ".relay", "history").mkdir(mode=0o700, exist_ok=True)
        result = fs.commit(fs.child(root, ".relay", "CURRENT.md", exists=True),
                           fs.child(root, ".relay", "history", exists=True), old, new, int(meta["revision"]))
        # Do not disclose a user's absolute path or reject a successful commit
        # merely because their directory name resembles sensitive content.
        result["history"] = Path(result["history"]).name
        result["warnings"] = archive_warnings + result.get("warnings", [])
        if final_bytes >= p.COMPACTION_TRIGGER_BYTES:
            result["warnings"].append("capacity remains at or above the 80% threshold")
        if compacted and final_bytes > p.COMPACTION_TARGET_BYTES:
            result["warnings"].append("compaction target was not reached; business content remains near the limit")
        result["compaction"] = {"applied": compacted, "before_bytes": before_bytes,
                                 "after_bytes": final_bytes, "ungoverned_candidate_bytes": preview_size,
                                 "archived_receipts": sum(len(payload.splitlines()) - 1 for _, payload in segments),
                                 "archived_segments": len(segments),
                                 "retained_receipts": len(state["operations"]),
                                 "target_bytes": p.COMPACTION_TARGET_BYTES,
                                 "target_reached": final_bytes <= p.COMPACTION_TARGET_BYTES,
                                 "near_limit": final_bytes >= p.COMPACTION_TRIGGER_BYTES}
        return {**result, "revision": revision, "schema": schema, "writer": updates["writer"],
                "lease_until": updates["lease_until"], "replayed": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for command in ("init", "status", "validate", "resume", "update", "save", "compact", "migrate", "recover", "export", "verify"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--root", default=".")
        if command == "init":
            cmd.add_argument("--project-id")
            cmd.add_argument("--name")
        if command in ("resume", "update", "save", "compact", "migrate", "recover"):
            required = command not in ("migrate", "compact")
            cmd.add_argument("--writer", required=required)
            cmd.add_argument("--expected-revision", type=int, required=required)
            cmd.add_argument("--operation-id", required=required)
            cmd.add_argument("--lease-minutes", type=int, default=30)
            cmd.add_argument("--allow-drift", action="store_true")
        if command in ("resume", "update", "save", "recover"):
            cmd.add_argument("--no-auto-compact", action="store_true",
                             help="disable automatic receipt compaction for this write")
        if command in ("update", "save"):
            cmd.add_argument("--input", help="JSON change file, or - for standard input")
        if command == "compact":
            cmd.add_argument("--apply", action="store_true", help="commit the compaction; otherwise preview only")
        if command == "migrate":
            cmd.add_argument("--apply", action="store_true")
            cmd.add_argument("--to-v3", action="store_true", help="upgrade a v2 document to the receipt-aware v3 format")
            cmd.add_argument("--source-sha256")
        if command == "recover":
            cmd.add_argument("--reason", required=True)
            cmd.add_argument("--snapshot")
        if command == "export":
            cmd.add_argument("--output", required=True, help="new .zip destination; existing files are refused")
        if command == "verify":
            cmd.add_argument("--bundle", required=True, help="handoff .zip to validate")
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
        elif args.command == "export":
            result = export_bundle(args, root)
        elif args.command == "verify":
            result = verify_bundle(args, root)
        elif args.command in ("status", "validate"):
            result = status(args, root)
        elif args.command in ("compact", "migrate") and not args.apply and all(
                value is not None for value in (args.writer, args.expected_revision, args.operation_id)):
            result = mutate(args, root, dry_run=True)
        elif args.command == "compact" and not args.apply:
            old, lines, body, meta, state, matches = read(root)
            p.require(state is not None, "v1 is read-only; run migrate preview and explicit --apply")
            p.require(matches, "derived view conflict; review direct edits before compacting")
            archive = _archive_info(root, state, meta)
            p.require(archive["integrity"] != "invalid", "receipt archive integrity check failed")
            schema = p.SCHEMA_V3
            candidate_state, candidate, segments, would_compact, _, projected = _plan_candidate(
                lines, body, state, {"schema": schema}, meta["project_id"], force=True)
            result = {"dry_run": True, "schema": meta["schema"], "target_schema": schema,
                      "revision": int(meta["revision"]), "before_bytes": len(old.encode()),
                      "candidate_bytes": projected, "would_compact": would_compact,
                      "archived_segments": len(segments), "retained_receipts": len(candidate_state["operations"]),
                      "target_bytes": p.COMPACTION_TARGET_BYTES,
                      "target_reached": projected <= p.COMPACTION_TARGET_BYTES,
                      "estimate_only": True, "excludes_new_operation_metadata": True,
                      "would_fit": projected <= p.MAX_BYTES, "archive_integrity": archive["integrity"]}
        elif args.command == "migrate" and not args.apply:
            old, lines, body, meta, state, _ = read(root)
            if getattr(args, "to_v3", False):
                p.require(state is not None, "v2 to v3 migration requires a v2 document")
                if meta["schema"] == p.SCHEMA_V3:
                    result = {"migration_required": False, "schema": p.SCHEMA_V3,
                              "revision": int(meta["revision"])}
                else:
                    p.require(meta["schema"] == p.SCHEMA_V2, "v2 to v3 migration requires a v2 document")
                    candidate_state, _, _ = migration(old, body, meta, to_v3=True, state=state)
                    candidate_state, candidate, planned_segments, _, _, projected = _plan_candidate(
                        lines, body, candidate_state, {"schema": p.SCHEMA_V3}, meta["project_id"], force=True)
                    checked(candidate)
                    result = {"dry_run": True, "source_sha256": hashlib.sha256(old.encode()).hexdigest(),
                              "from_schema": meta["schema"], "to_schema": p.SCHEMA_V3,
                              "revision": int(meta["revision"]), "candidate_bytes": len(candidate.encode()),
                              "planned_segments": len(planned_segments), "mapping_review_required": False}
            elif state is not None:
                result = {"migration_required": False, "revision": int(meta["revision"]), "schema": meta["schema"]}
            else:
                state, body, _ = migration(old, body, meta)
                candidate = checked(metadata(lines, {"schema": p.SCHEMA, "status": "paused"}) + body)
                result = {"dry_run": True, "source_sha256": hashlib.sha256(old.encode()).hexdigest(),
                          "revision": int(meta["revision"]), "candidate_bytes": len(candidate.encode()),
                          "mapping_review_required": True}
        else:
            result = mutate(args, root)
        output = json.dumps(result, ensure_ascii=False)
        # CLI diagnostics may legitimately be larger than CURRENT.md (for
        # example status includes task summaries); the relay document limit
        # applies only to the committed file, not stdout.
        scan(output, enforce_limit=False)
        print(output)
        return 0
    except (fs.Error, p.Invalid) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        # Do not leak arbitrary filenames, subprocess output, or input fragments.
        print('{"error":"invalid input or unavailable local resource"}', file=sys.stderr)
        return 2
