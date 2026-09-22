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
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import git_state
import objectstore as obs
import progress as p
import storage as fs
import v4 as relay_v4
from relay_errors import (
    CORRUPTION_CODES,
    RelayError,
    RELAY_PAGE_CURSOR_STALE,
    RELAY_CORRECTION_TARGET_UNREACHABLE,
    RELAY_CURRENT_CAPACITY_EXCEEDED,
    RELAY_OBJECT_SCHEMA_INVALID,
    RELAY_PREVIEW_LEASE_CONFLICT,
    RELAY_SCHEMA_V4_REQUIRES_RESOLVER,
    RELAY_VALIDATION_BUDGET_EXCEEDED,
)

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


def read_envelope(root):
    """Read CURRENT.md and its managed JSON without loading external objects."""
    current = fs.child(root, ".relay", "CURRENT.md", exists=True)
    document = fs.read(current)
    lines, body, meta = split(document)
    if meta["schema"] in p.SUPPORTED_SCHEMAS:
        state, matches = parsed_body(body)
        compaction = state["extensions"].get("compaction")
        p.require((meta["schema"] in (p.SCHEMA_V3, p.SCHEMA_V4, p.SCHEMA_V5))
                  == (compaction is not None),
                  "receipt archive metadata/schema mismatch")
        p.require(state["project"]["status"] == meta["status"], "metadata/project status conflict")
        if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS:
            p.require(p.is_external(state),
                      "an external document requires an external evidence index")
            p.require("corrections" not in state or isinstance(state["corrections"], dict),
                      "an external document requires an external corrections index")
        if meta["schema"] == p.SCHEMA_V5:
            p.require(isinstance(state.get("markdown"), dict)
                      and isinstance(state.get("ac_map"), dict),
                      "v5 requires external markdown and acceptance map indexes")
        else:
            p.require("markdown" not in state and "ac_map" not in state,
                      "only a v5 document externalises markdown and the acceptance map")
    else:
        state, matches = None, True
    return document, lines, body, meta, state, matches


def resolve(document_text, project_root=None):
    """Public resolver contract: the complete logical state of a document.

    project_root is mandatory for a v4 document.  An envelope parse never
    substitutes for this call.
    """
    return relay_v4.resolve(document_text, project_root)


def resolve_markdown(document_text, project_root=None):
    """Public resolver contract: the exact custom Markdown of a document.

    For a v5 document the text is reconstructed byte for byte from the bound
    objects, so a reader that looks for a marker keeps seeing the real content
    instead of the generated stub.
    """
    return relay_v4.resolve_markdown(document_text, project_root)


def envelope_state(document_text):
    """Read-only front matter plus managed JSON (never a completed state)."""
    return relay_v4.envelope_state(document_text)


def read(root):
    """Full documented read: v4 evidence is reconstructed from its objects."""
    document, lines, body, meta, state, matches = read_envelope(root)
    if state is not None and meta["schema"] in relay_v4.EXTERNAL_SCHEMAS:
        state = relay_v4.expand(state, root, meta["project_id"])
    return document, lines, body, meta, state, matches


def lease_active(meta):
    return meta["writer"] != "null" and datetime.fromisoformat(meta["lease_until"].replace("Z", "+00:00")) > now()


def baseline(meta):
    """The recorded Git baseline as written in the document front matter.

    Kept for compatibility: it never claims to be a current-environment check.
    """
    if "git_baseline" in meta:
        value = p.loads(meta["git_baseline"])
        p.require(isinstance(value, dict), "invalid Git baseline")
        scan(json.dumps(value, ensure_ascii=False))
        return value
    return {"kind": "legacy", "branch": meta.get("branch"), "head": meta.get("base_commit")}


# ---------------------------------------------------------------------------
# One read-only baseline collection per command.
#
# The recorded baseline, the observed environment and the comparison outcome are
# three different things and are reported separately.  A command never falls
# back to the historical baseline and then claims current verification: when the
# capture is unavailable, the reason is named and nothing is marked verified.
# ---------------------------------------------------------------------------

BASELINE_OK = "ok"
BASELINE_NOT_A_REPOSITORY = "not_a_repository"
BASELINE_TIMEOUT = "timeout"
BASELINE_CAPTURE_FAILED = "capture_failed"

# Comparison outcomes.  They are intentionally distinct from p.BASELINE_*, which
# describes one evidence record's own baseline rather than the environment check.
CHECK_PASS = "pass"
CHECK_STALE = "stale"
CHECK_UNAVAILABLE = "unavailable"
CHECK_UNCHECKED = "unchecked"
CHECK_NOT_A_REPOSITORY = "not_a_repository"
CHECK_TIMEOUT = "timeout"
CHECK_CAPTURE_FAILED = "capture_failed"

CHECK_REASONS = {
    CHECK_STALE: "baseline_mismatch",
    CHECK_UNAVAILABLE: "current_baseline_unavailable",
    CHECK_UNCHECKED: "baseline_not_checked",
    CHECK_NOT_A_REPOSITORY: "current_baseline_not_a_repository",
    CHECK_TIMEOUT: "current_baseline_timeout",
    CHECK_CAPTURE_FAILED: "current_baseline_capture_failed",
}


def capture_baseline(root):
    """Collect the current environment exactly once, with a named outcome."""
    try:
        value = git_state.capture(root)
    except Exception:  # noqa: BLE001 - a capture failure must not abort a read-only report
        value = {"kind": "error", "reason": "Git inspection unavailable"}
    if not isinstance(value, dict):
        value = {"kind": "error", "reason": "Git inspection returned no record"}
    kind = value.get("kind")
    if kind == "git":
        return {"state": BASELINE_OK, "reason": None, "kind": "git", "baseline": value,
                "fingerprint": value.get("fingerprint"), "head": value.get("head"),
                "branch": value.get("branch")}
    if kind == "none":
        return {"state": BASELINE_NOT_A_REPOSITORY, "reason": "current_baseline_not_a_repository",
                "kind": "none", "baseline": None, "fingerprint": None, "head": None,
                "branch": None}
    detail = value.get("reason")
    state, reason = BASELINE_CAPTURE_FAILED, "current_baseline_capture_failed"
    if isinstance(detail, str) and "timed out" in detail.lower():
        state, reason = BASELINE_TIMEOUT, "current_baseline_timeout"
    return {"state": state, "reason": reason, "kind": "error", "baseline": None,
            "fingerprint": None, "head": None, "branch": None}


def recorded_baseline(meta):
    """The recorded baseline plus its named state; never rewritten here."""
    if "git_baseline" not in meta:
        return {"state": "unchecked", "source": "absent", "kind": "legacy",
                "branch": meta.get("branch"), "head": meta.get("base_commit"),
                "fingerprint": None, "baseline": None}
    try:
        value = baseline(meta)
    except (p.Invalid, RelayError, ValueError, TypeError, KeyError):
        return {"state": "invalid", "source": "git_baseline", "kind": None, "branch": None,
                "head": None, "fingerprint": None, "baseline": None}
    if not isinstance(value, dict) or not value or value.get("kind") != "git":
        return {"state": "unavailable", "source": "git_baseline", "kind": value.get("kind"),
                "branch": value.get("branch"), "head": value.get("head"),
                "fingerprint": None, "baseline": None}
    return {"state": "recorded", "source": "git_baseline", "kind": "git",
            "branch": value.get("branch"), "head": value.get("head"),
            "fingerprint": value.get("fingerprint"), "baseline": value}


def compare_baseline(recorded, observed):
    """Compare one recorded baseline with one captured environment."""
    if observed is None:
        return CHECK_UNCHECKED, CHECK_REASONS[CHECK_UNCHECKED], False
    if observed["state"] != BASELINE_OK:
        return observed["state"], observed["reason"], False
    if recorded is None or recorded.get("state") == "unchecked" or recorded.get("baseline") is None:
        return CHECK_UNCHECKED, CHECK_REASONS[CHECK_UNCHECKED], False
    if recorded.get("state") != "recorded":
        return CHECK_UNAVAILABLE, "recorded_baseline_invalid", False
    if recorded["baseline"] != observed["baseline"]:
        return CHECK_STALE, CHECK_REASONS[CHECK_STALE], False
    return CHECK_PASS, None, True


def baseline_block(meta, observed):
    """The shared three-part baseline block: recorded, observed, comparison.

    ``observed=None`` means the caller never captured the environment: the block
    then names that as a state instead of pretending the recorded baseline is a
    current check.
    """
    if observed is None:
        observed = {"state": CHECK_UNCHECKED, "reason": CHECK_REASONS[CHECK_UNCHECKED],
                    "kind": "none", "baseline": None, "fingerprint": None, "head": None,
                    "branch": None}
    recorded = recorded_baseline(meta)
    state, reason, verified = compare_baseline(recorded, observed)
    return {"recorded": {"state": recorded["state"], "source": recorded["source"],
                         "kind": recorded["kind"], "branch": recorded["branch"],
                         "head": recorded["head"], "fingerprint": recorded["fingerprint"]},
            "observed": {"state": observed["state"], "kind": observed["kind"],
                         "branch": observed["branch"], "head": observed["head"],
                         "fingerprint": observed["fingerprint"]},
            "check": {"checked": state != CHECK_UNCHECKED, "state": state,
                      "verified": verified, "reason": reason,
                      "scope": "git_identity_and_working_tree_fingerprint",
                      "covers": ["branch", "head", "dirty", "untracked", "fingerprint"],
                      "does_not_cover": ["external_references", "seals", "test_suites",
                                         "provider_calls", "network"]}}


def baseline_verified(meta, observed):
    return compare_baseline(recorded_baseline(meta), observed)[2]


def external_check(args):
    """External references are only ever named, never inferred from Git."""
    acknowledged = bool(getattr(args, "acknowledge_external", False))
    return {"checked": acknowledged,
            "state": "acknowledged" if acknowledged else "not_checked",
            "reason": None if acknowledged else "external_checks_not_executed",
            "scope": "caller_declared",
            "not_run": ["external_references", "seal_bytes", "test_suites",
                        "provider_calls", "network_fetches"]}


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
    p.require(meta["schema"] in (p.SCHEMA_V3, p.SCHEMA_V4, p.SCHEMA_V5),
              "receipt archive requires project-continuity/v3, v4 or v5")
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


def _plan_candidate(lines, body, state, updates, project_id, *, force=False, automatic=True,
                    markdown=None):
    """Pure budget/render plan; caller supplies complete transaction metadata.

    When the external Markdown records are supplied the candidate's unmanaged
    region is regenerated from them, so a v5 document can never drift from the
    objects its index reference names.
    """
    def render(value, compact=False):
        region = body if markdown is None else p.render_region(
            body, p.render_markdown_stub(markdown))
        return metadata(lines, updates) + p.render_body(value, region, compact=compact)

    candidate = render(state)
    ungoverned_bytes = _candidate_bytes(candidate)
    segments, compacted = [], False
    _limit, trigger, target = p.budget(updates["schema"])
    if updates["schema"] in (p.SCHEMA_V3, p.SCHEMA_V4, p.SCHEMA_V5) and (force or
            (automatic and ungoverned_bytes >= trigger)):
        state, segments, compacted = _plan_compaction(state, project_id, force=True)
        candidate = render(state)
        if len(candidate.encode("utf-8")) > target:
            candidate = render(state, compact=True)
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


def _reachable_object_names(root, meta, stub):
    """Object files the committed state depends on (external schemas only)."""
    if meta["schema"] not in relay_v4.EXTERNAL_SCHEMAS or stub is None:
        return []
    names, budget = [], obs.Budget()
    fields = [("evidence", "evidence"), ("corrections", "correction")]
    if meta["schema"] == p.SCHEMA_V5:
        fields.extend([("markdown", "markdown"), ("ac_map", "ac-map")])
    for field, record_type in fields:
        ref = stub.get(field)
        if isinstance(ref, dict) and ref.get("index") is not None:
            names.extend(obs.index_paths(root, meta["project_id"], record_type,
                                         ref["index"], budget))
    return sorted(set(names))


def _bundle_payload(root):
    old, _lines, _body, meta, stub, matches = read_envelope(root)
    p.require(stub is not None, "v1 is read-only; migrate before exporting a handoff bundle")
    p.require(matches, "derived view conflict; review direct edits before exporting")
    names = _reachable_archive_names(root, stub, meta)
    files = {"CURRENT.md": old}
    directory = fs.child(root, ".relay", ARCHIVE_DIR, exists=True) if names else None
    for name in names:
        files[f"receipts/{name}"] = fs.read(fs.child(directory, name, exists=True))
    objects = _reachable_object_names(root, meta, stub)
    for name in objects:
        files[name] = fs.read_bytes(fs.child(root, ".relay", *name.split("/")),
                                    max_bytes=obs.OBJECT_MAX_BYTES)
    manifest = {"format": "project-continuity/handoff/v1", "schema": meta["schema"],
                "project_id": meta["project_id"],
                "current_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                "receipts": names, "objects": objects}
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
            value = files[name]
            archive.writestr(info, value.encode("utf-8") if isinstance(value, str) else value,
                             compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    payload = out.getvalue()
    p.require(sum(len(value.encode("utf-8")) if isinstance(value, str) else len(value)
                 for value in files.values()) <= BUNDLE_MAX_BYTES,
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
                entry_limit = (BUNDLE_MAX_BYTES if name == "MANIFEST.json"
                               else obs.OBJECT_MAX_BYTES if name.startswith("objects/")
                               else p.MAX_BYTES)
                p.require(info.file_size <= entry_limit, "bundle entry exceeds size budget")
                total_size += info.file_size
                p.require(total_size <= BUNDLE_MAX_BYTES, "bundle exceeds size budget")
                file_mode = (info.external_attr >> 16) & 0o170000
                p.require(file_mode in (0, 0o100000) and "\\" not in name and
                          name == name.strip() and not name.startswith("/") and ".." not in Path(name).parts,
                          "invalid bundle path")
                p.require(name in {"CURRENT.md", "MANIFEST.json"} or
                          re.fullmatch(r"receipts/[0-9a-f]{64}\.jsonl", name) or
                          re.fullmatch(r"objects/[a-z-]+(/[a-z-]+)?/[0-9a-f]{2}/[0-9a-f]{64}\.(json|bin)",
                                       name), "unexpected bundle entry")
            manifest_raw = archive.read("MANIFEST.json").decode("utf-8")
            scan(manifest_raw, enforce_limit=False)
            manifest = p.loads(manifest_raw)
            p.require(isinstance(manifest, dict) and set(manifest) in (
                      {"format", "schema", "project_id", "current_sha256", "receipts"},
                      {"format", "schema", "project_id", "current_sha256", "receipts",
                       "objects"}), "invalid bundle manifest")
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
            object_names = manifest.get("objects", [])
            p.require(isinstance(object_names, list)
                      and len(object_names) == len(set(object_names)), "invalid bundle objects")
            p.require(set(object_names) == {name for name in names if name.startswith("objects/")},
                      "bundle object manifest mismatch")
            object_payloads = {name: archive.read(name) for name in object_names}
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
        for name, payload in object_payloads.items():
            target = relay.joinpath(*name.split("/"))
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(payload)
        doc, _, _, meta, state, matches = read(isolated)
        p.require(matches and meta["project_id"] == manifest["project_id"], "bundle state mismatch")
        p.require(meta["schema"] == manifest["schema"], "bundle schema mismatch")
        p.require(set(_reachable_archive_names(isolated, state, meta)) == set(receipt_names),
                  "bundle contains unreachable receipts")
        if state["extensions"].get("compaction") is not None:
            _archive_chain(isolated, state, meta)
    return {"valid": True, "schema": manifest["schema"], "project_id": manifest["project_id"],
            "receipts": len(receipt_names), "bytes": len(current.encode("utf-8"))}


def _receipt_bytes(operation_id, revision):
    receipt = {"id": operation_id, "hash": "0" * 64, "revision": revision}
    return len(json.dumps(receipt, sort_keys=True, separators=(",", ":")))


def _plan_candidate_from_state(lines, body, state, updates, project_id, *, force=False,
                               automatic=True, markdown=None):
    """Plan one commit from a state that is already in memory."""
    _state, candidate, segments, would_compact, _ungoverned, projected = _plan_candidate(
        lines, body, copy.deepcopy(state), updates, project_id, force=force,
        automatic=automatic, markdown=markdown)
    return candidate, projected, segments, would_compact


def _commit_model(meta, lines, body, state, command, revision, operation_id, writer,
                  lease_minutes, stamp, *, patch=None, git=None, targets=None,
                  markdown=None):
    """Model one commit exactly as the write path performs it.

    An optional typed patch is applied first, using the same ``progress.apply``
    upsert as the commit, so a preview shares the commit's candidate builder
    instead of re-serialising the state.  ``git`` is the observed baseline that
    the commit would record; without it an evidence record would be modelled
    with a different (synthetic) baseline than the commit writes.
    """
    state = copy.deepcopy(state)
    if patch:
        state = p.apply(state, patch, git if git is not None else {"kind": "none"},
                        targets=targets)
    receipt = {"id": operation_id, "hash": "0" * 64, "revision": revision}
    state["operations"] = list(state["operations"]) + [receipt]
    status = state["project"]["status"]
    if command == "resume" and status == "paused":
        status = "active"
    if command == "save":
        # A save releases the lease: the writer and its deadline are cleared,
        # and an active project is parked.  Modelling it as a renewed resume
        # would predict bytes the commit never writes.
        if status == "active":
            status = "paused"
        updates = {"schema": meta["schema"], "revision": str(revision),
                   "updated_at": iso(stamp), "writer": "null",
                   "lease_until": "null", "status": status}
    else:
        updates = {"schema": meta["schema"], "revision": str(revision),
                   "updated_at": iso(stamp), "writer": writer,
                   "lease_until": iso(stamp + timedelta(minutes=lease_minutes)),
                   "status": status}
    if git is not None:
        # A real commit also rewrites the recorded Git fields.  When the preview
        # captured the same environment it must include them, or the predicted
        # byte count would silently exclude the front-matter change.
        updates.update(git_fields(git))
    _candidate, projected, segments, would_compact = _plan_candidate_from_state(
        lines, body, state, updates, meta["project_id"], markdown=markdown)
    receipt_bytes = len(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return {"command": command, "revision": revision, "operation_id": operation_id,
            "candidate_bytes": projected, "archived_segments": len(segments),
            "would_compact": bool(would_compact), "receipt_bytes": receipt_bytes,
            "writer_bytes": len(writer.encode("utf-8")),
            "operation_id_bytes": len(operation_id.encode("utf-8")),
            "revision_digits": len(str(revision)), "state": state,
            "patched": bool(patch)}


def _modelled_next_commit(lines, body, stub, meta):
    """Model the next commit including its own receipt, writer and lease.

    A preview that excludes the operation metadata cannot prove the next write
    fits, so the estimate always carries a modelled receipt.  The model reports
    its own assumptions, and the exact dry run with real arguments supersedes it
    when those arguments are supplied.
    """
    revision = int(meta["revision"]) + 1
    model = _commit_model(meta, lines, body, stub, "resume", revision, "operation",
                          "writer", 15, now())
    limit = p.budget(meta["schema"])[0]
    return {"modelled": True, "models": "resume", "candidate_bytes": model["candidate_bytes"],
            "would_fit": model["candidate_bytes"] <= limit,
            "would_compact": model["would_compact"],
            "archived_segments": model["archived_segments"],
            "model": {"operation_receipt_bytes": model["receipt_bytes"],
                      "operation_id_bytes": model["operation_id_bytes"],
                      "writer_bytes": model["writer_bytes"],
                      "lease_minutes": 15, "hash_bytes": 64,
                      "revision_digits": model["revision_digits"],
                      "operation_id": "operation", "writer": "writer"},
            "sensitivity_bytes_per_byte": {"operation_id": 1, "writer": 1,
                                           "next_step": 1},
            "recheck_on_apply": True}


def _unique_object_bytes(plans):
    """Planned bytes minus anything the store already holds."""
    planned, unique = 0, 0
    for plan in plans:
        size = len(plan["content"])
        planned += size
        if not os.path.lexists(plan["path"]):
            unique += size
    return planned, unique


def _record_cost(root, project_id, state, record, record_type, reference):
    """Object and index cost of one more record of a known class."""
    entry, plans = obs.entry_for(root, project_id, record_type, record)
    planned, unique = _unique_object_bytes(plans)
    index_planned, index_unique, index_nodes, split = 0, 0, 0, False
    ref = state.get(reference) if isinstance(state, dict) else None
    # Adding any record creates or rewrites at least one index node,
    # including the very first one, so the index cost is always modelled.
    entries = (obs.read_index(root, project_id, record_type, ref["index"], obs.Budget())
               if isinstance(ref, dict) and ref.get("index") is not None else [])
    split = len(entries) + 1 > obs.MAX_INDEX_ENTRIES
    index_plans, _sha = obs.plan_index(root, project_id, record_type, entries + [entry])
    index_planned, index_unique = _unique_object_bytes(index_plans)
    index_nodes = len(index_plans)
    return {"object_bytes": entry["object_bytes"], "storage": entry["storage"],
            "manifest": entry["manifest_sha256"], "envelope_plans": len(plans),
            "planned_object_bytes": planned, "new_object_bytes": unique,
            "index_planned_bytes": index_planned, "index_new_bytes": index_unique,
            "index_bytes": index_planned, "index_nodes": index_nodes,
            "index_split": split, "split": split}


def _growth_report(root, meta, lines, body, stub):
    """Marginal cost of one more record of each class, measured in memory.

    Every class reports the planned bytes and the bytes that would actually be
    published (byte-identical objects already in the store are not rewritten).
    A class that cannot be modelled is named unavailable rather than reported as
    zero, and the save cycle is two consecutive commits, not one renamed commit.
    """
    schema = meta["schema"]
    if not isinstance(stub, dict) or not stub.get("project"):
        # A document without a usable state cannot be modelled; the report
        # names that instead of pretending zero growth.
        return {"model": {"status": "unavailable",
                          "unavailable": "the document has no usable state",
                          "synthetic_sample": False}}
    project_id = meta["project_id"]
    tasks = stub.get("tasks") or []
    synthetic = not tasks or not any(task.get("acceptance") for task in tasks)
    task_id = tasks[0]["id"] if tasks else "growth-task"
    generation = tasks[0]["generation"] if tasks else 0
    condition = "growth acceptance condition"
    for task in tasks:
        if task.get("acceptance"):
            condition = task["acceptance"][0]
            break

    def document_bytes(value):
        return len((metadata(lines, {}) + p.render_body(value, body)).encode("utf-8"))

    base = document_bytes(stub)

    def measured(mutate, note=""):
        candidate = copy.deepcopy(stub)
        mutate(candidate)
        return {"current_bytes": document_bytes(candidate) - base,
                "planned_object_bytes": 0, "new_object_bytes": 0, "note": note}

    def bump(reference):
        def mutate(value):
            ref = value[reference]
            ref["count"] = ref["count"] + 1
            if ref["index"] is None:
                ref["index"] = "0" * 64
        return mutate

    report = {}
    if isinstance(stub.get("evidence"), dict):
        report["evidence"] = measured(
            bump("evidence"),
            note="document carries only the index reference; the record lives in the store")
    if isinstance(stub.get("corrections"), dict):
        report["correction"] = measured(
            bump("corrections"),
            note="document carries only the index reference; the relationship lives in the store")
    report["task"] = measured(
        lambda value: value["tasks"].append(
            {"id": "growth-task", "title": "growth task title", "status": "todo",
             "owner": None, "depends_on": [], "acceptance": [condition], "generation": 0}))
    if tasks:
        report["acceptance"] = measured(
            lambda value: value["tasks"][0].__setitem__(
                "acceptance", list(value["tasks"][0]["acceptance"]) + [condition + " extra"]))
    else:
        report["acceptance"] = {"current_bytes": 0, "planned_object_bytes": 0,
                                "new_object_bytes": 0,
                                "unavailable": "the project has no task to extend"}
    def ensure_sample_task(value):
        # A decision or blocker must reference a real task, so an empty
        # project needs one synthetic task before the sample is valid.
        if not any(task["id"] == task_id for task in value["tasks"]):
            value["tasks"].append({"id": task_id, "title": "growth sample task",
                                    "status": "todo", "owner": None,
                                    "depends_on": [],
                                    "acceptance": [condition], "generation": 0})

    def add_decision(value):
        ensure_sample_task(value)
        value["decisions"].append(
            {"id": "growth-decision", "task_ids": [task_id],
             "conclusion": "growth conclusion", "reason": "growth reason"})

    def add_blocker(value):
        ensure_sample_task(value)
        value["blockers"].append(
            {"id": "growth-blocker", "task_id": task_id,
             "description": "growth blocker", "status": "open"})

    report["decision"] = measured(add_decision,
        note="includes one synthetic sample task when the project has none")
    report["blocker"] = measured(add_blocker,
        note="includes one synthetic sample task when the project has none")
    revision = int(meta["revision"]) + 1
    report["operation_receipt"] = measured(
        lambda value: value["operations"].append(
            {"id": "growth-operation", "hash": "0" * 64, "revision": revision}))
    if schema in relay_v4.EXTERNAL_SCHEMAS:
        # Both classes are measured explicitly: a correction is the class a
        # reviewer is most likely to add, and an evidence record is the class a
        # normal round adds.  Neither is inferred from the other.
        evidence_record = {"id": "growth-evidence", "task_id": task_id,
                           "check": "growth probe", "result": "not_run",
                           "at": iso(now()), "ref": "growth/probe", "baseline": {},
                           "acceptance": [condition], "generation": generation}
        evidence_cost = _record_cost(root, project_id, stub, evidence_record,
                                     "evidence", "evidence")
        report.setdefault("evidence", measured(
            bump("evidence"),
            note="the document carries only the index reference")
        ).update(evidence_cost)
        report["evidence"]["note"] += " (new envelope plus rewritten index nodes)"
        report["evidence"]["index_created"] = evidence_cost["index_nodes"] > 0
        corrections_ref = stub.get("corrections")
        correction_record = {"id": "growth-correction", "kind": "correction",
                             "target_type": "evidence", "target_id": "growth-target",
                             "replacement_id": None, "reason": "growth probe",
                             "at": iso(now()), "target_sha256": "0" * 64}
        if isinstance(corrections_ref, dict):
            correction_cost = _record_cost(root, project_id, stub, correction_record,
                                           "correction", "corrections")
            report.setdefault("correction", measured(
                bump("corrections"),
                note="the document carries only the index reference")
            ).update(correction_cost)
            report["correction"]["note"] += (" (new relationship object plus rewritten"
                                             " corrections index nodes)")
            report["correction"]["index_created"] = (
                corrections_ref.get("index") is None and correction_cost["index_nodes"] > 0)
        else:
            report["correction"] = {"current_bytes": 0, "planned_object_bytes": 0,
                                    "new_object_bytes": 0,
                                    "unavailable": "this document has no corrections index"}

    stamp = now()
    writer = "growth-writer"
    first = _commit_model(meta, lines, body, stub, "resume", revision, "growth-resume",
                          writer, 30, stamp)
    second = _commit_model(meta, lines, body, first["state"], "save", revision + 1,
                           "growth-save", writer, 30, stamp)
    report["save_cycle"] = {
        "commits": 2,
        "operations": [{"command": first["command"], "revision": first["revision"],
                        "operation_id": first["operation_id"],
                        "receipt_bytes": first["receipt_bytes"],
                        "candidate_bytes": first["candidate_bytes"],
                        "revision_digits": first["revision_digits"]},
                       {"command": second["command"], "revision": second["revision"],
                        "operation_id": second["operation_id"],
                        "receipt_bytes": second["receipt_bytes"],
                        "candidate_bytes": second["candidate_bytes"],
                        "revision_digits": second["revision_digits"]}],
        "current_bytes": second["candidate_bytes"] - base,
        "current_delta_bytes": second["candidate_bytes"] - base,
        "object_bytes": 0, "planned_object_bytes": 0, "new_object_bytes": 0,
        "revision_digits": second["revision_digits"],
        "predicted_end_bytes": second["candidate_bytes"],
        "would_compact": first["would_compact"] or second["would_compact"],
        "archived_segments": first["archived_segments"] + second["archived_segments"],
        "operation_receipt_bytes": first["receipt_bytes"] + second["receipt_bytes"],
        "note": "two consecutive commits planned from one virtual start state",
    }
    report["model"] = {"status": "modelled", "synthetic_sample": synthetic,
                       "sample_task_id": task_id, "sample_generation": generation,
                       "commands": ["resume", "save"], "clock": iso(stamp),
                       "assumptions": ["operation ids growth-resume/growth-save",
                                       "writer growth-writer with a 30 minute lease",
                                       "receipt compaction is left to the writepath"],
                       "limitations": ["a real commit with another writer, operation id",
                                       " or clock differs in the bytes it names"]}
    return report


LONG_RECORD_HINT_BYTES = 4096
LONG_RECORD_FIELDS = {"tasks": ("title", "acceptance"),
                      "blockers": ("description", "resolution"),
                      "evidence": ("check", "ref"),
                      "decisions": ("conclusion", "reason")}


def _step_summary(model, limit):
    """One planned commit reduced to the capacity-relevant numbers."""
    return {"command": model["command"], "revision": model["revision"],
            "operation_id": model["operation_id"],
            "candidate_bytes": model["candidate_bytes"],
            "would_fit": model["candidate_bytes"] <= limit,
            "headroom_bytes": limit - model["candidate_bytes"],
            "would_compact": bool(model["would_compact"]),
            "archived_segments": model["archived_segments"]}


def _next_save_cycle(lines, body, state, meta, schema, revision, *, writer="next-writer",
                     lease_minutes=30, stamp=None):
    """Second-order estimate: the next ordinary resume -> save cycle.

    Modelled with the same planner as a commit, from the state that was just
    written.  It is an estimate, never a reservation: the real commit rereads
    under the lock and is not bound by this prediction.
    """
    stamp = stamp or now()
    limit = p.budget(schema)[0]
    resume_op, save_op = writer + "-resume", writer + "-save"
    first = _commit_model(meta, lines, body, state, "resume", revision + 1, resume_op,
                          writer, lease_minutes, stamp)
    second = _commit_model(meta, lines, body, first["state"], "save", revision + 2,
                           save_op, writer, lease_minutes, stamp)
    steps = [_step_summary(first, limit), _step_summary(second, limit)]
    return {"status": "modelled", "commands": ["resume", "save"], "revision": revision,
            "steps": steps, "would_fit": all(step["would_fit"] for step in steps),
            "end_bytes": second["candidate_bytes"],
            "end_headroom_bytes": limit - second["candidate_bytes"],
            "would_compact": first["would_compact"] or second["would_compact"],
            "model": {"writer": writer, "operation_ids": [resume_op, save_op],
                      "lease_minutes": lease_minutes, "clock": iso(stamp),
                      "patch": "none (a metadata-only maintenance cycle)"},
            "assumptions": ["the project's business content is unchanged",
                            "the cycle is one resume followed by one save"],
            "invalidated_by": ["a different writer or operation id length",
                               "business content added or evidence recorded before the cycle",
                               "another concurrent commit changing the revision",
                               "receipt compaction triggered by a different history"]}


def _capacity_receipt(schema, before_bytes, after_bytes, *, committed, replayed=False,
                      object_store_new_bytes=0, archived_segments=0,
                      next_save_cycle=None, next_cycle_reason=None):
    """Uniform, backward-compatible capacity block for a write result.

    `delta_bytes` is the CURRENT.md net change only; object-store bytes are
    reported separately so the two are never added together.  A replay reports
    the current observed occupancy, never a fresh commit, and a model that could
    not be computed is named rather than reported as a reassuring zero.
    """
    limit, trigger, target = p.budget(schema)
    used = after_bytes
    near = used >= trigger
    if replayed:
        action = "replay: no new bytes were committed"
    elif not committed:
        action = "no commit was attempted"
    elif used > limit:
        action = "over the limit: migration or business review required"
    elif near:
        action = ("above the compaction trigger: compact receipts or migrate to an "
                  "external schema before the next write")
    else:
        action = "none"
    if next_save_cycle is None:
        next_save_cycle = {"status": "not_computed",
                           "reason": next_cycle_reason or "not modelled"}
    return {"committed": committed, "replayed": replayed, "used_bytes": used,
            "limit_bytes": limit, "headroom_bytes": limit - used, "delta_bytes": after_bytes - before_bytes,
            "trigger_bytes": trigger, "target_bytes": target, "near_limit": near,
            "recommended_action": action, "object_store_new_bytes": object_store_new_bytes,
            "archived_segments": archived_segments,
            "next_save_cycle_estimate": next_save_cycle}


def _record_field_bytes(record, field):
    value = record.get(field)
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _reference_kind(value):
    """A protocol object reference is provable; a plain path is only a pointer."""
    if not isinstance(value, str):
        return "not_a_reference"
    if re.match(r"^objects/", value) or ".relay/objects/" in value:
        return "protocol_object_reference"
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return "content_digest"
    return "plain_path_reference"


def _long_record_hints(state, threshold):
    """Non-blocking, byte-based hints; never truncates or rewrites content.

    `result` on an evidence record is an enum and is intentionally not treated
    as prose.  The report locates the collection, record id and field and never
    echoes the value, so a large note is not duplicated into the report.
    """
    hints, external = [], []
    for collection, fields in LONG_RECORD_FIELDS.items():
        rows = state.get(collection)
        if isinstance(rows, dict):
            external.append({"collection": collection,
                             "reason": "collection is externalised; it does not grow CURRENT.md"})
            continue
        for record in rows or []:
            if not isinstance(record, dict):
                continue
            base = {"collection": collection, "id": record.get("id")}
            for field in fields:
                size = _record_field_bytes(record, field)
                if size >= threshold:
                    hints.append({**base, "field": field, "bytes": size,
                                  "recommended": "keep a pointer here and put the detail in an evidence root"})
            total = len(json.dumps(record, ensure_ascii=False).encode("utf-8"))
            if total >= threshold:
                hints.append({**base, "field": "(record)", "bytes": total,
                              "recommended": "split the record or reference an evidence root"})
    references = [{"id": record.get("id"), "kind": _reference_kind(record.get("ref"))}
                  for record in (state.get("evidence") or [])
                  if isinstance(record, dict)]
    return {"threshold_bytes": threshold, "count": len(hints), "hints": hints,
            "external_collections": external, "references": references}


def _patch_preview(root, args, lines, body, meta, stub, patch):
    """Read-only preview of a typed patch, reusing the commit's planner.

    Never creates a lock, lease, object, receipt or history file and never
    changes CURRENT.md.  The real commit rereads and revalidates under the lock.
    """
    schema = meta["schema"]
    limit, _trigger, _target = p.budget(schema)
    writer = getattr(args, "writer", None) or "preview-writer"
    lease_minutes = getattr(args, "lease_minutes", None) or 30
    operation_id = getattr(args, "operation_id", None)
    if schema in relay_v4.EXTERNAL_SCHEMAS:
        full = relay_v4.expand(stub, root, meta["project_id"])
    else:
        full = copy.deepcopy(stub)
    resolver = relay_v4.make_target_resolver(root, meta["project_id"], stub)
    git = git_state.capture(root)
    if git.get("kind") == "error":
        return {"status": "unavailable", "reason": "Git inspection failed; preview refused"}
    md_patch = patch.get("markdown") if isinstance(patch, dict) else None
    markdown_records = None
    if md_patch is not None:
        p.require(schema == p.SCHEMA_V5, "markdown changes require a v5 document")
        markdown_records = relay_v4.collect_markdown(stub, root, meta["project_id"])
        markdown_records, _change = p.merge_markdown(markdown_records, md_patch)
    apply_patch = ({key: value for key, value in patch.items() if key != "markdown"}
                   if md_patch is not None else patch)
    stamp = now()
    revision = int(meta["revision"])
    active = lease_active(meta)
    if active and meta["writer"] != writer:
        raise RelayError(RELAY_PREVIEW_LEASE_CONFLICT,
                         "another writer holds a live lease; no commit can be predicted")
    if active:
        model = _commit_model(meta, lines, body, full, "save", revision + 1,
                              operation_id or "preview-save", writer, lease_minutes, stamp,
                              patch=apply_patch, git=git, targets=resolver,
                              markdown=markdown_records if schema == p.SCHEMA_V5 else None)
        steps, mode, commands = [model], "save", ["save"]
    else:
        resume_op = (operation_id + "-resume") if operation_id else "preview-resume"
        save_op = (operation_id + "-save") if operation_id else "preview-save"
        first = _commit_model(meta, lines, body, full, "resume", revision + 1, resume_op,
                              writer, lease_minutes, stamp, git=git, targets=resolver)
        second = _commit_model(meta, lines, body, first["state"], "save", revision + 2,
                               save_op, writer, lease_minutes, stamp, patch=apply_patch,
                               git=git, targets=resolver,
                               markdown=markdown_records if schema == p.SCHEMA_V5 else None)
        steps, mode, commands = [first, second], "cycle", ["resume", "save"]
    summaries = [_step_summary(step, limit) for step in steps]
    final_bytes = summaries[-1]["candidate_bytes"]
    result = {"status": "modelled", "mode": mode, "commands": commands, "schema": schema,
              "revision": revision, "steps": summaries, "final_candidate_bytes": final_bytes,
              "final_headroom_bytes": limit - final_bytes,
              "would_fit": all(step["would_fit"] for step in summaries),
              "would_compact": any(step["would_compact"] for step in summaries),
              "model": {"writer": writer, "operation_ids": [step["operation_id"] for step in summaries],
                        "lease_minutes": lease_minutes, "clock": iso(stamp),
                        "patch_fields": sorted(patch) if isinstance(patch, dict) else []},
              "assumptions": ["the typed patch is applied on the save commit",
                              "the preview models the same receipt, lease and derived view as a commit"],
              "invalidated_by": ["a concurrent commit changing the revision",
                                 "a different lease state at commit time",
                                 "business content added outside the patch"],
              "read_only": True, "created_files": False, "recheck_on_apply": True}
    if isinstance(patch, dict) and "project" in patch:
        next_step = patch["project"].get("next_step")
        if isinstance(next_step, str):
            result["next_step_bytes"] = len(next_step.encode("utf-8"))
    if result["would_fit"]:
        try:
            nxt = _next_save_cycle(lines, body, steps[-1]["state"], meta, schema,
                                   steps[-1]["revision"], writer=writer,
                                   lease_minutes=lease_minutes, stamp=stamp)
            result["next_save_cycle_estimate"] = nxt
            if not nxt["would_fit"] or nxt["end_headroom_bytes"] < 0:
                result["recommended_action"] = (
                    "this save fits, but the next ordinary resume->save cycle does not; "
                    "compact receipts or migrate to an external schema first")
        except (RelayError, p.Invalid, fs.Error, OSError, ValueError, KeyError) as exc:
            result["next_save_cycle_estimate"] = {
                "status": "not_computed",
                "reason": exc.code if isinstance(exc, RelayError) else "model unavailable"}
    return result


def capacity_view(args, root):
    """Read-only capacity: current file, object store and the next commit."""
    path = fs.child(root, ".relay", "CURRENT.md")
    if not path.exists():
        return {"exists": False, "verification": "UNVERIFIED"}
    document, lines, body, meta, stub, _matches = read_envelope(root)
    size = len(document.encode("utf-8"))
    limit, trigger, target = p.budget(meta["schema"])
    stats = obs.store_stats(root)
    result = {"exists": True, "schema": meta["schema"], "revision": int(meta["revision"]),
              "current": {"bytes": size, "limit_bytes": limit,
                          "protocol_max_bytes": p.MAX_BYTES,
                          "headroom_bytes": limit - size,
                          "trigger_bytes": trigger, "target_bytes": target,
                          "ratio": round(size / limit, 4),
                          "sections": _capacity_sections(document, body)},
              "objects": {"files": stats["files"], "bytes": stats["bytes"],
                          "soft_quota_bytes": obs.OBJECT_SOFT_QUOTA_BYTES,
                          "hard_quota_bytes": obs.OBJECT_HARD_QUOTA_BYTES,
                          "disk_free_bytes": obs.disk_free(root),
                          "reserve_bytes": obs.DISK_RESERVE_BYTES},
              "garbage_collection": "never automatic"}
    threshold = getattr(args, "long_record_threshold", None)
    if threshold is None:
        threshold = LONG_RECORD_HINT_BYTES
    if stub is not None:
        result["long_records"] = _long_record_hints(stub, threshold)
    patch = input_patch(args) if getattr(args, "input", None) else None
    if patch is not None:
        p.require(stub is not None, "v1 is read-only; migrate before previewing a patch")
        result["patch_preview"] = _patch_preview(root, args, lines, body, meta, stub, patch)
    if patch is None and all(value is not None for value in (getattr(args, "writer", None),
                                                             getattr(args, "expected_revision", None),
                                                             getattr(args, "operation_id", None))):
        # Model the realistic next write (a resume) so that the predicted byte
        # count is the one the commit will actually produce.  The dry run runs the
        # same planning code as the commit, so with identical arguments the two
        # byte counts are equal rather than merely close.
        preview_args = argparse.Namespace(**vars(args))
        preview_args.command = "resume"
        preview_args.input = None
        preview_args.allow_drift = False
        preview_args.no_auto_compact = False
        preview = mutate(preview_args, root, dry_run=True)
        model = {"operation_id": getattr(args, "operation_id", None),
                 "writer": getattr(args, "writer", None),
                 "lease_minutes": getattr(args, "lease_minutes", None),
                 "revision": int(meta["revision"]) + 1,
                 "revision_digits": len(str(int(meta["revision"]) + 1)),
                 "operation_receipt_bytes": _receipt_bytes(getattr(args, "operation_id", None),
                                                           int(meta["revision"]) + 1)}
        result["next_commit"] = {"estimate_only": False,
                                 "excludes_new_operation_metadata": False,
                                 "modelled": False, "models": "resume",
                                 "candidate_bytes": preview["candidate_bytes"],
                                 "ungoverned_candidate_bytes":
                                     preview.get("ungoverned_candidate_bytes"),
                                 "schema": preview.get("schema"),
                                 "objects": preview.get("objects", {}),
                                 "would_compact": bool(preview.get("archived_segments")),
                                 "archived_segments": preview.get("archived_segments"),
                                 "model": model,
                                 "would_fit": preview.get("would_fit", False)}
    elif stub is None:
        result["next_commit"] = {"estimate_only": True, "unavailable": "v1 is read-only",
                                 "excludes_new_operation_metadata": True,
                                 "modelled": False, "would_fit": None}
    else:
        result["next_commit"] = _modelled_next_commit(lines, body, stub, meta)
        result["next_commit"]["estimate_only"] = True
        result["next_commit"]["excludes_new_operation_metadata"] = False
    if stub is not None:
        try:
            result["growth"] = _growth_report(root, meta, lines, body, stub)
        except (RelayError, p.Invalid, fs.Error, OSError, ValueError, KeyError) as exc:
            detail = exc.code if isinstance(exc, RelayError) else "growth model unavailable"
            result["growth"] = {"error": detail}
    scan(json.dumps(result, ensure_ascii=False), enforce_limit=False)
    return result


def coverage_check(rows, coverage_view_data):
    """L3 coverage as one named check: never a stand-in for L4 verification."""
    uncovered = [row for row in rows if not row["covered"]]
    withdrawn = sum(row["withdrawn_page"]["total"] for row in rows)
    return {"checked": True, "state": "covered" if not uncovered else "uncovered",
            "reason": None if not uncovered else "uncovered_acceptance",
            "covered": len(rows) - len(uncovered), "uncovered": len(uncovered),
            "withdrawn": withdrawn, "applicable": bool(rows)}


def mapping_check(state):
    migration = state["extensions"].get("mapping") or {}
    pending = bool(isinstance(migration, dict) and migration.get("mapping_review_required"))
    return {"checked": True, "state": "review_required" if pending else "not_required",
            "reason": "mapping_review_required" if pending else None, "applicable": True}


def baseline_check_of(baseline_block_value, rows):
    """L4 as one named check, derived from the baseline block and coverage rows."""
    check = baseline_block_value["check"]
    contributors = [key for row in rows for key in row["contributors"]]
    if check["verified"]:
        state, reason = "verified", None
    elif check["state"] == CHECK_UNCHECKED:
        state, reason = "not_checked", "baseline_not_checked"
    elif check["state"] == CHECK_PASS and not contributors:
        state, reason = "not_applicable", "no_effective_pass"
    else:
        state, reason = check["state"], check["reason"]
    return {"checked": check["checked"], "state": state, "reason": reason,
            "applicable": bool(contributors), "verified": check["verified"],
            "recorded_state": baseline_block_value["recorded"]["state"],
            "observed_state": baseline_block_value["observed"]["state"],
            "scope": check["scope"], "does_not_cover": check["does_not_cover"]}


def integrity_check(root, meta, stub, enabled, state=None):
    """A full object read is only performed when it is explicitly requested."""
    if not enabled:
        return {"checked": False, "state": "not_checked", "reason": "integrity_not_checked",
                "applicable": meta["schema"] in relay_v4.EXTERNAL_SCHEMAS,
                "objects_checked": 0, "verified": False}
    return relay_v4.object_integrity(root, meta["project_id"], stub, meta["schema"])


MANDATORY_CHECKS = ("coverage", "baseline", "mapping")
CHECK_ORDER = ("coverage", "baseline", "mapping", "integrity", "external")


def _check_state(value):
    return value.get("state") if isinstance(value, dict) else value


def verification_record(checks, required=(), complete=True, reason=None):
    """The explicit verification record every reader must be able to audit.

    A mandatory check that did not pass blocks a verified conclusion.  Checks
    that were not requested are reported as not_checked: they are never
    silently treated as passing just because another check succeeded.
    """
    checks = dict(checks)
    checks.setdefault("integrity", {"checked": False, "state": "not_checked",
                                    "reason": "integrity_not_checked"})
    checks.setdefault("external", {"checked": False, "state": "not_checked",
                                   "reason": "external_checks_not_executed"})
    # A check that does not apply to this schema blocks nothing: it is reported
    # as not applicable instead of being folded into a pass or a failure.
    failures = [(name, check) for name, check in checks.items()
                if name in required and (check.get("applicable", True)
                                         if isinstance(check, dict) else True)
                and _check_state(check) != "verified"]
    not_checked = sorted(name for name, check in checks.items()
                         if _check_state(check) == "not_checked")
    if not complete:
        return {"schema": "project-continuity/verification-record/v1", "verified": False,
                "reason": "pagination_incomplete", "complete": False,
                "checks": checks, "required": list(required), "not_checked": not_checked,
                "scope": {"restricted_to": list(CHECK_ORDER),
                          "external_checks_executed": bool(checks["external"].get("checked")),
                          "integrity_check_executed": bool(checks["integrity"].get("checked")),
                          "conclusion": "pagination is incomplete; no handoff conclusion"},
                "basis": "read_only_local"}
    if failures:
        name, check = failures[0]
        reason = _check_reason(name, check)
        verified = False
    else:
        verified = True
    return {"schema": "project-continuity/verification-record/v1", "verified": verified,
            "reason": reason, "complete": True, "checks": checks, "required": list(required),
            "not_checked": not_checked,
            "scope": {"restricted_to": list(CHECK_ORDER),
                      "external_checks_executed": bool(checks["external"].get("checked")),
                      "integrity_check_executed": bool(checks["integrity"].get("checked")),
                      "conclusion": _conclusion(verified, reason, not_checked)},
            "basis": "read_only_local"}


def _check_reason(name, check):
    reason = check.get("reason") if isinstance(check, dict) else None
    if reason:
        return reason
    if name == "coverage" and _check_state(check) == "uncovered":
        return "uncovered_acceptance"
    return name + "_" + str(_check_state(check))


def _conclusion(verified, reason, not_checked):
    if verified and not_checked:
        return ("verified for the requested scope; not asserted: " + ", ".join(not_checked))
    if verified:
        return "verified for the requested scope"
    return "not verified: " + str(reason)


def markdown_view(args, root):
    """Read-only: the exact custom Markdown text of a document (v4 or v5).

    For a v5 document the text is reconstructed byte for byte from the bound,
    content-addressed objects, so externalising it never loses a byte and never
    silently drops a section.  Checksums are reported so the restored text can
    be verified without trusting this output.
    """
    document, _lines, _body, meta, stub, matches = read_envelope(root)
    p.require(stub is not None, "v1 is read-only; migrate before reading markdown")
    p.require(matches, "derived view conflict; review direct edits before reading markdown")
    selector = getattr(args, "section", None)
    if meta["schema"] != p.SCHEMA_V5:
        p.require(selector is None, "sections are addressable only on a v5 document")
        text = relay_v4.resolve_markdown(document, root)
        return {"schema": meta["schema"], "revision": int(meta["revision"]),
                "external": False, "count": 0, "sections": [],
                "bytes": len(text.encode("utf-8")),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text}
    records = relay_v4.collect_markdown(stub, root, meta["project_id"])
    sections = [{"id": record["id"], "ordinal": record["ordinal"], "title": record["title"],
                 "bytes": record["bytes"], "sha256": record["sha256"]} for record in records]
    if selector is not None:
        p.identifier(selector)
        chosen = [record for record in records if record["id"] == selector]
        p.require(len(chosen) == 1, "unknown markdown section")
        record = chosen[0]
        return {"schema": meta["schema"], "revision": int(meta["revision"]),
                "external": True, "section": record["id"], "title": record["title"],
                "bytes": record["bytes"], "sha256": record["sha256"], "text": record["content"]}
    text = p.join_markdown(records)
    return {"schema": meta["schema"], "revision": int(meta["revision"]),
            "external": True, "sections": sections, "count": len(sections),
            "bytes": len(text.encode("utf-8")),
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "provenance": (stub.get("extensions", {}) or {}).get("external_markdown"),
            "provenance_meaning": {
                "source_revision": "the revision the text was FIRST externalised from; it is not the revision of the last content change",
                "content_identity": ["bytes", "sha256", "sections"],
                "freshness_rule": "compare sha256/bytes with the bound sections; source_revision alone never proves the text is stale"},
            "text": text}


def coverage_view(args, root):
    """Read-only evidence x acceptance-condition matrix.

    The current environment is captured here, exactly once, and compared with
    the recorded baseline.  Covered (L3) and verified (L4) stay orthogonal: a
    stale baseline never erases coverage and a complete coverage never stands
    in for a baseline check.
    """
    document, lines, body, meta, stub, matches = read_envelope(root)
    p.require(stub is not None, "v1 is read-only; migrate before coverage")
    p.require(matches, "derived view conflict; review direct edits before reading coverage")
    observed = capture_baseline(root)
    block = baseline_block(meta, observed)
    state = (relay_v4.expand(stub, root, meta["project_id"])
             if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS else stub)
    content_sha = hashlib.sha256(document.encode("utf-8")).hexdigest()
    result = relay_v4.coverage(state, meta["project_id"], getattr(args, "task", None),
                               getattr(args, "limit", None) or 20,
                               baseline=observed["baseline"],
                               current_sha256=content_sha,
                               cursor=getattr(args, "acceptance_cursor", None),
                               revision=meta["revision"], schema_version=meta["schema"],
                               integrity=integrity_check(root, meta, stub,
                                                         bool(getattr(args, "verify_integrity",
                                                                  False)), state),
                               flags={"acknowledge_external":
                                      bool(getattr(args, "acknowledge_external", False))},
                               block=block)
    # The CLI contributes only its own observation of the environment and its
    # integrity/external flags; the check set and the verification record come
    # from the view so that library and CLI callers share one judgement.
    result["recorded_baseline"] = block["recorded"]
    result["observed_baseline"] = block["observed"]
    result["baseline"] = block
    result["revision"] = int(meta["revision"])
    result["schema_version"] = meta["schema"]
    scan(json.dumps(result, ensure_ascii=False), enforce_limit=False)
    return result


def handoff_view(args, root):
    """Read-only derived handoff; missing references fail, never succeed.

    Verification is derived from explicit checks.  A matching Git identity is a
    baseline check, never proof that an external reference, a seal, a test suite
    or a provider call was re-verified.
    """
    document, lines, body, meta, stub, matches = read_envelope(root)
    p.require(stub is not None, "v1 is read-only; migrate before a handoff")
    p.require(matches, "derived view conflict; review direct edits before a handoff")
    observed = capture_baseline(root)
    block = baseline_block(meta, observed)
    requested = getattr(args, "cursor", None)
    # One named refusal for every way a stale cursor can be detected: a token
    # minted for another document, for another revision, or for no page at all.
    page_error = RelayError(RELAY_PAGE_CURSOR_STALE, "acceptance cursor is stale for this document")
    try:
        relay_v4.check_page_cursor(requested, {"revision": int(meta["revision"]),
                                               "current_sha256":
                                                   hashlib.sha256(document.encode("utf-8")).hexdigest(),
                                               "schema_version": meta["schema"],
                                               "task_filter": getattr(args, "task", None)},
                                  meta["revision"], expected=None)
    except RelayError as exc:
        raise page_error from exc
    state = (relay_v4.expand(stub, root, meta["project_id"])
             if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS else stub)
    current = hashlib.sha256(document.encode("utf-8")).hexdigest()
    args.task = getattr(args, "task", None)
    try:
        result = relay_v4.handoff(
            state, meta, meta["project_id"], current,
            getattr(args, "task", None),
            getattr(args, "limit", None) or 25,
            getattr(args, "offset", None) or 0,
            baseline=observed["baseline"],
            uncovered_offset=getattr(args, "uncovered_offset", None),
            uncovered_limit=getattr(args, "uncovered_limit", None),
            cursor=getattr(args, "cursor", None),
            tasks_offset=getattr(args, "tasks_offset", None),
            evidence_offset=getattr(args, "evidence_offset", None),
            blockers_offset=getattr(args, "blockers_offset", None),
            evidence_limit=getattr(args, "evidence_limit", None),
            blockers_limit=getattr(args, "blockers_limit", None),
            integrity=integrity_check(root, meta, stub,
                                      bool(getattr(args, "verify_integrity", False)),
                                      state),
            flags={"acknowledge_external":
                   bool(getattr(args, "acknowledge_external", False))},
            block=block)
    except RelayError as exc:
        if "cursor" in str(exc):
            raise page_error from exc
        raise
    # The baseline block is the CLI's observation of the current environment;
    # every other check is derived inside the view so that both entry points
    # (CLI and library) agree on one judgement.
    result["baseline"] = block
    result["recorded_baseline"] = block["recorded"]
    result["observed_baseline"] = block["observed"]
    scan(json.dumps(result, ensure_ascii=False), enforce_limit=False)
    return result


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


def _objects_info(root, meta, stub):
    """Object-store identity for status/capacity output (no writes)."""
    stats = obs.store_stats(root)
    evidence = stub.get("evidence") if isinstance(stub, dict) else None
    corrections = stub.get("corrections") if isinstance(stub, dict) else None
    return {"integrity": ("ok" if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS
                          else "not_configured"),
            "files": stats["files"], "bytes": stats["bytes"],
            "index": evidence.get("index") if isinstance(evidence, dict) else None,
            "corrections_index": corrections.get("index") if isinstance(corrections, dict) else None,
            "evidence_count": evidence.get("count") if isinstance(evidence, dict) else 0,
            "soft_quota_bytes": obs.OBJECT_SOFT_QUOTA_BYTES,
            "hard_quota_bytes": obs.OBJECT_HARD_QUOTA_BYTES,
            "disk_free_bytes": obs.disk_free(root),
            "reserve_bytes": obs.DISK_RESERVE_BYTES}


def _resolve_for_read(root, meta, stub, budget=None):
    """Resolve external objects; return (state, named_error).

    Resolving is itself a full read of every reachable object, so the caller
    receives the budget actually consumed and can report how many objects were
    checked instead of claiming an unknown.
    """
    if stub is None or meta["schema"] not in relay_v4.EXTERNAL_SCHEMAS:
        return stub, None
    budget = budget if budget is not None else obs.Budget()
    try:
        return relay_v4.expand(stub, root, meta["project_id"], budget), None
    except RelayError as exc:
        return stub, exc
    except (p.Invalid, fs.Error, OSError, UnicodeError, ValueError) as exc:
        return stub, RelayError(RELAY_OBJECT_SCHEMA_INVALID, "v4 state could not be resolved")


def status(args, root):
    path = fs.child(root, ".relay", "CURRENT.md")
    if not path.exists():
        return {"exists": False, "verification": "UNVERIFIED"}
    doc, lines, body, meta, stub, matches = read_envelope(root)
    # One read-only baseline collection per command.  The captured environment is
    # the only source of "now"; the recorded baseline is compared against it and is
    # never promoted to a current check.
    observed = capture_baseline(root)
    block = baseline_block(meta, observed)
    git = observed["baseline"] if observed["state"] == BASELINE_OK else observed
    size = len(doc.encode("utf-8"))
    limit, trigger, target = p.budget(meta["schema"])
    archive = _archive_info(root, stub, meta)
    objects = _objects_info(root, meta, stub)
    read_budget = obs.Budget()
    state, object_error = _resolve_for_read(root, meta, stub, read_budget)
    objects["objects_checked"] = (read_budget.files
                                  if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS else 0)
    resolved = state is not None and object_error is None
    if object_error is not None:
        objects["integrity"] = ("budget_exceeded"
                                if object_error.code == RELAY_VALIDATION_BUDGET_EXCEEDED
                                else "degraded")
    checks = {
        "baseline": {"checked": block["check"]["checked"], "state": block["check"]["state"],
                     "reason": block["check"]["reason"], "verified": block["check"]["verified"],
                     "applicable": True, "scope": block["check"]["scope"],
                     "does_not_cover": block["check"]["does_not_cover"]},
        "integrity": ({"checked": objects["integrity"] not in ("degraded", "budget_exceeded"),
                       "state": ("not_applicable" if meta["schema"] not in relay_v4.EXTERNAL_SCHEMAS
                                 else "verified" if objects["integrity"] == "ok" else
                                 "budget_exceeded" if objects["integrity"] == "budget_exceeded" else
                                 "degraded"),
                       "reason": None if objects["integrity"] in ("ok", "not_configured")
                                 else (object_error.code if object_error is not None
                                       else "RELAY_OBJECT_INTEGRITY_DEGRADED"),
                       "applicable": meta["schema"] in relay_v4.EXTERNAL_SCHEMAS,
                       "objects_checked": objects["objects_checked"],
                       "verified": (objects["integrity"] in ("ok", "not_configured")
                                     and archive["integrity"] != "invalid")}),
        "external": external_check(args),
    }
    result = {"exists": True, "schema": meta["schema"], "revision": int(meta["revision"]),
              "status": meta["status"], "writer": meta["writer"], "lease_until": meta["lease_until"],
              "lease_active": lease_active(meta), "git": git,
              "drift": block["check"]["state"] == CHECK_STALE,
              "verification": "CONFLICT" if not matches else
              "DEGRADED" if archive["integrity"] == "invalid" or objects["integrity"] == "degraded" else
              "UNVERIFIED" if (git["kind"] != "git" or objects["integrity"] == "budget_exceeded"
                               or object_error is not None) else "structurally_valid",
              "bytes": size, "near_limit": size >= trigger,
              "capacity": {"bytes": size, "max_bytes": p.MAX_BYTES,
                           "limit_bytes": limit, "ratio": round(size / p.MAX_BYTES, 4),
                           "limit_ratio": round(size / limit, 4),
                           "trigger_bytes": trigger, "target_bytes": target,
                           "observation_bytes": p.V4_OBSERVATION_BYTES,
                           "sections": _capacity_sections(doc, body)},
              "receipts": archive,
              "objects": objects,
              "objects_integrity": objects["integrity"],
              "archive_integrity": archive["integrity"],
              "current_receipts": archive["current"],
              "archived_receipts": archive["archived"],
              "document_valid": bool(matches),
              "migration_required": meta["schema"] not in p.SUPPORTED_SCHEMAS,
              "requires_drift_review": block["check"]["state"] in (CHECK_STALE, CHECK_UNCHECKED),
              "git_check_status": observed["state"],
              "drift_check_status": block["check"]["state"],
              "capacity_status": "over_limit" if size > limit else
                                 "near_limit" if size >= trigger else "available",
              # Write readiness requires a *verified current* environment: an
              # unavailable capture or a stale comparison is not readiness.
              "write_ready": resolved and matches and block["check"]["verified"] and
                             archive["integrity"] != "invalid" and
                             objects["integrity"] not in ("degraded", "budget_exceeded") and
                             size <= limit,
              "warnings": []}
    result["baseline"] = block
    result["recorded_baseline"] = block["recorded"]
    result["observed_baseline"] = block["observed"]
    result["checks"] = checks
    result["verification_record"] = verification_record(checks, ("baseline", "integrity"),
                                                        complete=bool(matches))
    result["verification"] = ("verified" if result["verification_record"]["verified"]
                              else "unverified")

    if object_error is not None:
        result["object_error"] = {"error": object_error.code, "message": object_error.detail}
    inbox = fs.child(root, ".relay", "inbox")
    result["pending_inbox"] = len(list(inbox.iterdir())) if inbox.exists() else 0
    if resolved:
        result.update(p.summary(state, git))
        preview_state, planned, would_compact = _plan_compaction(stub, meta["project_id"])
        schema = (meta["schema"] if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS
                  else p.SCHEMA_V3)
        if would_compact:
            preview = metadata(lines, {"schema": schema}) + p.render_body(preview_state, body)
            projected = len(preview.encode("utf-8"))
        else:
            projected = size
        result["receipts"].update({"reclaimable_bytes": max(0, size - projected),
                                   "projected_bytes": projected,
                                   "planned_segments": len(planned)})
        result["compact_recommended"] = bool(size >= trigger or planned)
        result["compact_fits"] = projected <= limit
        result["write_ready"] = result["write_ready"] and result["compact_fits"]
        if result["compact_recommended"]:
            result["warnings"].append("capacity governance recommended; v2 requires explicit migration, v3/v4 govern eligible writes")
        if not result["compact_fits"]:
            result["warnings"].append("receipt compaction cannot fit the current business content under the document limit")
        migration_info = state["extensions"].get("migration", {})
        pending = isinstance(migration_info, dict) and migration_info.get("mapping_review_required", False)
        result["mapping_review_required"] = bool(pending)
        if pending and matches:
            result["verification"] = "UNVERIFIED"
    elif stub is None:
        result["migration_required"] = True
    if args.command == "validate":
        p.require(matches, "derived view conflict")
        if archive["integrity"] == "invalid":
            p.require(False, "receipt archive integrity check failed")
        if object_error is not None:
            raise object_error
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
    patch = (input_patch(args)
             if args.command in ("update", "save")
             and getattr(args, "input", None) else {})
    # The custom Markdown of a v5 document is a managed, externalised collection:
    # it is applied here (the state has to be resolved first) and never reaches
    # the generic upsert, which only knows the v2/v3/v4 collections.
    markdown_patch = patch.get("markdown") if isinstance(patch, dict) else None
    if markdown_patch is not None:
        apply_patch = {key: value for key, value in patch.items() if key != "markdown"}
    else:
        apply_patch = patch
    p.require(args.expected_revision is not None and args.expected_revision >= 0, "expected revision required")
    p.identifier(args.writer)
    p.identifier(args.operation_id)
    p.require(0 < args.lease_minutes <= 1440, "lease must be 1 to 1440 minutes")
    hash_input = {"command": args.command, "writer": args.writer, "patch": patch,
                  "expected_revision": args.expected_revision, "lease_minutes": args.lease_minutes,
                  "allow_drift": bool(getattr(args, "allow_drift", False)),
                  "reason": getattr(args, "reason", None),
                  "snapshot": getattr(args, "snapshot", None), "source_sha256": getattr(args, "source_sha256", None)}
    # Preserve the v2 receipt hash for existing commands.  The opt-out is a
    # new input only when explicitly requested; compact has no v2 equivalent.
    if getattr(args, "no_auto_compact", False) or args.command == "compact":
        hash_input["no_auto_compact"] = bool(getattr(args, "no_auto_compact", False))
    if getattr(args, "to_v3", False):
        hash_input["to_v3"] = True
    if getattr(args, "to_v4", False):
        hash_input["to_v4"] = True
    if getattr(args, "to_v5", False):
        hash_input["to_v5"] = True
    operation_hash = p.digest(hash_input)
    # Never create relay or lock if CURRENT is absent.
    fs.child(root, ".relay", "CURRENT.md", exists=True)
    with (nullcontext() if dry_run else fs.locked(fs.child(root, ".relay", "CURRENT.md.lock"))):
        old, lines, body, meta, state, matches = read_envelope(root)
        full = state
        # The custom Markdown records travel with the transaction: for a v5
        # document they are read from the store, and a v4 to v5 migration
        # derives them from the document's own region before anything is
        # published.
        markdown_records = None
        stub_regenerated = False
        markdown_change = None
        if state is not None and meta["schema"] in relay_v4.EXTERNAL_SCHEMAS:
            full = relay_v4.expand(state, root, meta["project_id"])
            if meta["schema"] == p.SCHEMA_V5:
                markdown_records = relay_v4.collect_markdown(state, root, meta["project_id"])
                if markdown_patch is not None:
                    markdown_records, markdown_change = p.merge_markdown(
                        markdown_records, markdown_patch)
                # The stub is generated from the bound records.  A hand edit is
                # regenerated rather than honoured (the edited bytes remain in
                # the history snapshot of the revision being replaced) and the
                # write reports that it happened instead of hiding it.
                stub_regenerated = (p.custom_region(body)
                                    != p.render_markdown_stub(markdown_records))
        # A correction target whose bytes live on disk is only ever bound after
        # this root-bound resolver has verified it.  Without one, such a target
        # is refused by name rather than accepted on its syntax.
        target_resolver = relay_v4.make_target_resolver(root, meta["project_id"], state)
        archived = []
        if state:
            # A receipt still inline is sufficient to answer an idempotent
            # retry even if an unrelated old archive is degraded.
            for operation in state["operations"]:
                if operation["id"] == args.operation_id:
                    p.require(operation["hash"] == operation_hash, "operation ID reused with different input")
                    replayed_bytes = len(old.encode("utf-8"))
                    return {"revision": operation["revision"], "current_revision": int(meta["revision"]),
                            "replayed": True,
                            "capacity": _capacity_receipt(
                                meta["schema"], replayed_bytes, replayed_bytes,
                                committed=False, replayed=True,
                                next_cycle_reason="a replay commits no new state")}
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
                    replayed_bytes = len(old.encode("utf-8"))
                    return {"revision": operation["revision"], "current_revision": int(meta["revision"]),
                            "replayed": True, "archived": True,
                            "capacity": _capacity_receipt(
                                meta["schema"], replayed_bytes, replayed_bytes,
                                committed=False, replayed=True,
                                next_cycle_reason="a replay commits no new state")}
        p.require(int(meta["revision"]) == args.expected_revision, "revision conflict")
        p.require(matches or args.command == "recover", "derived view conflict; review direct edits before writing")
        git = git_state.capture(root)
        p.require(git["kind"] != "error", "Git inspection failed")
        old_git = baseline(meta)
        changed_branch = old_git.get("kind") == "git" and (git.get("kind") != "git" or old_git.get("branch") != git.get("branch"))
        if ((args.command == "resume" or (args.command == "migrate" and meta["schema"] in p.SUPPORTED_SCHEMAS)) and old_git != git) or changed_branch:
            p.require(getattr(args, "allow_drift", False),
                      "Git drift detected; review before --allow-drift")
        migrate_to_v3 = args.command == "migrate" and getattr(args, "to_v3", False)
        migrate_to_v4 = args.command == "migrate" and getattr(args, "to_v4", False)
        migrate_to_v5 = args.command == "migrate" and getattr(args, "to_v5", False)
        p.require(sum((bool(migrate_to_v3), bool(migrate_to_v4), bool(migrate_to_v5))) <= 1,
                  "choose one migration target")
        p.require(markdown_patch is None or meta["schema"] == p.SCHEMA_V5,
                  "markdown changes require a v5 document")
        if args.command == "migrate":
            p.require(not lease_active(meta), "active lease prevents migration")
            p.require(args.source_sha256 == hashlib.sha256(old.encode()).hexdigest(), "migration source hash changed or missing")
            if migrate_to_v5:
                p.require(meta["schema"] == p.SCHEMA_V4,
                          "v5 migration requires a v4 document; the v4 migration is already done")
                full = relay_v4.expand(state, root, meta["project_id"])
                markdown_records = p.split_markdown(p.custom_region(body))
            elif migrate_to_v4:
                p.require(meta["schema"] in (p.SCHEMA_V2, p.SCHEMA_V3),
                          "v4 migration requires a v2 or v3 document")
                full = p.validate(copy.deepcopy(state))
            else:
                state, body, migrated_to_v3 = migration(old, body, meta, to_v3=migrate_to_v3, state=state)
                migrate_to_v3 = migrate_to_v3 or migrated_to_v3
                full = state
        else:
            p.require(state is not None, "v1 is read-only; run migrate preview and explicit --apply")
        if args.command == "resume":
            p.require(not lease_active(meta) or meta["writer"] == args.writer, "lease conflict")
            if full["project"]["status"] == "paused":
                full["project"]["status"] = "active"
        elif args.command in ("update", "save", "compact"):
            p.require(meta["writer"] == args.writer and lease_active(meta), "missing, expired or conflicting lease; resume first")
            if args.command in ("update", "save"):
                full = p.apply(full, apply_patch, git, targets=target_resolver)
            if args.command == "save" and "status" not in patch.get("project", {}) and full["project"]["status"] == "active":
                full["project"]["status"] = "paused"
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
                if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS:
                    p.require(snap_meta["schema"] == meta["schema"],
                              "recovery requires a snapshot of the same external schema")
                    snap_state, snap_matches = parsed_body(snap_body)
                    full = relay_v4.expand(snap_state, root, snap_meta["project_id"])
                else:
                    full, snap_matches = parsed_body(snap_body)
                p.require(snap_matches, "snapshot view conflict")
                # Preserve current user extensions/metadata text, replace only managed progress.
                full["operations"] = receipts
                full["extensions"].update(current_extensions)
            full["extensions"].setdefault("recovery_log", []).append({"reason": args.reason, "from_revision": int(meta["revision"]), "snapshot": args.snapshot})
        revision = int(meta["revision"]) + 1
        full["operations"].append({"id": args.operation_id, "hash": operation_hash, "revision": revision})
        before_bytes = len(old.encode("utf-8"))
        auto_enabled = not getattr(args, "no_auto_compact", False)
        compacted = False
        segments = []
        if migrate_to_v5 or meta["schema"] == p.SCHEMA_V5:
            schema = p.SCHEMA_V5
        elif migrate_to_v4 or meta["schema"] == p.SCHEMA_V4:
            schema = p.SCHEMA_V4
        elif meta["schema"] == p.SCHEMA_V3 or migrate_to_v3:
            schema = p.SCHEMA_V3
        else:
            schema = p.SCHEMA
        limit, trigger, target = p.budget(schema)
        if args.command == "compact":
            p.require(schema in (p.SCHEMA_V3, p.SCHEMA_V4, p.SCHEMA_V5),
                      "v2 requires explicit migrate --to-v3 before compact")
        keep_lease = args.command in ("resume", "update", "compact")
        timestamp = now()
        if migrate_to_v5:
            # Additive provenance: the v3 to v4 record in extensions.migration is
            # never rewritten, and the new step is recorded separately.
            full["extensions"]["migration_v5"] = {
                "source_schema": meta["schema"],
                "source_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                "to_schema": p.SCHEMA_V5,
                "migrated_at": iso(timestamp),
                "record_mapping": {"markdown_sections": len(markdown_records or []),
                                   "markdown_bytes": sum(r["bytes"] for r in markdown_records or []),
                                   "tasks": len(full["tasks"]),
                                   "acceptance_map": len(p.build_ac_map(full["tasks"])["entries"])},
                "mapping_review_required": False}
        if migrate_to_v4:
            full.setdefault("corrections", [])
            full["extensions"]["migration"] = {
                "source_schema": meta["schema"],
                "source_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                "to_schema": p.SCHEMA_V4,
                "migrated_at": iso(timestamp),
                "record_mapping": {"evidence": len(full["evidence"]),
                                   "tasks": len(full["tasks"]),
                                   "blockers": len(full["blockers"]),
                                   "decisions": len(full["decisions"])},
                "mapping_review_required": True}
            full["extensions"]["ac_map"] = p.build_ac_map(full["tasks"])
        updates = {"schema": schema, "revision": str(revision), "updated_at": iso(timestamp),
                   "writer": args.writer if keep_lease else "null",
                   "lease_until": iso(timestamp + timedelta(minutes=args.lease_minutes)) if keep_lease else "null",
                   "status": full["project"]["status"], **git_fields(git)}
        if schema in relay_v4.EXTERNAL_SCHEMAS:
            document_state, plans, scan_texts = relay_v4.plan_full(
                full, root, meta["project_id"], target_resolver,
                markdown=markdown_records if schema == p.SCHEMA_V5 else None,
                source_revision=(int(meta["revision"]) if schema == p.SCHEMA_V5 else None))
        else:
            document_state, plans, scan_texts = full, [], []
        # Every new persistence path is secret scanned before anything is
        # published: object payloads, index nodes and manifests.
        for scan_text in scan_texts:
            scan(scan_text, enforce_limit=False)
        # A v5 candidate is rendered from the same records that were just planned
        # into objects, so the document can never disagree with the objects its
        # index reference names.
        document_state, candidate, segments, compacted, preview_size, final_bytes = _plan_candidate(
            lines, body, document_state, updates, meta["project_id"],
            force=(args.command == "compact" or migrate_to_v4 or migrate_to_v3
                   or migrate_to_v5),
            automatic=auto_enabled,
            markdown=markdown_records if schema == p.SCHEMA_V5 else None)
        if final_bytes > limit:
            if schema in relay_v4.EXTERNAL_SCHEMAS:
                raise RelayError(RELAY_CURRENT_CAPACITY_EXCEEDED,
                                 "candidate is " + str(final_bytes) + " bytes; the limit is "
                                 + str(limit))
            hint = "explicit migrate --to-v3 is required" if schema == p.SCHEMA_V2 else "business capacity requires review"
            p.require(False, f"document exceeds 64 KiB; candidate={preview_size}, after={final_bytes}, "
                      f"excess={final_bytes - p.MAX_BYTES}; {hint}; no content was truncated")
        new = checked(candidate)
        planned_object_bytes = obs.plan_bytes(plans)
        if dry_run:
            return {"dry_run": True, "estimate_only": False, "would_fit": True,
                    "source_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                    "revision": int(meta["revision"]), "candidate_revision": revision,
                    "candidate_bytes": final_bytes, "ungoverned_candidate_bytes": preview_size,
                    "archived_segments": len(segments), "schema": schema,
                    "objects": {"planned": len(plans), "unique_new_bytes": planned_object_bytes},
                    "recheck_on_apply": True}
        # Pre-write budget for the unique new objects, the history snapshot and
        # the candidate CURRENT.md, checked under the single-writer lock.  A
        # refusal here leaves the committed CURRENT.md untouched and publishes
        # no object at all.
        storage = obs.check_write_budget(root, planned_object_bytes + before_bytes + final_bytes)
        # New objects are published (immutable, fsynced, directory-fsynced)
        # before the history snapshot and the atomic CURRENT replacement.
        # Re-verify root-bound correction targets immediately before the commit
        # so that verification and commit cannot drift apart.
        for correction in (full.get("corrections", [])
                           if isinstance(full.get("corrections"), list) else []):
            if correction.get("target_type") in p.RESOLVED_CORRECTION_TARGETS:
                if target_resolver(correction) != correction["target_sha256"]:
                    raise RelayError(RELAY_CORRECTION_TARGET_UNREACHABLE,
                                     "correction target changed between verification and commit")
        published = obs.publish(root, plans) if plans else {"created": 0, "bytes_written": 0, "warnings": []}
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
        result["warnings"] = published["warnings"] + archive_warnings + result.get("warnings", [])
        if stub_regenerated:
            result["warnings"].append(
                "custom markdown stub was regenerated from the bound objects")
        if markdown_change is not None:
            result["markdown"] = {**markdown_change, "sections": len(markdown_records)}
        if final_bytes >= trigger:
            result["warnings"].append("capacity remains at or above the compaction trigger")
        if compacted and final_bytes > target:
            result["warnings"].append("compaction target was not reached; business content remains near the limit")
        result["compaction"] = {"applied": compacted, "before_bytes": before_bytes,
                                 "after_bytes": final_bytes, "ungoverned_candidate_bytes": preview_size,
                                 "archived_receipts": sum(len(payload.splitlines()) - 1 for _, payload in segments),
                                 "archived_segments": len(segments),
                                 "retained_receipts": len(document_state["operations"]),
                                 "target_bytes": target,
                                 "target_reached": final_bytes <= target,
                                 "near_limit": final_bytes >= trigger}
        result["objects"] = {"planned": len(plans), "created": published["created"],
                             "bytes_written": published["bytes_written"],
                             "unique_new_bytes": planned_object_bytes,
                             "object_files": storage["object_files"],
                             "object_bytes": storage["object_bytes"],
                             "soft_quota_bytes": storage["soft_quota_bytes"],
                             "hard_quota_bytes": storage["hard_quota_bytes"],
                             "disk_free_bytes": storage["disk_free_bytes"]}
        # A best-effort, additive capacity receipt.  It never turns a committed
        # write into a failure: if the next-cycle model cannot be built the
        # estimate is named as not computed.
        try:
            nlines, nbody, nmeta = split(new, enforce_limit=False)
            next_cycle = _next_save_cycle(nlines, nbody, document_state, nmeta, schema,
                                          revision)
        except (RelayError, p.Invalid, fs.Error, OSError, ValueError, KeyError, TypeError) as exc:
            next_cycle = {"status": "not_computed",
                          "reason": exc.code if isinstance(exc, RelayError) else "model unavailable"}
        result["capacity"] = _capacity_receipt(
            schema, before_bytes, final_bytes, committed=True,
            object_store_new_bytes=published["bytes_written"],
            archived_segments=len(segments), next_save_cycle=next_cycle)
        return {**result, "revision": revision, "schema": schema, "writer": updates["writer"],
                "lease_until": updates["lease_until"], "replayed": False}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest="command", required=True)
    for command in ("init", "status", "validate", "resume", "update", "save", "compact",
                    "migrate", "recover", "export", "verify", "capacity", "coverage", "handoff",
                    "markdown"):
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
            cmd.add_argument("--to-v4", action="store_true", help="explicitly externalize evidence into the v4 object store")
            cmd.add_argument("--to-v5", action="store_true",
                             help="externalize custom Markdown and the acceptance map into the v5 object store")
            cmd.add_argument("--source-sha256")
        if command == "capacity":
            cmd.add_argument("--writer")
            cmd.add_argument("--expected-revision", type=int)
            cmd.add_argument("--operation-id")
            cmd.add_argument("--lease-minutes", type=int, default=30)
            cmd.add_argument("--allow-drift", action="store_true")
            cmd.add_argument("--input", help="JSON change file, or - for standard input; read-only patch preview")
            cmd.add_argument("--long-record-threshold", type=int, default=None,
                             help="UTF-8 byte threshold for the non-blocking long-record hint")
        if command == "coverage":
            cmd.add_argument("--task")
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--acceptance-cursor", default=None,
                             help="acceptance cursor <task_id>:<offset> (independent of --limit)")
            cmd.add_argument("--verify-integrity", action="store_true",
                             help="also read and re-hash every reachable object (slower)")
            cmd.add_argument("--acknowledge-external", action="store_true",
                             help="declare that external references were checked outside the CLI")
        if command == "handoff":
            cmd.add_argument("--task")
            cmd.add_argument("--limit", type=int, default=25)
            cmd.add_argument("--offset", type=int, default=0)
            cmd.add_argument("--cursor", default=None,
                             help="acceptance cursor <task_id>:<offset>; not a task filter")
            cmd.add_argument("--uncovered-limit", type=int, default=None,
                             help="page size for the uncovered acceptance list (default: --limit)")
            cmd.add_argument("--uncovered-offset", type=int, default=None,
                             help="page offset for the uncovered acceptance list (default: --offset)")
            cmd.add_argument("--tasks-offset", type=int, default=None,
                             help="page offset for the task list (default: --offset)")
            cmd.add_argument("--evidence-offset", type=int, default=None,
                             help="page offset for the evidence list (default: --offset)")
            cmd.add_argument("--blockers-offset", type=int, default=None,
                             help="page offset for the blocker list (default: --offset)")
            cmd.add_argument("--evidence-limit", type=int, default=None,
                             help="page size for the evidence list (default: --limit)")
            cmd.add_argument("--blockers-limit", type=int, default=None,
                             help="page size for the blocker list (default: --limit)")
            cmd.add_argument("--verify-integrity", action="store_true",
                             help="also read and re-hash every reachable object (slower)")
            cmd.add_argument("--acknowledge-external", action="store_true",
                             help="declare that external references were checked outside the CLI")
        if command == "recover":
            cmd.add_argument("--reason", required=True)
            cmd.add_argument("--snapshot")
        if command == "export":
            cmd.add_argument("--output", required=True, help="new .zip destination; existing files are refused")
        if command == "verify":
            cmd.add_argument("--bundle", required=True, help="handoff .zip to validate")
        if command == "markdown":
            cmd.add_argument("--section", default=None,
                             help="print one external Markdown section by its stable id")
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
        elif args.command == "capacity":
            result = capacity_view(args, root)
        elif args.command == "coverage":
            result = coverage_view(args, root)
        elif args.command == "handoff":
            result = handoff_view(args, root)
        elif args.command == "markdown":
            result = markdown_view(args, root)
        elif args.command in ("compact", "migrate") and not args.apply and all(
                value is not None for value in (args.writer, args.expected_revision, args.operation_id)):
            result = mutate(args, root, dry_run=True)
        elif args.command == "compact" and not args.apply:
            old, lines, body, meta, stub, matches = read_envelope(root)
            p.require(stub is not None, "v1 is read-only; run migrate preview and explicit --apply")
            p.require(matches, "derived view conflict; review direct edits before compacting")
            archive = _archive_info(root, stub, meta)
            p.require(archive["integrity"] != "invalid", "receipt archive integrity check failed")
            schema = (meta["schema"] if meta["schema"] in relay_v4.EXTERNAL_SCHEMAS
                      else p.SCHEMA_V3)
            limit, _trigger, target = p.budget(schema)
            candidate_state, candidate, segments, would_compact, _, projected = _plan_candidate(
                lines, body, stub, {"schema": schema}, meta["project_id"], force=True)
            result = {"dry_run": True, "schema": meta["schema"], "target_schema": schema,
                      "revision": int(meta["revision"]), "before_bytes": len(old.encode()),
                      "candidate_bytes": projected, "would_compact": would_compact,
                      "archived_segments": len(segments), "retained_receipts": len(candidate_state["operations"]),
                      "target_bytes": target,
                      "target_reached": projected <= target,
                      "estimate_only": True, "excludes_new_operation_metadata": True,
                      "would_fit": projected <= limit, "archive_integrity": archive["integrity"]}
        elif args.command == "migrate" and not args.apply:
            old, lines, body, meta, state, _ = read_envelope(root)
            if getattr(args, "to_v5", False):
                if meta["schema"] == p.SCHEMA_V5:
                    result = {"migration_required": False, "schema": p.SCHEMA_V5,
                              "revision": int(meta["revision"])}
                else:
                    p.require(meta["schema"] == p.SCHEMA_V4,
                              "v5 migration requires a v4 document; run --to-v4 first")
                    source = relay_v4.expand(state, root, meta["project_id"])
                    records = p.split_markdown(p.custom_region(body))
                    resolver = relay_v4.make_target_resolver(root, meta["project_id"], state)
                    stub, plans, _texts = relay_v4.plan_full(
                        source, root, meta["project_id"], resolver,
                        markdown=records, source_revision=int(meta["revision"]))
                    _st, candidate, planned_segments, _wc, _ung, projected = _plan_candidate(
                        lines, body, stub, {"schema": p.SCHEMA_V5}, meta["project_id"],
                        force=True, markdown=records)
                    checked(candidate)
                    result = {"dry_run": True,
                              "source_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                              "from_schema": meta["schema"], "to_schema": p.SCHEMA_V5,
                              "revision": int(meta["revision"]),
                              "candidate_bytes": projected,
                              "planned_segments": len(planned_segments),
                              "markdown_sections": len(records),
                              "markdown_bytes": sum(r["bytes"] for r in records),
                              "objects": {"planned": len(plans),
                                          "unique_new_bytes": obs.plan_bytes(plans)},
                              "mapping_review_required": False}
            elif getattr(args, "to_v4", False):
                if meta["schema"] == p.SCHEMA_V4:
                    result = {"migration_required": False, "schema": p.SCHEMA_V4,
                              "revision": int(meta["revision"])}
                else:
                    p.require(meta["schema"] in (p.SCHEMA_V2, p.SCHEMA_V3),
                              "v4 migration requires a v2 or v3 document")
                    source = p.validate(copy.deepcopy(state))
                    stub, plans, _texts = relay_v4.plan_full(source, root, meta["project_id"])
                    _st, candidate, planned_segments, _wc, _ung, projected = _plan_candidate(
                        lines, body, stub, {"schema": p.SCHEMA_V4}, meta["project_id"],
                        force=True)
                    checked(candidate)
                    result = {"dry_run": True,
                              "source_sha256": hashlib.sha256(old.encode("utf-8")).hexdigest(),
                              "from_schema": meta["schema"], "to_schema": p.SCHEMA_V4,
                              "revision": int(meta["revision"]),
                              "candidate_bytes": projected,
                              "planned_segments": len(planned_segments),
                              "objects": {"planned": len(plans),
                                          "unique_new_bytes": obs.plan_bytes(plans)},
                              "mapping_review_required": True}
            elif getattr(args, "to_v3", False):
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
    except RelayError as exc:
        print(json.dumps(exc.as_result(), ensure_ascii=False), file=sys.stderr)
        return 2
    except (fs.Error, p.Invalid) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, RecursionError):
        # Do not leak arbitrary filenames, subprocess output, or input fragments.
        print('{"error":"invalid input or unavailable local resource"}', file=sys.stderr)
        return 2
