"""Structured progress document and typed ID-based changes (standard library only)."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime

import objectstore as obs
from relay_errors import (
    RelayError,
    RELAY_CORRECTION_TARGET_CONFLICT,
    RELAY_CORRECTION_TARGET_UNSUPPORTED,
    RELAY_CORRECTION_TARGET_UNREACHABLE,
    RELAY_VALIDATION_BUDGET_EXCEEDED,
)

# v2 remains the default wire format so existing installations keep working.
# v3 is selected explicitly (or on the first automatic receipt compaction) and
# is rejected by old clients rather than silently dropping archived receipts.
SCHEMA = "project-continuity/v2"
SCHEMA_V2 = SCHEMA
SCHEMA_V3 = "project-continuity/v3"
SCHEMA_V4 = "project-continuity/v4"
# v5 keeps the v4 object model and externalises two more collections: the
# project's custom Markdown sections and the acceptance map.  Resolving a v5
# document produces exactly the logical state its v4 predecessor produced; only
# the on-disk representation changes, and old clients refuse v5 by name.
SCHEMA_V5 = "project-continuity/v5"
SUPPORTED_SCHEMAS = (SCHEMA_V2, SCHEMA_V3, SCHEMA_V4, SCHEMA_V5)
RECEIPT_SCHEMA = "project-continuity/receipts/v1"
AC_MAP_SCHEMA = "project-continuity/ac-map/v1"
MARKDOWN_SCHEMA = "project-continuity/markdown/v1"
EXTERNAL_MARKDOWN_SCHEMA = "project-continuity/external-markdown/v1"
MARKDOWN_ID_PREFIX = "md-"
BLOCKER_SCOPE_SCHEMA = "project-continuity/blocker-scope/v1"
HANDOFF_VIEW_SCHEMA = "project-continuity/handoff-view/v1"
MAX_BYTES = 65536
COMPACTION_TRIGGER_BYTES = (MAX_BYTES * 80 + 99) // 100
COMPACTION_TARGET_BYTES = int(MAX_BYTES * 0.7)
# v4 keeps the 64 KiB protocol document ceiling but commits CURRENT.md at or
# below half of it, so the object store never grows the authoritative file.
V4_MAX_BYTES = 32768
V4_OBSERVATION_BYTES = 24576
V4_COMPACTION_TRIGGER_BYTES = (V4_MAX_BYTES * 80 + 99) // 100
V4_COMPACTION_TARGET_BYTES = int(V4_MAX_BYTES * 0.7)
CORRECTION_KINDS = ("correction", "revocation", "supersession")
# Target types this build can bind to a real record or a real object.
CORRECTION_TARGETS = ("evidence", "decision", "correction", "chunk_manifest")
# Targets whose bytes live outside the document: a digest is only ever stored
# after an explicit resolver has verified the object.
RESOLVED_CORRECTION_TARGETS = ("chunk_manifest",)
# Manifest names that have no owned namespace here.  They are refused by name
# instead of being accepted because the digest merely looks like a digest.
UNSUPPORTED_CORRECTION_TARGETS = ("manifest", "handoff_manifest",
                                  "external_evidence_manifest", "export_manifest")
EVIDENCE_VIEW_SCHEMA = "project-continuity/evidence-view/v2"
RELATION_SCHEMA = "project-continuity/relation-index/v1"

# Four separate judgements; none of them implies another.
BASELINE_RECORDED = "recorded"
BASELINE_STALE = "stale"
BASELINE_UNAVAILABLE = "unavailable"
BASELINE_UNCHECKED = "unchecked"
CURRENT_EFFECTIVE = "effective"
CURRENT_REVOKED = "revoked"
CURRENT_SUPERSEDED = "superseded"
KIND_STATE = {"revocation": CURRENT_REVOKED, "supersession": CURRENT_SUPERSEDED,
              "correction": None}


RECEIPT_KEEP = 32
RECEIPT_SEGMENT_MAX = 128
DATA_START = "<!-- project-continuity:data -->\n```json\n"
DATA_END = "\n```\n<!-- project-continuity:/data -->"
VIEW_START = "<!-- project-continuity:view -->\n"
VIEW_END = "\n<!-- project-continuity:/view -->"
COLLECTIONS = ("tasks", "blockers", "evidence", "decisions")


class Invalid(ValueError):
    """Input violates the progress contract; messages contain no input values."""


def require(condition, message):
    if not condition:
        raise Invalid(message)


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(Invalid("non-finite JSON number")))
    except (ValueError, RecursionError) as exc:
        raise Invalid("invalid JSON input") from exc


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def text(value, label, empty=False):
    require(isinstance(value, str) and (empty or bool(value.strip())), label + " must be text")
    require(len(value) <= 4096 and not any(ord(c) < 32 and c not in "\n\t" for c in value)
            and "<!-- project-continuity:" not in value and "\x7f" not in value,
            label + " contains unsupported content")


def identifier(value):
    require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value),
            "invalid identifier")


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "timestamp needs timezone")
    except (AttributeError, ValueError) as exc:
        raise Invalid("invalid timestamp") from exc


def empty_state(name="Project"):
    return {"project": {"name": name, "goal": "", "status": "active", "current_task": None,
                        "next_step": "", "outcomes": {}},
            **{key: [] for key in COLLECTIONS}, "extensions": {}, "operations": []}


def keyed(rows, label):
    require(isinstance(rows, list), label + " must be a list")
    out = {}
    for row in rows:
        require(isinstance(row, dict), label + " entry must be an object")
        identifier(row.get("id"))
        require(row["id"] not in out, "duplicate identifier")
        out[row["id"]] = row
    return out


def budget(schema):
    """Return (document limit, compaction trigger, compaction target)."""
    if schema in (SCHEMA_V4, SCHEMA_V5):
        return V4_MAX_BYTES, V4_COMPACTION_TRIGGER_BYTES, V4_COMPACTION_TARGET_BYTES
    return MAX_BYTES, COMPACTION_TRIGGER_BYTES, COMPACTION_TARGET_BYTES


def is_external_schema(schema):
    """Schemas whose committed logical state lives in the object store."""
    return schema in (SCHEMA_V4, SCHEMA_V5)


def is_external(state):
    return isinstance(state.get("evidence"), dict)


def index_ref(index, count, logical_sha256):
    return {"schema": obs.INDEX_SCHEMA, "index": index, "count": count,
            "sha256": logical_sha256}


def _require_index_ref(ref, label):
    require(isinstance(ref, dict) and set(ref) == {"schema", "index", "count", "sha256"},
            "invalid " + label + " index reference")
    require(ref["schema"] == obs.INDEX_SCHEMA, "invalid " + label + " index reference")
    require(ref["index"] is None or obs.is_hex64(ref["index"]),
            "invalid " + label + " index reference")
    require(type(ref["count"]) is int and ref["count"] >= 0,
            "invalid " + label + " index reference")
    require(obs.is_hex64(ref["sha256"]), "invalid " + label + " index reference")
    if ref["count"] == 0:
        require(ref["index"] is None, "empty " + label + " collection carries an index")
    else:
        require(ref["index"] is not None, label + " index reference is missing")


def ac_identifier(task_id, condition):
    raw = (task_id + "\u0000" + condition).encode("utf-8")
    return "ac-" + hashlib.sha256(raw).hexdigest()[:16]


def build_ac_map(tasks):
    """Deterministic, persistent acceptance-condition identifiers."""
    entries = []
    for task in tasks:
        for position, condition in enumerate(task.get("acceptance", [])):
            entries.append({"task_id": task["id"],
                            "ac_id": ac_identifier(task["id"], condition),
                            "sha256": hashlib.sha256(condition.encode("utf-8")).hexdigest(),
                            "index": position})
    return {"schema": AC_MAP_SCHEMA, "count": len(entries), "entries": entries}


def _validate_ac_map(value):
    if value is None:
        return
    require(isinstance(value, dict) and set(value) == {"schema", "count", "entries"},
            "invalid acceptance map")
    require(value["schema"] == AC_MAP_SCHEMA, "invalid acceptance map")
    entries = value["entries"]
    require(isinstance(entries, list) and type(value["count"]) is int
            and value["count"] == len(entries), "invalid acceptance map")
    seen = set()
    for entry in entries:
        require(isinstance(entry, dict)
                and set(entry) == {"task_id", "ac_id", "sha256", "index"},
                "invalid acceptance map entry")
        identifier(entry["task_id"])
        identifier(entry["ac_id"])
        require(obs.is_hex64(entry["sha256"]), "invalid acceptance map entry")
        require(type(entry["index"]) is int and entry["index"] >= 0,
                "invalid acceptance map entry")
        require(entry["ac_id"] not in seen, "duplicate acceptance identifier")
        seen.add(entry["ac_id"])


MARKDOWN_STUB_HEADING = "## 交接摘要（自定义 Markdown 已外置 · schema v5）"
MARKDOWN_PREAMBLE_TITLE = "(preamble)"
MARKDOWN_MAX_BYTES = 262144


def markdown_identifier(title):
    """A stable ASCII id for one custom Markdown section."""
    return MARKDOWN_ID_PREFIX + hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]


def markdown_title(value):
    require(isinstance(value, str) and value.strip(), "markdown title must be text")
    require(len(value) <= 256 and "\n" not in value and "\r" not in value
            and "<!-- project-continuity:" not in value and "\x7f" not in value,
            "markdown title contains unsupported content")
    require(not value.startswith(MARKDOWN_ID_PREFIX) or value == MARKDOWN_PREAMBLE_TITLE,
            "markdown title collides with a managed identifier")


def markdown_content(value, allow_blank=False):
    require(isinstance(value, str), "markdown content must be text")
    if not allow_blank:
        require(bool(value.strip()), "markdown content must be text")
    require(len(value.encode("utf-8")) <= MARKDOWN_MAX_BYTES,
            "markdown section is too large")
    require("\x7f" not in value and "<!-- project-continuity:" not in value,
            "markdown content contains unsupported content")


def validate_markdown(records):
    """Ordered, content-addressed custom Markdown sections.

    The text is reconstructed verbatim by joining the records in ordinal order,
    so every record carries the digest and byte length of its own content and
    the sequence is complete and gapless.
    """
    require(isinstance(records, list), "markdown must be a list")
    seen, titles = set(), set()
    for position, record in enumerate(records):
        require(isinstance(record, dict) and set(record) == {
            "id", "ordinal", "title", "content", "sha256", "bytes"},
            "invalid markdown record")
        require(type(record["ordinal"]) is int and record["ordinal"] == position,
                "markdown records are not in order")
        markdown_title(record["title"])
        identifier(record["id"])
        require(record["id"] == markdown_identifier(record["title"]),
                "markdown identifier does not match its title")
        markdown_content(record["content"],
                         allow_blank=record["title"] == MARKDOWN_PREAMBLE_TITLE)
        raw = record["content"].encode("utf-8")
        require(record["sha256"] == hashlib.sha256(raw).hexdigest(),
                "markdown content hash mismatch")
        require(type(record["bytes"]) is int and record["bytes"] == len(raw),
                "markdown byte length mismatch")
        require(record["id"] not in seen, "duplicate markdown identifier")
        require(record["title"] not in titles, "duplicate markdown section title")
        seen.add(record["id"])
        titles.add(record["title"])
    return records


def custom_region(body):
    """The unmanaged Markdown region of a body: everything before the data block."""
    position = body.find(DATA_START)
    require(position >= 0, "missing managed data section")
    return body[:position]


def render_region(body, region):
    """Replace the unmanaged region, leaving both managed blocks byte-identical."""
    position = body.find(DATA_START)
    require(position >= 0, "missing managed data section")
    return region + body[position:]


def split_markdown(region):
    """Split a custom Markdown region into reversible, ordered sections.

    Nothing that was present may be dropped: joining the returned records in
    order reproduces the input byte for byte, including the leading separator.
    """
    markdown_content(region, allow_blank=True)
    parts = [part for part in re.split(r"(?m)^(?=## )", region) if part != ""]
    records = []
    for position, part in enumerate(parts):
        heading = part.split("\n", 1)[0]
        if heading.startswith("## "):
            title = heading[3:].strip()
            markdown_title(title)
            require(part == "## " + title or part.startswith("## " + title + "\n"),
                    "unsupported Markdown section heading")
        else:
            require(not part.strip(), "custom Markdown must start with a level-2 heading")
            title = MARKDOWN_PREAMBLE_TITLE
        markdown_content(part, allow_blank=title == MARKDOWN_PREAMBLE_TITLE)
        raw = part.encode("utf-8")
        records.append({"id": markdown_identifier(title), "ordinal": position,
                        "title": title, "content": part,
                        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)})
    validate_markdown(records)
    require(join_markdown(records) == region, "markdown split is not reversible")
    return records


def join_markdown(records):
    validate_markdown(records)
    return "".join(record["content"] for record in records)


def render_markdown_stub(records):
    """The compact, human-readable replacement for an externalised region.

    Every section heading stays in the document.  A reader that looks for a
    known marker (the canonical status file requires "## 前置任务状态（live）"
    to be present) keeps working, and a human can see which sections exist,
    how large they are, where their bytes went and how to restore them.
    """
    validate_markdown(records)
    total = sum(record["bytes"] for record in records)
    digest = hashlib.sha256(join_markdown(records).encode("utf-8")).hexdigest()
    lines = [MARKDOWN_STUB_HEADING,
             "- " + str(len(records)) + " 节 / " + str(total) + " 字节已外置为不可变对象；原文未删除、未改写。",
             "- 全文 sha256 " + digest,
             "- 逐字节恢复：python scripts/write_current.py markdown --root <project>",
             "- 引用：.relay/objects/markdown/<aa>/<sha256>.json（内容寻址，摘要写入托管 JSON 块）"]
    for record in records:
        if record["title"] == MARKDOWN_PREAMBLE_TITLE:
            continue
        lines.append("")
        lines.append("## " + record["title"])
        lines.append("- 已外置 · " + record["id"] + " · " + str(record["bytes"])
                     + " B · sha256 " + record["sha256"][:16] + "…")
    return "\n".join(lines) + "\n\n"


def markdown_text_digest(records):
    validate_markdown(records)
    return hashlib.sha256(join_markdown(records).encode("utf-8")).hexdigest()


def merge_markdown(records, additions):
    """Apply typed Markdown upserts by section title.

    A v5 document's custom Markdown is externalised, so a writer adds or
    replaces a section through a typed change rather than by editing the
    generated stub.  Existing sections keep their relative order, a new title
    is appended, and every identity (id, ordinal, content digest, byte length)
    is recomputed here instead of being transcribed by the caller.
    """
    require(isinstance(additions, list), "markdown changes must be a list")
    working = [{"title": record["title"], "content": record["content"]}
               for record in records]
    positions = {record["title"]: index for index, record in enumerate(working)}
    replaced = 0
    added = 0
    for addition in additions:
        require(isinstance(addition, dict)
                and set(addition) == {"title", "content"},
                "invalid markdown change")
        title, content = addition["title"], addition["content"]
        markdown_title(title)
        markdown_content(content, allow_blank=title == MARKDOWN_PREAMBLE_TITLE)
        if title != MARKDOWN_PREAMBLE_TITLE:
            require(content == "## " + title or content.startswith("## " + title + "\n"),
                    "markdown content must start with its own heading")
        if title in positions:
            working[positions[title]]["content"] = content
            replaced += 1
        else:
            positions[title] = len(working)
            working.append({"title": title, "content": content})
            added += 1
    merged = []
    for position, record in enumerate(working):
        raw = record["content"].encode("utf-8")
        merged.append({"id": markdown_identifier(record["title"]),
                       "ordinal": position, "title": record["title"],
                       "content": record["content"],
                       "sha256": hashlib.sha256(raw).hexdigest(),
                       "bytes": len(raw)})
    validate_markdown(merged)
    return merged, {"applied": replaced + added, "replaced": replaced, "added": added}


def external_markdown_metadata(source_revision, records):
    """Provenance for the externalisation: where the text came from."""
    return {"schema": EXTERNAL_MARKDOWN_SCHEMA, "source_revision": int(source_revision),
            "sections": len(records), "bytes": sum(r["bytes"] for r in records),
            "text_sha256": markdown_text_digest(records)}


def _validate_external_markdown(value):
    if value is None:
        return
    require(isinstance(value, dict) and set(value) == {
        "schema", "source_revision", "sections", "bytes",
        "text_sha256"}, "invalid external markdown metadata")
    require(value["schema"] == EXTERNAL_MARKDOWN_SCHEMA, "invalid external markdown metadata")
    require(type(value["source_revision"]) is int and value["source_revision"] >= 0,
            "invalid external markdown revision")
    require(type(value["sections"]) is int and value["sections"] >= 0
            and type(value["bytes"]) is int and value["bytes"] >= 0,
            "invalid external markdown size")
    require(obs.is_hex64(value["text_sha256"]),
            "invalid external markdown text digest")


def _blocker_scopes(value):
    if value is None:
        return {}
    require(isinstance(value, dict) and set(value) == {"schema", "entries"},
            "invalid blocker scope extension")
    require(value["schema"] == BLOCKER_SCOPE_SCHEMA, "invalid blocker scope extension")
    require(isinstance(value["entries"], list), "invalid blocker scope extension")
    scopes = {}
    for entry in value["entries"]:
        require(isinstance(entry, dict) and set(entry) == {"blocker_id", "scope"},
                "invalid blocker scope entry")
        identifier(entry["blocker_id"])
        require(entry["blocker_id"] not in scopes, "duplicate blocker scope entry")
        scope = entry["scope"]
        require(isinstance(scope, list) and scope
                and all(isinstance(item, str) for item in scope), "invalid blocker scope")
        require(len(scope) == len(set(scope)), "duplicate blocker scope")
        scopes[entry["blocker_id"]] = list(scope)
    return scopes


def _scope_blocks(scope, declared_task, task_id):
    if scope is None:
        return declared_task == task_id
    return "project" in scope or ("task:" + task_id) in scope


def _effective_evidence(evidence, revoked, superseded):
    """Filter one record type by typed relations; never by a bare id."""
    return {key: value for key, value in evidence.items()
            if ("evidence", key) not in revoked and ("evidence", key) not in superseded}


def correction_target_digest(record, state, targets=None):
    """Bind a correction to the exact canonical bytes of its target.

    In-document targets are hashed here.  A target whose bytes live outside the
    document cannot be resolved from the document alone, so it is refused unless
    the caller passes an explicit resolver; the resolver's verified digest is
    the only value ever stored.  Ambiguous manifest names are refused by name:
    a syntactically valid digest is never accepted as a verified target.
    """
    evidence = keyed(state["evidence"], "evidence") if isinstance(state.get("evidence"), list) else {}
    decisions = keyed(state.get("decisions", []), "decisions")
    corrections = keyed(state.get("corrections", []), "corrections")
    target_type = record.get("target_type")
    target_id = record.get("target_id")
    if target_type in UNSUPPORTED_CORRECTION_TARGETS:
        raise RelayError(RELAY_CORRECTION_TARGET_UNSUPPORTED,
                         "correction target type is ambiguous and has no owned namespace")
    require(target_type in CORRECTION_TARGETS, "invalid correction target type")
    if target_type in RESOLVED_CORRECTION_TARGETS:
        require(obs.is_hex64(target_id), "invalid correction target id")
        if not callable(targets):
            raise RelayError(RELAY_CORRECTION_TARGET_UNSUPPORTED,
                             "this correction target type requires an explicit resolver")
        resolved = targets(record)
        require(obs.is_hex64(resolved), "correction target resolver returned no digest")
        return resolved
    identifier(target_id)
    require(not (target_type == "correction" and target_id == record.get("id")),
            "a correction cannot target itself")
    table = {"evidence": evidence, "decision": decisions, "correction": corrections}[target_type]
    require(target_id in table, "correction target missing")
    return digest(table[target_id])


def _validate_corrections(corrections, evidence, decisions, targets=None):
    """Return (revoked, superseded) keyed by (record_type, record_id).

    A relationship is never keyed by a bare id: revoking a decision must not
    touch an evidence record that happens to share the same string.
    """
    _edges, revoked, superseded = _correction_edges(corrections, evidence, decisions,
                                                   targets, verify_digests=True)
    return revoked, superseded


def relation_index(state, targets=None):
    """Public typed relation view: every edge, every reason, no ordering effect."""
    evidence = state.get("evidence")
    evidence = keyed(evidence, "evidence") if isinstance(evidence, list) else {}
    decisions = keyed(state.get("decisions", []), "decisions")
    corrections = state.get("corrections")
    corrections = keyed(corrections, "corrections") if isinstance(corrections, list) else {}
    edges, revoked, superseded = _correction_edges(corrections, evidence, decisions, targets)
    return {"schema": RELATION_SCHEMA, "edges": edges, "revoked": revoked,
            "superseded": superseded, "count": len(edges),
            "revoked_count": len(revoked), "superseded_count": len(superseded)}


def reasons_by_target(edges):
    """Every named reason per target: a hidden single reason is never reported."""
    out = {}
    for edge in edges:
        out.setdefault(edge["target"], []).append(edge["kind"])
    return {key: sorted(set(value)) for key, value in out.items()}

def _correction_edges(corrections, evidence, decisions=None, targets=None,
                     verify_digests=True):
    """Validate every relationship edge; none may hide behind another.

    Returns the complete typed edge list.  Each edge is validated on its own, so
    a cycle cannot be hidden behind a per-target dictionary, and two different
    replacements for one target are refused as a conflict instead of being
    silently ordered by traversal.
    """
    evidence = evidence or {}
    corrections = corrections or {}
    edges, revoked, superseded = [], set(), {}
    for item in corrections.values():
        require(isinstance(item, dict) and set(item) == {
                "id", "kind", "target_type", "target_id", "replacement_id",
                "reason", "at", "target_sha256"}, "invalid correction fields")
        require(item["kind"] in CORRECTION_KINDS, "invalid correction kind")
        if item["target_type"] in UNSUPPORTED_CORRECTION_TARGETS:
            raise RelayError(RELAY_CORRECTION_TARGET_UNSUPPORTED,
                             "correction target type is ambiguous and has no owned namespace")
        require(item["target_type"] in CORRECTION_TARGETS, "invalid correction target type")
        if item["target_type"] in RESOLVED_CORRECTION_TARGETS:
            require(obs.is_hex64(item["target_id"]), "invalid correction target id")
        else:
            identifier(item["target_id"])
        require(obs.is_hex64(item["target_sha256"]), "invalid correction target digest")
        if item["kind"] == "supersession":
            identifier(item["replacement_id"])
        else:
            require(item["replacement_id"] is None, "only supersession carries a replacement")
        if item["target_type"] == "correction":
            # A relationship may annotate another relationship; it may not use a
            # relationship to revoke or replace one (immutability of history).
            require(item["kind"] == "correction",
                    "only an annotating correction may target a correction")
        text(item["reason"], "correction reason")
        timestamp(item["at"])
        table = {"evidence": list(evidence.values()),
                 "decisions": list((decisions or {}).values()),
                 "corrections": list(corrections.values())}
        # A writer binds a digest to the exact bytes of its target; a
        # reader cannot recompute that from an index reference, and must not
        # pretend it did.  The graph itself (conflicts, cycles, targets, types)
        # is still validated by every reader.
        if verify_digests:
            expected = correction_target_digest(
                item, {"evidence": table["evidence"],
                       "decisions": table["decisions"],
                       "corrections": table["corrections"]}, targets=targets)
            require(expected == item["target_sha256"],
                    "correction target digest mismatch")
        key = (item["target_type"], item["target_id"])
        if item["kind"] == "revocation":
            revoked.add(key)
        elif item["kind"] == "supersession":
            require(item["target_type"] == "evidence" and item["replacement_id"] in evidence,
                    "supersession replacement missing")
            replacement = ("evidence", item["replacement_id"])
            previous = superseded.get(key)
            if previous is not None and previous != replacement:
                raise RelayError(RELAY_CORRECTION_TARGET_CONFLICT,
                                 "two corrections replace one target with different records")
            superseded[key] = replacement
        edges.append({"id": item["id"], "kind": item["kind"], "target": key,
                      "replacement": (("evidence", item["replacement_id"])
                                      if item["kind"] == "supersession" else None),
                      "reason": item["reason"], "at": item["at"]})
    _require_acyclic(superseded)
    return edges, revoked, superseded


def _require_acyclic(superseded):
    """Reject any cycle in the complete supersession edge set.

    Traversal starts from every edge instead of from a per-target dictionary, so
    e1->e2, e2->e1 and e1->e3 can no longer hide a cycle behind the last edge.
    """
    edges = {}
    for target, replacement in superseded.items():
        edges.setdefault(target, set()).add(replacement)
    state = {}

    def visit(node):
        status = state.get(node)
        if status == "done":
            return
        require(status != "active", "correction supersession cycle")
        state[node] = "active"
        for neighbour in sorted(edges.get(node, ())):
            visit(neighbour)
        state[node] = "done"

    for node in sorted(edges):
        visit(node)

def _baseline_status(record, baseline):
    """L4: has this record's runtime baseline been checked, and did it match?"""
    if baseline is None:
        return BASELINE_UNCHECKED, "baseline_not_checked"
    stored = record.get("baseline")
    if not isinstance(stored, dict) or not stored:
        return BASELINE_UNAVAILABLE, "baseline_unavailable"
    if not isinstance(baseline, dict) or not baseline:
        return BASELINE_UNAVAILABLE, "current_baseline_unavailable"
    if stored != baseline:
        return BASELINE_STALE, "baseline_mismatch"
    if baseline.get("kind") != "git":
        return BASELINE_UNAVAILABLE, "current_baseline_not_git"
    return BASELINE_RECORDED, None


def _evidence_item(record, task, current, superseded_by, baseline, reasons=()):
    generation_match = record["generation"] == task["generation"]
    accepted = [item for item in record["acceptance"] if item in task["acceptance"]]
    status, baseline_reason = _baseline_status(record, baseline)
    if current != CURRENT_EFFECTIVE:
        contributes, why = False, current
    elif not generation_match:
        contributes, why = False, "generation_mismatch"
    elif record["result"] != "pass":
        contributes, why = False, "result_" + record["result"]
    elif not accepted:
        contributes, why = False, "acceptance_not_matched"
    else:
        contributes, why = True, None
    order = {CURRENT_REVOKED: 0, CURRENT_SUPERSEDED: 1}
    named = sorted({reason for reason in reasons if reason},
                   key=lambda value: order.get(value, 9))
    return {"id": record["id"], "task_id": record["task_id"], "check": record["check"],
            "ref": record["ref"], "at": record["at"],
            "recorded_result": record["result"], "acceptance": list(record["acceptance"]),
            "acceptance_match": accepted,
            "generation": record["generation"], "task_generation": task["generation"],
            "generation_match": generation_match,
            "current": current, "superseded_by": superseded_by,
            "current_reasons": named,
            "baseline": status, "baseline_reason": baseline_reason,
            "contributes": contributes, "reason": why}


def _relation_edges(state):
    """Typed relation edges exactly as written, with no digest recomputation.

    Digest verification is the job of validate/apply/resolve, which are the
    only places that may need a root-bound resolver.  A reader therefore never
    re-resolves an external target and never needs one: any state that reaches
    a reader has already passed validation and is immutable afterwards.

    The revoked set, the supersession map and the reason lists are all derived
    from the one validated edge list, so a reader and a validator can never
    disagree about which edges exist.
    """
    corrections = state.get("corrections")
    corrections = keyed(corrections, "corrections") if isinstance(corrections, list) else {}
    if corrections:
        # The reader validates the same graph from its own view: real
        # record ids for the target/replacement checks, while the digest
        # check stays a writer duty (it needs the write-time resolver).
        context = {}
        for record in state.get("evidence") or []:
            if isinstance(record, dict) and isinstance(record.get("id"), str):
                context[record["id"]] = record
        edges, revoked, superseded = _correction_edges(corrections, context, None,
                                                   None, verify_digests=False)
    else:
        edges, revoked, superseded = [], set(), {}
    return revoked, superseded, edges


def evidence_view(state, baseline=None, targets=None):
    """The single shared L1..L4 judgement for every reader.

    Pure logic: no filesystem access and no clock.  Callers that need the
    runtime baseline or a root-bound target resolver pass it in explicitly;
    when a resolver is supplied it is used to re-verify the state first.
    """
    require(not is_external(state), "evidence view requires resolved records")
    if targets is not None:
        validate(state, targets)
    tasks = keyed(state["tasks"], "tasks")
    revoked, superseded, edges = _relation_edges(state)
    reasons = reasons_by_target(edges)
    records, by_id = [], {}
    counts = {CURRENT_EFFECTIVE: 0, CURRENT_REVOKED: 0, CURRENT_SUPERSEDED: 0,
              "contributing": 0}
    expected = 0
    for record in state["evidence"]:
        task = tasks[record["task_id"]]
        key = ("evidence", record["id"])
        # kinds are named after the relationship (revocation/supersession); the
        # current state uses the adjective form (revoked/superseded).
        named = [KIND_STATE[reason] for reason in reasons.get(key, [])
                 if reason in KIND_STATE]
        if CURRENT_REVOKED in named:
            current = CURRENT_REVOKED
        elif CURRENT_SUPERSEDED in named:
            current = CURRENT_SUPERSEDED
        else:
            current = CURRENT_EFFECTIVE
        superseded_by = superseded[key][1] if key in superseded else None
        item = _evidence_item(record, task, current, superseded_by, baseline, named)
        records.append(item)
        by_id[item["id"]] = item
        counts[current] = counts.get(current, 0) + 1
        counts["contributing"] += 1 if item["contributes"] else 0
    return {"schema": EVIDENCE_VIEW_SCHEMA, "count": len(records), "records": records,
            "by_id": by_id, "counts": counts,
            "revoked": sorted([list(key) for key in revoked]),
            "superseded": {key[0] + ":" + key[1]: value[1]
                           for key, value in superseded.items()},
            "edges": edges,
            "reasons": {key[0] + ":" + key[1]: value for key, value in reasons.items()},
            "baseline_supplied": baseline is not None}

def acceptance_coverage(state, task, view=None, baseline=None, targets=None,
                        limit=20, offset=0):
    """Per acceptance-condition coverage shared by the gate, coverage and handoff."""
    view = evidence_view(state, baseline, targets) if view is None else view
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    rows = []
    for position, condition in enumerate(task["acceptance"]):
        contributors, recorded, withdrawn = [], [], []
        for item in view["records"]:
            if item["task_id"] != task["id"] or condition not in item["acceptance"]:
                continue
            if item["recorded_result"] == "pass":
                recorded.append(item["id"])
            if item["contributes"] and condition in item["acceptance_match"]:
                contributors.append(item["id"])
            elif item["recorded_result"] == "pass" and item["current"] != CURRENT_EFFECTIVE:
                withdrawn.append({"id": item["id"], "current": item["current"],
                                  "superseded_by": item["superseded_by"],
                                  "recorded_result": item["recorded_result"],
                                  "reason": item["reason"],
                                  "reasons": item["current_reasons"]})
        baseline_verified = bool(contributors) and all(
            view["by_id"][key]["baseline"] == BASELINE_RECORDED for key in contributors)
        baseline_verified = bool(contributors) and any(
            view["by_id"][key]["baseline"] == BASELINE_RECORDED for key in contributors)
        if not contributors:
            baseline_reason = "no_current_pass"
        elif baseline_verified:
            baseline_reason = None
        else:
            baseline_reason = sorted({view["by_id"][key]["baseline_reason"] or BASELINE_UNCHECKED
                                      for key in contributors})[0]
        rows.append({"task_id": task["id"], "ac_id": ac_identifier(task["id"], condition),
                     "index": position, "text": condition,
                     "task_status": task["status"], "task_generation": task["generation"],
                     "covered": bool(contributors),
                     "recorded_covered": bool(recorded),
                     "baseline_verified": baseline_verified,
                     "baseline_reason": baseline_reason,
                     "verified": bool(contributors) and baseline_verified,
                     "contributors": contributors,
                     "recorded_contributors": recorded,
                     "withdrawn": withdrawn[offset:offset + limit],
                     "withdrawn_page": {"offset": offset, "limit": limit,
                                        "total": len(withdrawn),
                                        "has_more": offset + limit < len(withdrawn)}})
    return rows


def validate(state, targets=None):
    require(isinstance(state, dict), "state must be an object")
    # The key set is exact: every optional collection present in the document
    # widens the expectation, and a collection that is absent stays absent.
    allowed = {"project", *COLLECTIONS, "extensions", "operations"}
    for optional in ("corrections", "ac_map", "markdown"):
        if optional in state:
            allowed = allowed | {optional}
    require(set(state) == allowed, "invalid state fields")
    project = state["project"]
    require(isinstance(project, dict), "project must be an object")
    require(set(project) == {"name", "goal", "status", "current_task", "next_step", "outcomes"}, "invalid project fields")
    text(project["name"], "project name")
    for key in ("goal", "next_step"):
        text(project[key], key, empty=True)
    require(project["status"] in ("active", "paused", "blocked", "complete"), "invalid project status")
    require(isinstance(project["outcomes"], dict) and set(project["outcomes"]) <= {"merge", "release", "deploy", "external"}, "invalid outcomes")
    for value in project["outcomes"].values():
        text(value, "outcome")
    require(isinstance(state["extensions"], dict), "extensions must be an object")
    compaction = state["extensions"].get("compaction")
    if compaction is not None:
        require(isinstance(compaction, dict) and set(compaction) == {"schema", "head", "count", "retained"},
                "invalid compaction metadata")
        require(compaction["schema"] == RECEIPT_SCHEMA, "invalid compaction schema")
        head = compaction["head"]
        require(head is None or (isinstance(head, str) and re.fullmatch(r"[0-9a-f]{64}\.jsonl", head)),
                "invalid compaction head")
        require(type(compaction["count"]) is int and compaction["count"] >= 0, "invalid compaction count")
        require(compaction["head"] is not None or compaction["count"] == 0, "compaction head missing")
        require(type(compaction["retained"]) is int and compaction["retained"] == RECEIPT_KEEP,
                "invalid compaction retention")
    _validate_ac_map(state["extensions"].get("ac_map"))
    _validate_external_markdown(state["extensions"].get("external_markdown"))
    scopes = _blocker_scopes(state["extensions"].get("blocker_scope"))
    tasks, blockers, decisions = (keyed(state[k], k) for k in ("tasks", "blockers", "decisions"))
    external = isinstance(state["evidence"], dict)
    corrections = {}
    if "corrections" in state:
        if isinstance(state["corrections"], dict):
            _require_index_ref(state["corrections"], "corrections")
            require(external, "an unresolved corrections index requires unresolved evidence")
        else:
            require(not external, "resolved corrections require resolved evidence")
            corrections = keyed(state["corrections"], "corrections")
    if external:
        _require_index_ref(state["evidence"], "evidence")
        evidence = {}
    else:
        evidence = keyed(state["evidence"], "evidence")
    # v5 collections are index references inside the document.  The resolved
    # state keeps the v4 shape (the acceptance map returns to
    # extensions.ac_map and the Markdown text is reconstructed on demand), so a
    # v5 document and its v4 predecessor resolve to the same logical state.
    if "ac_map" in state:
        require("ac_map" not in state["extensions"], "the acceptance map is declared twice")
        require(external, "an external acceptance map index requires an external document")
        _require_index_ref(state["ac_map"], "acceptance map")
    if "markdown" in state:
        require(external, "an external markdown index requires an external document")
        _require_index_ref(state["markdown"], "markdown")
    require(project["current_task"] is None or project["current_task"] in tasks, "current task missing")
    for task in tasks.values():
        require(set(task) <= {"id", "title", "status", "owner", "depends_on", "acceptance", "reason", "generation"}, "invalid task fields")
        text(task.get("title"), "title")
        require(task.get("status") in ("todo", "doing", "blocked", "done", "cancelled"), "invalid task status")
        require(task.get("owner") is None or isinstance(task["owner"], str), "invalid owner")
        deps = task.get("depends_on")
        require(isinstance(deps, list) and all(isinstance(x, str) for x in deps), "invalid dependencies")
        require(len(deps) == len(set(deps)) and all(x in tasks for x in deps), "missing or duplicate dependency")
        ac = task.get("acceptance")
        require(isinstance(ac, list) and all(isinstance(x, str) and x.strip() for x in ac), "invalid acceptance")
        require(len(ac) == len(set(ac)), "duplicate acceptance")
        require(type(task.get("generation")) is int and task["generation"] >= 0, "invalid task generation")
        if task["status"] == "cancelled":
            text(task.get("reason"), "cancellation reason")
        if task["status"] in ("doing", "done"):
            require(all(tasks[d]["status"] == "done" for d in deps), "dependency not complete")
    visiting, visited = set(), set()
    def visit(key):
        require(key not in visiting, "dependency cycle")
        if key in visited:
            return
        visiting.add(key)
        for dep in tasks[key]["depends_on"]:
            visit(dep)
        visiting.remove(key)
        visited.add(key)
    for key in tasks:
        visit(key)
    for blocker in blockers.values():
        require(set(blocker) <= {"id", "task_id", "description", "status", "resolution"}, "invalid blocker fields")
        require(blocker.get("task_id") in tasks, "blocker task missing")
        text(blocker.get("description"), "blocker description")
        require(blocker.get("status") in ("open", "resolved"), "invalid blocker status")
        if blocker["status"] == "resolved":
            text(blocker.get("resolution"), "blocker resolution")
        scope = scopes.get(blocker["id"])
        if scope is not None:
            require("project" in scope or ("task:" + blocker["task_id"]) in scope,
                    "blocker scope must cover its declared task")
            for item in scope:
                require(item == "project" or (item.startswith("task:") and item[5:] in tasks),
                        "blocker scope names an unknown task")
    for blocker_id in scopes:
        require(blocker_id in blockers, "blocker scope names an unknown blocker")
    for item in evidence.values():
        require(set(item) == {"id", "task_id", "check", "result", "at", "ref", "baseline", "acceptance", "generation"}, "invalid evidence fields")
        require(item["task_id"] in tasks, "evidence task missing")
        for key in ("check", "ref"):
            text(item[key], "evidence " + key)
        timestamp(item["at"])
        require(item["result"] in ("pass", "fail", "not_run"), "invalid evidence result")
        require(isinstance(item["baseline"], dict), "invalid evidence baseline")
        require(type(item["generation"]) is int and 0 <= item["generation"] <= tasks[item["task_id"]]["generation"], "invalid evidence generation")
        require(isinstance(item["acceptance"], list) and all(isinstance(a, str) and a in tasks[item["task_id"]]["acceptance"] for a in item["acceptance"]), "unknown evidence acceptance")
    revoked, replaced = _validate_corrections(corrections, evidence, decisions, targets)
    live = _effective_evidence(evidence, revoked, replaced)
    for task in tasks.values():
        blocked = any(b["status"] == "open" and _scope_blocks(scopes.get(b["id"]), b["task_id"], task["id"])
                      for b in blockers.values())
        if task["status"] == "blocked":
            require(blocked, "blocked task needs an open blocker")
        if task["status"] == "done" and not external:
            require(not blocked and task["acceptance"], "done task needs acceptance and no open blocker")
            covered = {a for e in live.values() if e["task_id"] == task["id"] and e["result"] == "pass" and e["generation"] == task["generation"] for a in e["acceptance"]}
            require(set(task["acceptance"]) <= covered, "done task needs evidence for every acceptance")
    for decision in decisions.values():
        require(set(decision) == {"id", "task_ids", "conclusion", "reason"}, "invalid decision fields")
        require(isinstance(decision["task_ids"], list) and all(isinstance(t, str) and t in tasks for t in decision["task_ids"]), "decision task missing")
        text(decision["conclusion"], "decision conclusion")
        text(decision["reason"], "decision reason")
    if project["status"] == "complete":
        require(bool(tasks) and all(t["status"] in ("done", "cancelled") for t in tasks.values()), "project not complete")
    ops = keyed(state["operations"], "operations")
    for op in ops.values():
        require(set(op) == {"id", "hash", "revision"} and isinstance(op["hash"], str) and re.fullmatch("[0-9a-f]{64}", op["hash"]) and type(op["revision"]) is int and op["revision"] >= 0, "invalid operation receipt")
    return state


def apply(state, patch, baseline, targets=None):
    """Apply typed partial upserts; no deletions, arbitrary paths or evidence rewrites."""
    require(isinstance(patch, dict) and set(patch) <= {"project", *COLLECTIONS, "extensions", "corrections", "markdown"}, "invalid change fields")
    out = copy.deepcopy(state)
    if "project" in patch:
        require(isinstance(patch["project"], dict), "project change must be object")
        out["project"].update(patch["project"])
    if "extensions" in patch:
        require(isinstance(patch["extensions"], dict), "extensions change must be object")
        for managed in ("compaction", "ac_map"):
            require(managed not in patch["extensions"], managed + " metadata is managed internally")
        out["extensions"].update(patch["extensions"])
    if "corrections" in patch:
        require(isinstance(out.get("evidence"), list),
                "corrections require a resolved v4 document")
        rows = keyed(out.get("corrections", []), "corrections")
        for key, incoming in keyed(patch["corrections"], "corrections").items():
            require(isinstance(incoming, dict), "correction entry must be an object")
            # The digest is derived here, never trusted from input.  A caller
            # may supply it (for example a reader replaying a record), and it
            # is then required to equal the derived value.
            new = copy.deepcopy(incoming)
            expected = correction_target_digest(new, out, targets)
            if "target_sha256" in incoming:
                require(incoming["target_sha256"] == expected,
                        "correction target digest mismatch")
            new["target_sha256"] = expected
            old = rows.get(key)
            if old is not None:
                require(old == new, "corrections are immutable; use a new ID")
                continue
            rows[key] = new
            out["corrections"] = list(rows.values())
    for name in COLLECTIONS:
        rows = keyed(out[name], name)
        for key, incoming in keyed(patch.get(name, []), name).items():
            old = rows.get(key)
            if old is not None and name in ("evidence", "decisions"):
                require(incoming == old, "evidence and decisions are immutable; use a new ID")
                continue
            new = copy.deepcopy(old or {})
            new.update(incoming)
            if name == "tasks":
                require("generation" not in incoming, "task generation is managed internally")
                for field, default in {"status": "todo", "owner": None, "depends_on": [], "acceptance": [], "generation": 0}.items():
                    new.setdefault(field, default)
                if old:
                    require(set(old["acceptance"]) <= set(new["acceptance"]), "acceptance conditions cannot be removed")
                    if old["status"] in ("done", "cancelled") and new["status"] != old["status"]:
                        text(incoming.get("reason"), "reopen reason")
                        new["generation"] += 1
            if name == "evidence":
                task = next((t for t in out["tasks"] if t["id"] == new.get("task_id")), None)
                require(task is not None, "evidence task missing")
                require("generation" not in incoming and "baseline" not in incoming, "evidence baseline and generation are managed internally")
                new["baseline"] = copy.deepcopy(baseline)
                new["generation"] = task["generation"]
            rows[key] = new
        out[name] = list(rows.values())
    return validate(out, targets)


def display(value):
    # Display is never parsed as task state. JSON encoding keeps injected headings inert.
    return json.dumps(value, ensure_ascii=False).replace("<", "&lt;").replace(">", "&gt;").replace("`", "&#96;")


def view(state):
    project = state["project"]
    lines = ["## Project", "- Name: " + display(project["name"]), "- Goal: " + display(project["goal"]),
             "- Status: " + project["status"], "- Next: " + display(project["next_step"]), "", "## Tasks"]
    for task in state["tasks"]:
        lines.append(f"- {task['id']} [{task['status']}] " + display(task["title"]))
    lines.extend(["", "## Open blockers"])
    lines.extend("- " + b["id"] + ": " + display(b["description"]) for b in state["blockers"] if b["status"] == "open")
    return "\n".join(lines)


def block(body, start, end):
    def matches(marker):
        pattern = r'\r?\n'.join(re.escape(line) for line in marker.split('\n'))
        return list(re.finditer(pattern, body))
    starts, ends = matches(start), matches(end)
    require(len(starts) == 1 and len(ends) == 1, "missing or duplicate managed section")
    a = starts[0].end()
    b = ends[0].start()
    require(a <= b, "invalid managed section order")
    return a, b


def parse_body(body):
    a, b = block(body, DATA_START, DATA_END)
    c, d = block(body, VIEW_START, VIEW_END)
    require(b + len(DATA_END) <= c - len(VIEW_START) or d + len(VIEW_END) <= a - len(DATA_START), "overlapping managed sections")
    state = validate(loads(body[a:b]))
    return state, body[c:d].replace('\r\n', '\n') in (view(state), compact_view(state))


def compact_view(state):
    return "# Project progress\n\nStatus: " + state["project"]["status"] + "\nSee the structured block for tasks, evidence, blockers and next step.\n"


def render_body(state, body=None, compact=False):
    validate(state)
    if body is None:
        body = DATA_START + "{}" + DATA_END + "\n\n" + VIEW_START + "" + VIEW_END + "\n"
    if body is not None:
        a, b = block(body, VIEW_START, VIEW_END)
        compact = compact or body[a:b].startswith("# Project progress\n\nStatus: ")
    replacements = [(DATA_START, DATA_END, json.dumps(state, ensure_ascii=False,
                    indent=None if compact else 2, separators=(",", ":") if compact else None, allow_nan=False)),
                    (VIEW_START, VIEW_END, compact_view(state) if compact else view(state))]
    for start, end, value in replacements:
        a, b = block(body, start, end)
        body = body[:a] + value + body[b:]
    return body


def summary(state, baseline):
    counts = {status: sum(t["status"] == status for t in state["tasks"]) for status in ("todo", "doing", "blocked", "done", "cancelled")}
    view = evidence_view(state, baseline)
    evidence = [{"id": item["id"], "result": item["recorded_result"],
                 "verification": "recorded" if item["baseline"] == BASELINE_RECORDED
                 and item["generation_match"] else "UNVERIFIED",
                 "current": item["current"], "baseline": item["baseline"],
                 "generation_match": item["generation_match"]}
                for item in view["records"]]
    return {"project": state["project"], "tasks": state["tasks"], "counts": counts,
            "blockers": [b for b in state["blockers"] if b["status"] == "open"], "evidence": evidence}
