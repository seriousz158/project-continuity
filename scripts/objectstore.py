"""Content-addressed immutable relay objects for project-continuity/v4.

Layout (every physical file name is the SHA-256 of its own exact bytes):

  .relay/objects/evidence/<aa>/<object_sha>.json
  .relay/objects/correction/<aa>/<object_sha>.json
  .relay/objects/manifest/<aa>/<manifest_sha>.json
  .relay/objects/chunk/<aa>/<chunk_sha>.bin
  .relay/objects/index/evidence/<aa>/<index_sha>.json
  .relay/objects/index/correction/<aa>/<index_sha>.json

An object envelope binds the schema, project id, record type and content
format version; the record itself is preserved verbatim under "payload".
Objects larger than OBJECT_MAX_BYTES are split into ordered raw chunks with a
parent manifest, and the manifest is itself content addressed.

Nothing here mutates an existing file.  Publication uses the official
storage.immutable primitive, so a same-name object is either byte-identical
or a named refusal.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

import storage as fs
from relay_errors import (
    RelayError,
    RELAY_CHUNK_MISSING,
    RELAY_DISK_SPACE_INSUFFICIENT,
    RELAY_INDEX_INVALID,
    RELAY_OBJECT_HASH_MISMATCH,
    RELAY_OBJECT_LIMIT_EXCEEDED,
    RELAY_OBJECT_MISSING,
    RELAY_OBJECT_PATH_INVALID,
    RELAY_OBJECT_PROJECT_MISMATCH,
    RELAY_OBJECT_SCHEMA_INVALID,
    RELAY_OBJECT_TYPE_INVALID,
    RELAY_REFERENCE_CYCLE,
    RELAY_STORAGE_QUOTA_EXCEEDED,
    RELAY_VALIDATION_BUDGET_EXCEEDED,
)

OBJECT_SCHEMA = "project-continuity/object/v1"
INDEX_SCHEMA = "project-continuity/index/v1"
MANIFEST_SCHEMA = "project-continuity/chunk-manifest/v1"
RECORD_FORMATS = {
    "evidence": "project-continuity/evidence-record/v1",
    "correction": "project-continuity/correction-record/v1",
    # v5 externalises the project's custom Markdown and its acceptance map
    # through the same content-addressed machinery: the document keeps an index
    # reference, never a second copy of the text.
    "markdown": "project-continuity/markdown-record/v1",
    # The namespace grammar is lower-case and hyphenated: the JSON key keeps its
    # underscore (extensions.ac_map), the object namespace does not.
    "ac-map": "project-continuity/acceptance-map-record/v1",
}
RECORD_TYPES = tuple(sorted(RECORD_FORMATS))
OBJECT_MAX_BYTES = 262144
MAX_INDEX_ENTRIES = 512
MAX_INDEX_DEPTH = 3
MAX_OBJECTS = 200000
MAX_LOGICAL_BYTES = 64 * 1024 * 1024
VALIDATION_SECONDS = 30.0
OBJECT_SOFT_QUOTA_BYTES = 256 * 1024 * 1024
OBJECT_HARD_QUOTA_BYTES = 1024 * 1024 * 1024
DISK_RESERVE_BYTES = 64 * 1024 * 1024

_HEX64 = re.compile(r"[0-9a-f]{64}")
_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_hex64(value) -> bool:
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def require_hex64(value, code, what):
    if not is_hex64(value):
        raise RelayError(code, what)


def require_record_type(record_type):
    if record_type not in RECORD_FORMATS:
        raise RelayError(RELAY_OBJECT_TYPE_INVALID, "unknown record type")


def loads_named(text, code, what):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise RelayError(code, what + " has a duplicate key")
            result[key] = value
        return result
    try:
        return json.loads(text, object_pairs_hook=pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(RelayError(code, what + " has a non-finite number")))
    except RelayError:
        raise
    except (ValueError, RecursionError) as exc:
        raise RelayError(code, what + " is not valid JSON") from exc


class Budget:
    """Bounded deep-validation budget.  Exhaustion is never corruption."""

    def __init__(self, seconds=VALIDATION_SECONDS, max_objects=MAX_OBJECTS,
                 max_bytes=MAX_LOGICAL_BYTES):
        self.deadline = time.monotonic() + seconds
        self.files = 0
        self.bytes = 0
        self.max_objects = max_objects
        self.max_bytes = max_bytes

    def consume(self, size=0):
        self.files += 1
        self.bytes += int(size)
        if self.files > self.max_objects:
            raise RelayError(RELAY_VALIDATION_BUDGET_EXCEEDED, "object count budget exceeded")
        if self.bytes > self.max_bytes:
            raise RelayError(RELAY_VALIDATION_BUDGET_EXCEEDED, "logical byte budget exceeded")
        if time.monotonic() > self.deadline:
            raise RelayError(RELAY_VALIDATION_BUDGET_EXCEEDED, "validation time budget exceeded")


def objects_root(root: Path) -> Path:
    return fs.child(root, ".relay", "objects")


def object_path(root: Path, record_type: str, sha: str) -> Path:
    require_record_type(record_type)
    require_hex64(sha, RELAY_OBJECT_TYPE_INVALID, "object digest")
    return fs.child(root, ".relay", "objects", record_type, sha[:2], sha + ".json")


def index_path(root: Path, record_type: str, sha: str) -> Path:
    require_record_type(record_type)
    require_hex64(sha, RELAY_OBJECT_TYPE_INVALID, "index digest")
    return fs.child(root, ".relay", "objects", "index", record_type, sha[:2], sha + ".json")


def manifest_path(root: Path, sha: str) -> Path:
    require_hex64(sha, RELAY_OBJECT_TYPE_INVALID, "manifest digest")
    return fs.child(root, ".relay", "objects", "manifest", sha[:2], sha + ".json")


def chunk_path(root: Path, sha: str) -> Path:
    require_hex64(sha, RELAY_OBJECT_TYPE_INVALID, "chunk digest")
    return fs.child(root, ".relay", "objects", "chunk", sha[:2], sha + ".bin")


def manifest_by_sha_path(root: Path, sha: str) -> Path:
    return manifest_path(root, sha)


def record_id_of(payload, code, what):
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) \
            or _IDENTIFIER.fullmatch(payload["id"]) is None:
        raise RelayError(code, what + " has no valid record id")
    return payload["id"]


def envelope_text(project_id: str, record_type: str, payload: dict) -> str:
    """Canonical object envelope text, used for planning and secret scanning."""
    require_record_type(record_type)
    return canonical({"schema": OBJECT_SCHEMA, "project_id": project_id,
                      "record_type": record_type, "format": RECORD_FORMATS[record_type],
                      "payload": payload})


def plan_payload(root: Path, project_id: str, record_type: str, payload: dict) -> tuple:
    """Return (plans, object_sha) without touching the filesystem."""
    require_record_type(record_type)
    data = envelope_text(project_id, record_type, payload).encode("utf-8")
    object_sha = sha256_hex(data)
    plans = []
    if len(data) <= OBJECT_MAX_BYTES:
        plans.append({"kind": "object", "path": object_path(root, record_type, object_sha),
                      "content": data, "sha256": object_sha})
        return plans, object_sha
    refs = []
    for offset in range(0, len(data), OBJECT_MAX_BYTES):
        chunk = data[offset:offset + OBJECT_MAX_BYTES]
        chunk_sha = sha256_hex(chunk)
        refs.append({"sha256": chunk_sha, "bytes": len(chunk)})
        plans.append({"kind": "chunk", "path": chunk_path(root, chunk_sha),
                      "content": chunk, "sha256": chunk_sha})
    manifest = {"schema": MANIFEST_SCHEMA, "project_id": project_id,
                "record_type": record_type, "format": RECORD_FORMATS[record_type],
                "object_sha256": object_sha, "object_bytes": len(data),
                "chunk_bytes": OBJECT_MAX_BYTES, "count": len(refs), "chunks": refs}
    manifest_data = canonical(manifest).encode("utf-8")
    manifest_sha = sha256_hex(manifest_data)
    plans.append({"kind": "manifest", "path": manifest_path(root, manifest_sha),
                  "content": manifest_data, "sha256": manifest_sha})
    return plans, object_sha


def plan_index(root: Path, project_id: str, record_type: str, entries: list) -> tuple:
    """Return (plans, root_index_sha_or_None) for a bounded index hierarchy."""
    require_record_type(record_type)
    if not entries:
        return [], None
    plans = []

    def emit(node):
        data = canonical(node).encode("utf-8")
        sha = sha256_hex(data)
        plans.append({"kind": "index", "path": index_path(root, record_type, sha),
                      "content": data, "sha256": sha})
        return sha

    levels = 1
    while len(entries) > MAX_INDEX_ENTRIES ** levels:
        levels += 1
        if levels > MAX_INDEX_DEPTH:
            raise RelayError(RELAY_OBJECT_LIMIT_EXCEEDED, "index depth exceeded")
    nodes = []
    for offset in range(0, len(entries), MAX_INDEX_ENTRIES):
        group = entries[offset:offset + MAX_INDEX_ENTRIES]
        node = {"schema": INDEX_SCHEMA, "project_id": project_id,
                "record_type": record_type, "kind": "leaf", "count": len(group),
                "entries": group}
        nodes.append({"sha256": emit(node), "kind": "leaf", "count": len(group)})
    for depth in range(levels, 1, -1):
        parents = []
        for offset in range(0, len(nodes), MAX_INDEX_ENTRIES):
            group = nodes[offset:offset + MAX_INDEX_ENTRIES]
            node = {"schema": INDEX_SCHEMA, "project_id": project_id,
                    "record_type": record_type, "kind": "branch", "depth": depth - 1,
                    "count": sum(item["count"] for item in group), "children": group}
            parents.append({"sha256": emit(node), "kind": "branch",
                            "count": sum(item["count"] for item in group)})
        nodes = parents
    return plans, nodes[0]["sha256"]


def plan_bytes(plans: list) -> int:
    total = 0
    for plan in plans:
        if not os.path.lexists(plan["path"]):
            total += len(plan["content"])
    return total


def publish(root: Path, plans: list) -> dict:
    warnings, created, written = [], 0, 0
    directories = []
    for plan in plans:
        fs.private_dir(plan["path"].parent)
        directories.append(plan["path"].parent)
        existed = os.path.lexists(plan["path"])
        warnings.extend(fs.immutable_bytes(plan["path"], plan["content"]))
        if not existed:
            created += 1
            written += len(plan["content"])
    for directory in dict.fromkeys(str(path) for path in directories):
        warnings.extend(fs.fsync_directory(Path(directory)))
    seen, unique = set(), []
    for warning in warnings:
        if warning not in seen:
            seen.add(warning)
            unique.append(warning)
    return {"created": created, "bytes_written": written, "warnings": unique}


def _read_hashed(path: Path, expected: str, budget: Budget, code: str, what: str) -> bytes:
    if not os.path.lexists(path):
        raise RelayError(code, what + " is missing")
    budget.consume(0)
    try:
        data = fs.read_bytes(path, max_bytes=OBJECT_MAX_BYTES)
    except fs.Error as exc:
        raise RelayError(RELAY_OBJECT_PATH_INVALID, what + " is not a readable regular file") from exc
    budget.consume(len(data))
    if sha256_hex(data) != expected:
        raise RelayError(RELAY_OBJECT_HASH_MISMATCH, what + " does not match its digest")
    return data


def _envelope(data: bytes, project_id: str, record_type: str, what: str) -> dict:
    text = data.decode("utf-8")
    value = loads_named(text, RELAY_OBJECT_SCHEMA_INVALID, what)
    if not isinstance(value, dict) or set(value) != {"schema", "project_id", "record_type", "format", "payload"}:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, what + " has invalid envelope fields")
    if value["schema"] != OBJECT_SCHEMA or value["record_type"] != record_type \
            or value["format"] != RECORD_FORMATS[record_type]:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, what + " has an invalid type or format")
    if not isinstance(value["project_id"], str) \
            or _IDENTIFIER.fullmatch(value["project_id"]) is None:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, what + " project id")
    if value["project_id"] != project_id:
        raise RelayError(RELAY_OBJECT_PROJECT_MISMATCH, what + " belongs to another project")
    if not isinstance(value["payload"], dict):
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, what + " payload is not an object")
    return value


def read_object(root: Path, project_id: str, record_type: str, entry: dict,
                budget: Budget) -> dict:
    require_record_type(record_type)
    object_sha = entry["object_sha256"]
    if entry["storage"] == "single":
        data = _read_hashed(object_path(root, record_type, object_sha), object_sha, budget,
                            RELAY_OBJECT_MISSING, record_type + " object")
        if len(data) != entry["object_bytes"]:
            raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, record_type + " object length mismatch")
    else:
        manifest = read_manifest_object(root, project_id, entry["manifest_sha256"], budget)
        if manifest["record_type"] != record_type:
            raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest type or format mismatch")
        if manifest["object_sha256"] != object_sha or manifest["object_bytes"] != entry["object_bytes"]:
            raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest identity mismatch")
        chunks = manifest["chunks"]
        if not isinstance(chunks, list) or manifest["count"] != len(chunks) or not chunks:
            raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest count mismatch")
        parts = []
        for index, ref in enumerate(chunks):
            if not isinstance(ref, dict) or set(ref) != {"sha256", "bytes"} \
                    or not is_hex64(ref["sha256"]) or type(ref["bytes"]) is not int \
                    or not 0 < ref["bytes"] <= OBJECT_MAX_BYTES:
                raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk reference is invalid")
            if index < len(chunks) - 1 and ref["bytes"] != OBJECT_MAX_BYTES:
                raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "only the final chunk may be short")
            try:
                part = _read_hashed(chunk_path(root, ref["sha256"]), ref["sha256"], budget,
                                    RELAY_CHUNK_MISSING, "chunk")
            except RelayError as exc:
                if exc.code == RELAY_OBJECT_MISSING:
                    raise RelayError(RELAY_CHUNK_MISSING, "chunk is missing") from exc
                raise
            if len(part) != ref["bytes"]:
                raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk length mismatch")
            parts.append(part)
        data = b"".join(parts)
        if len(data) != manifest["object_bytes"] or sha256_hex(data) != object_sha:
            raise RelayError(RELAY_OBJECT_HASH_MISMATCH, "reassembled object does not match its digest")
    envelope = _envelope(data, project_id, record_type, record_type + " object")
    if len(canonical(envelope).encode("utf-8")) != len(data):
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, record_type + " object is not canonical")
    if envelope["payload"].get("id") != entry["record_id"]:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, record_type + " object id mismatch")
    return envelope["payload"]


def _validate_entry(entry, record_type):
    if not isinstance(entry, dict) or set(entry) != {
            "record_type", "record_id", "object_sha256", "storage", "object_bytes",
            "manifest_sha256"}:
        raise RelayError(RELAY_INDEX_INVALID, "index entry has invalid fields")
    if entry["record_type"] != record_type:
        raise RelayError(RELAY_INDEX_INVALID, "index entry record type mismatch")
    if not isinstance(entry["record_id"], str) or _IDENTIFIER.fullmatch(entry["record_id"]) is None:
        raise RelayError(RELAY_INDEX_INVALID, "index entry has an invalid record id")
    if not is_hex64(entry["object_sha256"]):
        raise RelayError(RELAY_INDEX_INVALID, "index entry has an invalid object digest")
    if entry["storage"] not in ("single", "chunked"):
        raise RelayError(RELAY_INDEX_INVALID, "index entry storage kind is invalid")
    if type(entry["object_bytes"]) is not int or entry["object_bytes"] <= 0:
        raise RelayError(RELAY_INDEX_INVALID, "index entry object size is invalid")
    if entry["storage"] == "single":
        if entry["manifest_sha256"] is not None:
            raise RelayError(RELAY_INDEX_INVALID, "single object carries a manifest digest")
    elif not is_hex64(entry["manifest_sha256"]):
        raise RelayError(RELAY_INDEX_INVALID, "chunked object has no manifest digest")


def read_index(root: Path, project_id: str, record_type: str, index_sha,
               budget: Budget) -> list:
    require_record_type(record_type)
    if index_sha is None:
        return []
    require_hex64(index_sha, RELAY_INDEX_INVALID, "index digest")
    seen, entries = set(), []

    def walk(sha, depth):
        if depth > MAX_INDEX_DEPTH:
            raise RelayError(RELAY_OBJECT_LIMIT_EXCEEDED, "index depth exceeded")
        if sha in seen:
            raise RelayError(RELAY_REFERENCE_CYCLE, "index node referenced twice")
        seen.add(sha)
        raw = _read_hashed(index_path(root, record_type, sha), sha, budget,
                           RELAY_INDEX_INVALID, "index node")
        node = loads_named(raw.decode("utf-8"), RELAY_INDEX_INVALID, "index node")
        if not isinstance(node, dict):
            raise RelayError(RELAY_INDEX_INVALID, "index node is not an object")
        base = {"schema", "project_id", "record_type", "kind", "count"}
        if not base.issubset(set(node)):
            raise RelayError(RELAY_INDEX_INVALID, "index node has invalid fields")
        if node["schema"] != INDEX_SCHEMA or node["record_type"] != record_type:
            raise RelayError(RELAY_INDEX_INVALID, "index node type mismatch")
        if node["project_id"] != project_id:
            raise RelayError(RELAY_OBJECT_PROJECT_MISMATCH, "index node belongs to another project")
        if node["kind"] == "leaf":
            if set(node) != base | {"entries"} or not isinstance(node["entries"], list):
                raise RelayError(RELAY_INDEX_INVALID, "leaf index node is invalid")
            if node["count"] != len(node["entries"]):
                raise RelayError(RELAY_INDEX_INVALID, "leaf index count mismatch")
            for entry in node["entries"]:
                _validate_entry(entry, record_type)
                entries.append(entry)
        elif node["kind"] == "branch":
            if set(node) != base | {"depth", "children"} or not isinstance(node["children"], list) \
                    or not node["children"] or type(node["depth"]) is not int:
                raise RelayError(RELAY_INDEX_INVALID, "branch index node is invalid")
            if node["depth"] != depth:
                raise RelayError(RELAY_INDEX_INVALID, "branch index depth mismatch")
            if node["count"] != sum(child["count"] for child in node["children"]):
                raise RelayError(RELAY_INDEX_INVALID, "branch index count mismatch")
            if len(node["children"]) > MAX_INDEX_ENTRIES:
                raise RelayError(RELAY_OBJECT_LIMIT_EXCEEDED, "index node has too many children")
            for child in node["children"]:
                if not isinstance(child, dict) or set(child) != {"sha256", "kind", "count"} \
                        or not is_hex64(child["sha256"]) or child["kind"] not in ("leaf", "branch") \
                        or type(child["count"]) is not int or child["count"] <= 0:
                    raise RelayError(RELAY_INDEX_INVALID, "index child is invalid")
                walk(child["sha256"], depth + 1)
        else:
            raise RelayError(RELAY_INDEX_INVALID, "unknown index node kind")

    walk(index_sha, 1)
    for entry in entries:
        if not isinstance(entry, dict):
            raise RelayError(RELAY_INDEX_INVALID, "index entry is not an object")
    if len(entries) > MAX_OBJECTS:
        raise RelayError(RELAY_OBJECT_LIMIT_EXCEEDED, "index holds too many entries")
    return entries


def collect(root: Path, project_id: str, record_type: str, index_sha, budget: Budget) -> list:
    """Resolve one external collection into its ordered logical records."""
    entries = read_index(root, project_id, record_type, index_sha, budget)
    records = []
    for entry in entries:
        records.append(read_object(root, project_id, record_type, entry, budget))
    return records


def entry_for(root: Path, project_id: str, record_type: str, payload: dict) -> tuple:
    """Return (index_entry, publish_plans) for one immutable record."""
    plans, object_sha = plan_payload(root, project_id, record_type, payload)
    single = len(plans) == 1 and plans[0]["kind"] == "object"
    manifest_sha = None if single else plans[-1]["sha256"]
    object_bytes = len(plans[0]["content"]) if single else sum(
        len(plan["content"]) for plan in plans if plan["kind"] == "chunk")
    entry = {"record_type": record_type, "record_id": record_id_of(
                payload, RELAY_OBJECT_SCHEMA_INVALID, record_type),
             "object_sha256": object_sha, "storage": "single" if single else "chunked",
             "object_bytes": object_bytes, "manifest_sha256": manifest_sha}
    return entry, plans


def manifest_exists(root: Path, sha: str) -> bool:
    return is_hex64(sha) and os.path.lexists(manifest_path(root, sha))


MANIFEST_FIELDS = frozenset({
    "schema", "project_id", "record_type", "format", "object_sha256",
    "object_bytes", "chunk_bytes", "count", "chunks"})


def read_manifest_object(root: Path, project_id: str, sha: str, budget: Budget) -> dict:
    """Resolve one chunk manifest by content address, without a search path.

    The digest is verified against the file's own bytes, the schema and record
    type are checked, and the project binding is enforced.  Reachability from a
    committed index is deliberately the caller's decision.
    """
    require_hex64(sha, RELAY_OBJECT_TYPE_INVALID, "manifest digest")
    raw = _read_hashed(manifest_path(root, sha), sha, budget,
                       RELAY_OBJECT_MISSING, "chunk manifest")
    node = loads_named(raw.decode("utf-8"), RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest")
    if not isinstance(node, dict) or set(node) != set(MANIFEST_FIELDS):
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest has invalid fields")
    if node["schema"] != MANIFEST_SCHEMA:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest schema mismatch")
    if node["record_type"] not in RECORD_FORMATS \
            or node["format"] != RECORD_FORMATS[node["record_type"]]:
        raise RelayError(RELAY_OBJECT_TYPE_INVALID, "chunk manifest record type mismatch")
    if not isinstance(node["project_id"], str) or _IDENTIFIER.fullmatch(node["project_id"]) is None:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest project id")
    if node["project_id"] != project_id:
        raise RelayError(RELAY_OBJECT_PROJECT_MISMATCH, "chunk manifest belongs to another project")
    if not is_hex64(node["object_sha256"]) or type(node["object_bytes"]) is not int \
            or node["object_bytes"] <= 0 or type(node["chunk_bytes"]) is not int \
            or node["chunk_bytes"] <= 0 or type(node["count"]) is not int or node["count"] <= 0:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest identity is invalid")
    if not isinstance(node["chunks"], list) or node["count"] != len(node["chunks"]):
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest count mismatch")
    return node


def chunked_manifest_digests(root: Path, project_id: str, record_type: str, index_sha,
                             budget=None) -> list:
    """Manifest digests reachable from one committed collection index."""
    budget = budget if budget is not None else Budget()
    return [entry["manifest_sha256"]
            for entry in read_index(root, project_id, record_type, index_sha, budget)
            if entry["storage"] == "chunked"]


def index_paths(root: Path, project_id: str, record_type: str, index_sha,
               budget=None) -> list:
    """Relative object file names (under .relay/) reachable from a collection."""
    budget = budget if budget is not None else Budget()
    entries = read_index(root, project_id, record_type, index_sha, budget)
    if index_sha is None:
        return []
    relay = fs.child(root, ".relay")
    out, seen = [], set()

    def walk(sha, depth):
        if depth > MAX_INDEX_DEPTH:
            raise RelayError(RELAY_OBJECT_LIMIT_EXCEEDED, "index depth exceeded")
        if sha in seen:
            raise RelayError(RELAY_REFERENCE_CYCLE, "index node referenced twice")
        seen.add(sha)
        path = index_path(root, record_type, sha)
        raw = _read_hashed(path, sha, budget, RELAY_INDEX_INVALID, "index node")
        out.append(str(path.relative_to(relay)))
        node = loads_named(raw.decode("utf-8"), RELAY_INDEX_INVALID, "index node")
        if node.get("kind") == "branch":
            for child in node["children"]:
                walk(child["sha256"], depth + 1)

    walk(index_sha, 1)
    for entry in entries:
        if entry["storage"] == "single":
            out.append(str(object_path(root, record_type,
                                       entry["object_sha256"]).relative_to(relay)))
        else:
            manifest = manifest_path(root, entry["manifest_sha256"])
            raw = _read_hashed(manifest, entry["manifest_sha256"], budget,
                               RELAY_OBJECT_MISSING, "chunk manifest")
            out.append(str(manifest.relative_to(relay)))
            node = loads_named(raw.decode("utf-8"), RELAY_OBJECT_SCHEMA_INVALID, "chunk manifest")
            for ref in node["chunks"]:
                out.append(str(chunk_path(root, ref["sha256"]).relative_to(relay)))
    unique = sorted(set(out))
    if len(unique) > MAX_OBJECTS:
        raise RelayError(RELAY_OBJECT_LIMIT_EXCEEDED, "too many reachable object files")
    return unique


def store_stats(root: Path) -> dict:
    base = objects_root(root)
    files = 0
    total = 0
    if base.is_dir() and not base.is_symlink():
        for current, directories, names in os.walk(base, followlinks=False):
            directories[:] = [name for name in directories
                              if not os.path.islink(os.path.join(current, name))]
            for name in names:
                path = Path(current) / name
                if path.is_symlink():
                    continue
                try:
                    total += path.stat().st_size
                    files += 1
                except OSError:
                    continue
    return {"files": files, "bytes": total}


def disk_free(root: Path) -> int:
    try:
        return shutil.disk_usage(str(root)).free
    except OSError:
        return -1


def check_write_budget(root: Path, new_bytes: int) -> dict:
    stats = store_stats(root)
    if stats["bytes"] + new_bytes > OBJECT_HARD_QUOTA_BYTES:
        raise RelayError(RELAY_STORAGE_QUOTA_EXCEEDED,
                         "object store hard quota would be exceeded")
    free = disk_free(root)
    if free < 0:
        raise RelayError(RELAY_DISK_SPACE_INSUFFICIENT, "disk space is unavailable")
    if free < new_bytes + DISK_RESERVE_BYTES:
        raise RelayError(RELAY_DISK_SPACE_INSUFFICIENT, "insufficient free disk space")
    return {"object_files": stats["files"], "object_bytes": stats["bytes"],
            "soft_quota_bytes": OBJECT_SOFT_QUOTA_BYTES,
            "hard_quota_bytes": OBJECT_HARD_QUOTA_BYTES,
            "disk_free_bytes": free, "reserve_bytes": DISK_RESERVE_BYTES}
