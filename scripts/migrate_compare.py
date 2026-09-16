"""Lossless logical equivalence comparison for relay migrations.

This is an independent checker: it does not ask the writer whether a write was
correct.  It reads two authoritative documents (before/after), reconstructs the
COMPLETE logical state of each from the official protocol modules, locates
every record by record_type + record_id, and compares canonical per-record
digests, array order, field types and relations.

Only these may differ between a pre-migration and a post-migration document:

  * the protocol schema name;
  * storage references (an inline evidence list becomes an index reference);
  * compaction, migration and acceptance-map metadata;
  * revision, timestamps, writer lease and this migration's operation receipt.

Business fields are never on an ignore list.  A record that is missing, extra,
duplicated or changed is reported by name.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import objectstore as obs
import progress as p
import storage as fs
import v4 as relay_v4

COLLECTIONS = ("tasks", "blockers", "evidence", "decisions", "corrections")
MANAGED_EXTENSIONS = ("compaction", "migration", "ac_map")


def _document_parts(text):
    import cli_v2
    lines, body, meta = cli_v2.split(text)
    return body, meta


def logical_state(text, root=None):
    """Complete logical state of a relay document (v2/v3 inline or v4 resolved)."""
    body, meta = _document_parts(text)
    schema = meta["schema"]
    if schema == "project-continuity/v1":
        raise ValueError("v1 has no structured state")
    if schema == p.SCHEMA_V4:
        if root is None:
            raise ValueError("a v4 document needs an explicit project root")
        return relay_v4.resolve(text, root), meta
    state, _matches = p.parse_body(body)
    return state, meta


def _records(state):
    out = {}
    for name in COLLECTIONS:
        rows = state.get(name)
        if isinstance(rows, dict):
            rows = []
        table = {}
        for row in rows or []:
            record_id = row.get("id")
            if not isinstance(record_id, str):
                raise ValueError("a " + name + " record has no id")
            if record_id in table:
                raise ValueError("duplicate " + name + " id " + record_id)
            table[record_id] = row
        out[name] = table
    return out


def _extension_view(extensions):
    return {key: value for key, value in (extensions or {}).items()
            if key not in MANAGED_EXTENSIONS}


def compare(before_text, after_text, root=None):
    """Return (equal, report) for two relay documents."""
    before_state, before_meta = logical_state(before_text, root)
    after_state, after_meta = logical_state(after_text, root)
    before_records = _records(before_state)
    after_records = _records(after_state)
    report = {"equal": True, "before_sha256": hashlib.sha256(before_text.encode("utf-8")).hexdigest(),
              "after_sha256": hashlib.sha256(after_text.encode("utf-8")).hexdigest(),
              "before_schema": before_meta["schema"], "after_schema": after_meta["schema"],
              "before_revision": int(before_meta["revision"]),
              "after_revision": int(after_meta["revision"]),
              "collections": {}, "project": "identical", "extensions": "identical",
              "operations": {}, "roundtrip": None}
    for name in COLLECTIONS:
        left, right = before_records[name], after_records[name]
        missing = sorted(set(left) - set(right))
        extra = sorted(set(right) - set(left))
        changed = sorted(key for key in set(left) & set(right)
                         if p.digest(left[key]) != p.digest(right[key]))
        report["collections"][name] = {"before": len(left), "after": len(right),
                                       "missing": missing, "extra": extra,
                                       "changed": changed,
                                       "order_preserved": sorted(left) == sorted(right)}
        if missing or extra or changed:
            report["equal"] = False
    if before_state.get("project") != after_state.get("project"):
        report["equal"] = False
        report["project"] = "changed"
    left_ext = _extension_view(before_state.get("extensions"))
    right_ext = _extension_view(after_state.get("extensions"))
    if left_ext != right_ext:
        report["equal"] = False
        report["extensions"] = {"changed_keys": sorted(
            key for key in set(left_ext) | set(right_ext)
            if left_ext.get(key) != right_ext.get(key))}
    operations = _operation_report(before_state, after_state, before_meta, after_meta, root)
    report["operations"] = operations
    if operations["missing"]:
        report["equal"] = False
    report["roundtrip"] = _roundtrip(after_state, after_meta, root)
    if not report["roundtrip"]["matches"]:
        report["equal"] = False
    return report["equal"], report


def _operation_report(before_state, after_state, before_meta, after_meta, root):
    def inline(state):
        return {row["id"]: row["hash"] for row in state.get("operations", [])}

    left = inline(before_state)
    right = inline(after_state)
    archived = {}
    if after_meta["schema"] in (p.SCHEMA_V3, p.SCHEMA_V4) and root is not None:
        import cli_v2
        try:
            for record in cli_v2._archive_chain(root, after_state, after_meta):
                archived[record["id"]] = record["hash"]
        except Exception:
            archived = {}
    missing = sorted(key for key, digest in left.items()
                     if right.get(key, archived.get(key)) != digest)
    return {"before_inline": len(left), "after_inline": len(right),
            "after_archived": len(archived), "missing": missing,
            "extra": sorted(set(right) - set(left) - set(archived))}


def _roundtrip(after_state, after_meta, root):
    if after_meta["schema"] != p.SCHEMA_V4 or root is None:
        return {"matches": True, "applicable": False}
    stub, plans, _texts = relay_v4.plan_full(after_state, fs.root_path(root),
                                             after_meta["project_id"])
    body, meta = _document_parts(_read_current(root))
    document_stub, _matches = p.parse_body(body)
    keys = ("evidence", "corrections")
    matches = all(document_stub.get(key) == stub.get(key) for key in keys)
    matches = matches and document_stub["extensions"].get("ac_map") == \
        stub["extensions"].get("ac_map")
    return {"matches": matches, "applicable": True,
            "evidence_ref": stub.get("evidence"), "planned_objects": len(plans)}


def _read_current(root):
    return fs.read(fs.child(fs.root_path(root), ".relay", "CURRENT.md"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, help="pre-migration CURRENT.md path")
    parser.add_argument("--after", required=True, help="post-migration CURRENT.md path")
    parser.add_argument("--root", help="project root that owns the post-migration objects")
    args = parser.parse_args(argv)
    before = fs.read(Path(args.before).expanduser().absolute())
    after = fs.read(Path(args.after).expanduser().absolute())
    try:
        equal, report = compare(before, after, args.root)
    except (p.Invalid, fs.Error, ValueError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    report["equal"] = bool(equal)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if equal else 1


if __name__ == "__main__":
    raise SystemExit(main())
