"""project-continuity/v4: external objects, the evidence index and resolver.

Public contract:

  resolve(document_text, project_root) -> complete logical state

project_root is explicit: a v4 document is never resolved from the current
working directory, an environment guess or a candidate search.  An envelope
parse (front matter plus the managed JSON) is available read-only, but an
envelope or a stub is never a completed state and never decides a completion
claim.

The store is authoritative only through the resolver: CURRENT.md carries a
content-addressed index reference, the index carries ordered immutable object
references, and the objects carry the unmodified records.
"""
from __future__ import annotations

import base64
import copy
import json

import objectstore as obs
import progress as p
import storage as fs
from relay_errors import (
    RelayError,
    RELAY_AC_MAP_MISMATCH,
    RELAY_MARKDOWN_DIGEST_MISMATCH,
    RELAY_PAGE_CURSOR_STALE,
    RELAY_CORRECTION_TARGET_UNSUPPORTED,
    RELAY_CORRECTION_TARGET_UNREACHABLE,
    RELAY_INDEX_INVALID,
    RELAY_OBJECT_SCHEMA_INVALID,
    RELAY_OBJECT_TYPE_INVALID,
    RELAY_SCHEMA_V4_REQUIRES_RESOLVER,
    RELAY_VALIDATION_BUDGET_EXCEEDED,
)

V4 = p.SCHEMA_V4
# v2 changes the meaning of the per-evidence "current" field from a generation
# match boolean to a named validity state, and adds baseline columns.
CURSOR_PREFIX = "pc1."

# v3: acceptance pagination is independently addressable, every list
# discloses its own page, and verification is an explicit record of
# named checks.
COVERAGE_SCHEMA = "project-continuity/coverage-matrix/v3"
HANDOFF_SCHEMA = "project-continuity/handoff-view/v3"


def _require(condition, message):
    if not condition:
        raise p.Invalid(message)


def _page(limit, offset, total):
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return {"offset": offset, "limit": limit, "total": total,
            "has_more": offset + limit < total}


def parse_envelope(document_text):
    """Read-only front matter plus managed body.  Never a completed state."""
    import cli_v2
    _lines, body, meta = cli_v2.split(document_text)
    return meta, body


def envelope_state(document_text):
    """Return (meta, stub_state, view_matches) for the authoritative JSON."""
    meta, body = parse_envelope(document_text)
    schema = meta["schema"]
    if schema == "project-continuity/v1":
        return meta, None, True
    state, matches = p.parse_body(body)
    return meta, state, bool(matches)


def resolve(document_text, project_root=None):
    """Resolve a relay document into its complete logical state."""
    meta, stub, _matches = envelope_state(document_text)
    schema = meta["schema"]
    if schema == "project-continuity/v1":
        return None
    if schema in (p.SCHEMA_V2, p.SCHEMA_V3):
        return stub
    if schema not in EXTERNAL_SCHEMAS:
        raise RelayError(RELAY_OBJECT_SCHEMA_INVALID, "unsupported relay schema")
    return expand(stub, project_root, meta["project_id"])


def expand(stub, project_root, project_id, budget=None):
    """Reconstruct the complete logical state from the bound objects."""
    if project_root is None:
        raise RelayError(RELAY_SCHEMA_V4_REQUIRES_RESOLVER,
                         "an external document requires an explicit project root")
    _require(isinstance(stub, dict) and p.is_external(stub),
             "expansion requires an external evidence index")
    root = fs.root_path(project_root)
    budget = budget if budget is not None else obs.Budget()
    full = copy.deepcopy(stub)
    full["evidence"] = _collect_ref(root, project_id, "evidence", stub["evidence"], budget)
    if "corrections" in stub:
        full["corrections"] = _collect_ref(root, project_id, "correction",
                                           stub["corrections"], budget)
    if "ac_map" in stub:
        # The map is bound to the task acceptance identities it was minted for:
        # a task change makes the bound record invalid rather than silently
        # reused, and the resolved state carries the map in its v4 location.
        records = _collect_ref(root, project_id, "ac-map", stub["ac_map"], budget)
        _require(len(records) == 1, "the acceptance map collection must hold one record")
        record = records[0]
        _require(isinstance(record, dict)
                 and set(record) == {"id", "tasks_digest", "map"},
                 "the acceptance map record is invalid")
        if record["tasks_digest"] != p.digest(stub["tasks"]):
            raise RelayError(RELAY_AC_MAP_MISMATCH,
                             "the acceptance map is not bound to these acceptance conditions")
        if record["map"] != p.build_ac_map(stub["tasks"]):
            raise RelayError(RELAY_AC_MAP_MISMATCH,
                             "the acceptance map disagrees with the task acceptance conditions")
        full["extensions"]["ac_map"] = record["map"]
        full.pop("ac_map", None)
    if "markdown" in stub:
        # Validated here so a deep read covers the externalised text; the
        # logical state deliberately keeps the v4 shape and the text is
        # reconstructed by resolve_markdown().
        records = _collect_ref(root, project_id, "markdown", stub["markdown"], budget)
        p.validate_markdown(records)
        provenance = stub.get("extensions", {})
        provenance = (provenance.get("external_markdown")
                      if isinstance(provenance, dict) else None)
        if isinstance(provenance, dict) \
                and obs.is_hex64(provenance.get("text_sha256") or ""):
            # The document declares the digest of the whole externalised text;
            # the bound objects must still reproduce it.
            if provenance["text_sha256"] != p.markdown_text_digest(records):
                raise RelayError(
                    RELAY_MARKDOWN_DIGEST_MISMATCH,
                    "the externalised Markdown does not match the document digest")
        full.pop("markdown", None)
    p.validate(full, make_target_resolver(root, project_id, stub, budget))
    return full


def collect_markdown(stub, project_root, project_id, budget=None):
    """The ordered external Markdown records of a v5 document."""
    _require(isinstance(stub, dict) and isinstance(stub.get("markdown"), dict),
             "this document does not externalise its Markdown")
    root = fs.root_path(project_root)
    budget = budget if budget is not None else obs.Budget()
    records = _collect_ref(root, project_id, "markdown", stub["markdown"], budget)
    p.validate_markdown(records)
    return records


def resolve_markdown(document_text, project_root=None):
    """The exact custom Markdown text of a document, v4 or v5.

    For v5 the text is reconstructed byte for byte from the bound objects; for
    v4 and earlier it is the unmanaged region of the document itself.
    """
    meta, body = parse_envelope(document_text)
    if meta["schema"] != p.SCHEMA_V5:
        return p.custom_region(body)
    if project_root is None:
        raise RelayError(RELAY_SCHEMA_V4_REQUIRES_RESOLVER,
                         "a v5 document requires an explicit project root")
    _meta, stub, _matches = envelope_state(document_text)
    if not isinstance(stub.get("markdown"), dict):
        return ""
    records = collect_markdown(stub, project_root, meta["project_id"])
    return p.join_markdown(records)


def _collect_ref(root, project_id, record_type, ref, budget):
    p._require_index_ref(ref, record_type)
    records = obs.collect(root, project_id, record_type, ref["index"], budget)
    if len(records) != ref["count"]:
        raise RelayError(RELAY_INDEX_INVALID,
                         record_type + " index count disagrees with the document")
    if p.digest(records) != ref["sha256"]:
        raise RelayError(RELAY_INDEX_INVALID,
                         record_type + " index content disagrees with the document")
    return records


# Schemas whose committed state depends on the external object store.  Every
# document in one of these schemas is deep-checked; every other schema reports
# the check as not applicable rather than as a pass.
EXTERNAL_SCHEMAS = tuple(s for s in (p.SCHEMA_V4, getattr(p, "SCHEMA_V5", None)) if s)

OBJECT_CHECK_SCOPE = "every reachable evidence, correction and markdown object"
OBJECT_CHECK_LIMITS = ("external_references", "seal_bytes", "test_suites",
                       "provider_calls", "network")


def object_integrity(root, project_id, stub, schema, budget=None):
    """Read every reachable object and verify its identity (an explicit deep check).

    The outcome vocabulary is deliberately narrow and never optimistic:

      * verified        — every reachable object and index node was read, its
                          bytes matched its digest, and its type, schema,
                          project id and index binding agreed;
      * degraded        — a named corruption (missing, tampered, wrong type,
                          cross-project, cyclic, invalid index, or an
                          acceptance map that no longer matches the tasks);
      * budget_exceeded — the check did not finish: INCOMPLETE, never a pass;
      * not_applicable  — a schema with no object store: never a pass either.

    The check is only ever executed when a caller explicitly asks for it.
    """
    if schema not in EXTERNAL_SCHEMAS or not isinstance(stub, dict) \
            or not p.is_external(stub):
        return {"checked": False, "state": "not_applicable",
                "reason": "integrity_not_applicable_to_schema",
                "applicable": False, "objects_checked": 0, "verified": False,
                "schema": schema, "scope": OBJECT_CHECK_SCOPE,
                "does_not_cover": list(OBJECT_CHECK_LIMITS)}
    budget = budget if budget is not None else obs.Budget()
    start = budget.files
    common = {"checked": True, "applicable": True, "schema": schema,
              "scope": OBJECT_CHECK_SCOPE, "does_not_cover": list(OBJECT_CHECK_LIMITS)}
    try:
        expand(stub, root, project_id, budget)
        reachable_manifest_digests(root, project_id, stub, budget)
        declared = stub.get("extensions", {})
        declared = declared.get("ac_map") if isinstance(declared, dict) else None
        if declared is not None and declared != p.build_ac_map(stub["tasks"]):
            raise RelayError(RELAY_AC_MAP_MISMATCH,
                             "the acceptance map disagrees with the task acceptance conditions")
    except RelayError as exc:
        state = ("budget_exceeded" if exc.code == RELAY_VALIDATION_BUDGET_EXCEEDED
                 else "degraded")
        return dict(common, state=state, reason=exc.code,
                    objects_checked=budget.files - start, verified=False)
    except (p.Invalid, fs.Error, OSError, UnicodeError, ValueError, TypeError, KeyError):
        return dict(common, state="degraded", reason=RELAY_OBJECT_SCHEMA_INVALID,
                    objects_checked=budget.files - start, verified=False)
    return dict(common, state="verified", reason=None,
                objects_checked=budget.files - start, verified=True,
                index=stub["evidence"].get("index"),
                count=stub["evidence"].get("count"))


def plan_full(state, root, project_id, targets=None, markdown=None, source_revision=None):
    """Return (stub_state, publish_plans); never touches the filesystem.

    Passing the custom Markdown records selects the v5 representation: the text
    and the acceptance map become external collections bound by an index
    reference, and the document keeps only the compact stub.
    """
    p.validate(state, targets)
    _require(not p.is_external(state), "v4 planning requires resolved records")
    plans, texts, evidence_entries = [], [], []
    for record in state["evidence"]:
        entry, part = obs.entry_for(root, project_id, "evidence", record)
        evidence_entries.append(entry)
        plans.extend(part)
        texts.append(obs.envelope_text(project_id, "evidence", record))
    index_plans, index_sha = obs.plan_index(root, project_id, "evidence", evidence_entries)
    plans.extend(index_plans)
    corrections = list(state.get("corrections", []))
    correction_entries = []
    for record in corrections:
        entry, part = obs.entry_for(root, project_id, "correction", record)
        correction_entries.append(entry)
        plans.extend(part)
        texts.append(obs.envelope_text(project_id, "correction", record))
    correction_plans, correction_sha = obs.plan_index(root, project_id, "correction",
                                                      correction_entries)
    plans.extend(correction_plans)
    for plan in plans:
        if plan["kind"] in ("index", "manifest"):
            texts.append(plan["content"].decode("utf-8"))
    stub = copy.deepcopy(state)
    stub["evidence"] = p.index_ref(index_sha, len(evidence_entries),
                                   p.digest(list(state["evidence"])))
    stub["corrections"] = p.index_ref(correction_sha, len(correction_entries),
                                      p.digest(corrections))
    if markdown is None:
        stub["extensions"]["ac_map"] = p.build_ac_map(stub["tasks"])
    else:
        p.validate_markdown(markdown)
        markdown_entries = []
        for record in markdown:
            entry, part = obs.entry_for(root, project_id, "markdown", record)
            markdown_entries.append(entry)
            plans.extend(part)
            texts.append(obs.envelope_text(project_id, "markdown", record))
        markdown_plans, markdown_sha = obs.plan_index(root, project_id, "markdown",
                                                      markdown_entries)
        plans.extend(markdown_plans)
        stub["markdown"] = p.index_ref(markdown_sha, len(markdown_entries),
                                       p.digest(list(markdown)))
        # One immutable record per acceptance identity, bound to the digest of
        # the acceptance conditions it was minted from: rebinding the map to a
        # changed task set publishes a new object instead of reusing a stale one.
        tasks_digest = p.digest(stub["tasks"])
        map_record = {"id": "ac-map-" + tasks_digest[:16], "tasks_digest": tasks_digest,
                      "map": p.build_ac_map(stub["tasks"])}
        map_entry, map_plans = obs.entry_for(root, project_id, "ac-map", map_record)
        plans.extend(map_plans)
        texts.append(obs.envelope_text(project_id, "ac-map", map_record))
        map_index_plans, map_index_sha = obs.plan_index(root, project_id, "ac-map",
                                                        [map_entry])
        plans.extend(map_index_plans)
        stub["ac_map"] = p.index_ref(map_index_sha, 1, p.digest([map_record]))
        # The map moves from extensions.ac_map to the external collection: a
        # document may never carry both, so the inline copy is removed here.
        stub["extensions"].pop("ac_map", None)
        provenance = stub["extensions"].get("external_markdown")
        if not isinstance(provenance, dict) \
                or provenance.get("schema") != p.EXTERNAL_MARKDOWN_SCHEMA:
            provenance = p.external_markdown_metadata(
                source_revision if source_revision is not None else 0, markdown)
        else:
            # The provenance keeps the revision the text was first
            # externalised from, but every derived value is recomputed: a
            # section added or replaced in this commit changes the corpus and
            # therefore its declared digest.
            provenance = dict(provenance)
        provenance["sections"] = len(markdown)
        provenance["bytes"] = sum(record["bytes"] for record in markdown)
        provenance["text_sha256"] = p.markdown_text_digest(markdown)
        stub["extensions"]["external_markdown"] = provenance
    p.validate(stub)
    return stub, plans, texts


def classify(error):
    """Map a RelayError to the status vocabulary (never to PASS)."""
    if error.code == "RELAY_VALIDATION_BUDGET_EXCEEDED":
        return "budget_exceeded"
    return "degraded"


def reachable_manifest_digests(root, project_id, stub, budget=None):
    """Manifest digests reachable from the committed indexes of one document."""
    budget = budget if budget is not None else obs.Budget()
    out = set()
    for field, record_type in (("evidence", "evidence"), ("corrections", "correction")):
        ref = stub.get(field) if isinstance(stub, dict) else None
        if isinstance(ref, dict) and ref.get("index") is not None:
            out.update(obs.chunked_manifest_digests(root, project_id, record_type,
                                                    ref["index"], budget))
    return out


def verify_manifest_target(root, project_id, sha, reachable, budget=None):
    """Verify one object-store chunk manifest before it is bound.

    Steps: an explicit 64-hex digest; an explicit project root and record-type
    namespace (never a search path, a symlink or a cross-project lookup); a real
    object whose bytes hash to the claimed digest; the correct schema and type;
    a matching project id; and membership of the traceable range handed in by
    the caller.  The returned digest is derived from the resolved bytes, so a
    caller-supplied hash is never echoed back as if it had been verified.
    """
    obs.require_hex64(sha, RELAY_OBJECT_TYPE_INVALID, "manifest digest")
    root = fs.root_path(root)
    if sha not in set(reachable):
        raise RelayError(RELAY_CORRECTION_TARGET_UNREACHABLE,
                         "manifest target is not reachable from a committed index")
    node = obs.read_manifest_object(root, project_id, sha,
                                    budget if budget is not None else obs.Budget())
    return p.digest({"schema": node["schema"], "project_id": node["project_id"],
                     "record_type": node["record_type"],
                     "object_sha256": node["object_sha256"],
                     "object_bytes": node["object_bytes"], "count": node["count"],
                     "manifest_sha256": sha})


def make_target_resolver(root, project_id, stub, budget=None):
    """Root-bound resolver for correction targets whose bytes live on disk."""
    root = fs.root_path(root)
    cache = {}

    def resolve(record):
        if record.get("target_type") != "chunk_manifest":
            raise RelayError(RELAY_CORRECTION_TARGET_UNSUPPORTED,
                             "this correction target type has no owned namespace")
        if "reachable" not in cache:
            cache["reachable"] = reachable_manifest_digests(root, project_id, stub, budget)
        return verify_manifest_target(root, project_id, record.get("target_id"),
                                      cache["reachable"], budget)

    return resolve


CHECK_ORDER = ("coverage", "baseline", "mapping", "integrity", "external")

# A handoff may only claim verification when these checks passed.  Integrity and
# external checks are reported separately: they are never inferred from a
# matching Git identity or from a successful unrelated check.
MANDATORY_CHECKS = ("coverage", "baseline", "mapping")


def canonical(value) -> str:
    """The single canonical JSON encoding used for every derived digest."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)

def _list_page(limit, offset, total, items, identity=None):
    page = _page(limit, offset, total)
    page["cursor"] = None
    page["next_cursor"] = None
    page["next_offset"] = (offset + len(items)) if offset + len(items) < total else None
    page["identity_sha256"] = p.digest(identity) if identity is not None else None
    page["page_items"] = len(items)
    return page

def _acceptance_page(total, offset, limit, cursor, identity=None, reachable=None):
    end = offset + limit
    return {"offset": offset, "limit": limit, "total": total,
            "has_more": end < total, "cursor": cursor,
            "next_cursor": None, "next_offset": end if end < total else None,
            "identity_sha256": p.digest(identity) if identity is not None else None,
            "page_items": min(limit, max(0, total - offset))}

def _gap(row, task):
    reasons = []
    for item in row["withdrawn"]:
        for reason in item.get("reasons") or [item.get("reason")]:
            if reason and reason not in reasons:
                reasons.append(reason)
    if not reasons:
        reasons = ["no_current_pass"]
    return {"task_id": task["id"], "ac_id": row["ac_id"], "text": row["text"],
            "current_reason": reasons[0], "current_reasons": reasons,
            "withdrawn": row["withdrawn"],
            "withdrawn_total": row["withdrawn_page"]["total"],
            "recorded_contributors": row["recorded_contributors"],
            "covered": False}

def _baseline_block(meta, baseline):
    recorded = {"state": "recorded" if baseline is not None and meta.get("git_baseline") else
                "unchecked",
                "source": "git_baseline" if meta.get("git_baseline") else "absent",
                "kind": "git" if meta.get("git_baseline") else "legacy",
                "branch": meta.get("branch"), "head": meta.get("base_commit"),
                "fingerprint": None}
    observed = {"state": "ok" if baseline is not None else "unavailable",
                "kind": "git" if baseline is not None else "none",
                "branch": baseline.get("branch") if isinstance(baseline, dict) else None,
                "head": baseline.get("head") if isinstance(baseline, dict) else None,
                "fingerprint": baseline.get("fingerprint") if isinstance(baseline, dict) else None}
    return {"recorded": recorded, "observed": observed}

def _coverage_check(rows):
    if not rows:
        return {"checked": True, "state": "not_applicable", "reason": "no_acceptance_conditions",
                "covered": 0, "uncovered": 0, "applicable": False, "verified": True}
    uncovered = [row for row in rows if not row["covered"]]
    return {"checked": True, "state": "covered" if not uncovered else "uncovered",
            "reason": None if not uncovered else "uncovered_acceptance",
            "covered": len(rows) - len(uncovered), "uncovered": len(uncovered),
            "applicable": True, "verified": not uncovered}

def _baseline_check(rows, block=None):
    """L4 as one named check; the observed environment is never re-derived.

    When the caller supplies the baseline block it was captured from the real
    environment, so its comparison outcome is authoritative: a stale or
    unchecked environment is reported as such even if every contributor record
    happens to carry a baseline that equals the recorded one.
    """
    if isinstance(block, dict) and isinstance(block.get("check"), dict):
        check = block["check"]
        if check.get("state") != "pass" or not check.get("verified"):
            return {"checked": bool(check.get("checked")), "state": check.get("state"),
                    "reason": check.get("reason"), "applicable": True,
                    "verified": False, "scope": check.get("scope"),
                    "does_not_cover": check.get("does_not_cover")}
    contributors = [row for row in rows if row["contributors"]]
    if not contributors:
        return {"checked": False, "state": "not_applicable", "reason": "no_effective_pass",
                "applicable": False, "verified": False}
    stale = sorted({row["baseline_reason"] for row in contributors
                    if not row["baseline_verified"]})
    verified = all(row["baseline_verified"] for row in contributors)
    if verified:
        return {"checked": True, "state": "verified", "reason": None,
                "applicable": True, "verified": True}
    mismatch = "baseline_mismatch" in stale
    state = "stale" if mismatch else "not_checked"
    reason = "baseline_mismatch" if mismatch else (stale[0] if stale else "baseline_not_checked")
    return {"checked": True, "state": state, "reason": reason,
            "applicable": True, "verified": False, "reasons": stale}

def _mapping_check(state):
    migration = state["extensions"].get("migration") or {}
    pending = bool(isinstance(migration, dict) and migration.get("mapping_review_required"))
    return {"checked": True, "state": "review_required" if pending else "not_required",
            "reason": "mapping_review_required" if pending else None,
            "applicable": True, "verified": not pending}

def _integrity_check(integrity):
    if not isinstance(integrity, dict):
        return {"checked": False, "state": "not_checked", "reason": "integrity_not_checked",
                "applicable": True, "verified": False}
    state = integrity.get("state")
    return {"checked": bool(integrity.get("checked")), "state": state,
            "reason": integrity.get("reason"),
            "applicable": bool(integrity.get("applicable", True)),
            "objects_checked": integrity.get("objects_checked"),
            "verified": state == "verified"}

def _external_check(flags):
    acknowledged = bool((flags or {}).get("acknowledge_external"))
    return {"checked": acknowledged,
            "state": "acknowledged" if acknowledged else "not_checked",
            "reason": None if acknowledged else "external_checks_not_executed",
            "applicable": False, "verified": acknowledged, "scope": "caller_declared",
            "not_run": ["external_references", "seal_bytes", "test_suites",
                        "provider_calls", "network_fetches"]}

def _verification_checks(state, view, rows, baseline, integrity, flags, block=None):
    return {"coverage": _coverage_check(rows),
            "baseline": _baseline_check(rows, block),
            "mapping": _mapping_check(state),
            "integrity": _integrity_check(integrity),
            "external": _external_check(flags)}

def _check_state(check):
    return check.get("state") if isinstance(check, dict) else check

def _verification_record(checks, complete=True):
    """Verification strength, derived from explicit checks only."""
    # A check blocks verification only when it is applicable and did not pass.
    # "covered" and "not_required" are passing outcomes: their names describe the
    # result, not a failure.  The explicit verified flag on each check is the one
    # source of that judgement.
    required = list(MANDATORY_CHECKS)
    integrity = checks.get("integrity")
    if isinstance(integrity, dict) and integrity.get("checked") \
            and integrity.get("applicable", True):
        # A deep check that was explicitly requested is part of the required set
        # for this conclusion: degrading or exhausting it can never be absorbed
        # into a verified handoff.
        required.append("integrity")
    required.sort(key=lambda name: CHECK_ORDER.index(name) if name in CHECK_ORDER else 99)
    failures = [(name, check) for name, check in checks.items()
                if name in required and check.get("applicable", True)
                and not check.get("verified")
                and _check_state(check) != "not_required"]
    failures.sort(key=lambda item: CHECK_ORDER.index(item[0])
                  if item[0] in CHECK_ORDER else 99)
    not_checked = sorted(name for name, check in checks.items()
                         if _check_state(check) == "not_checked")
    if not complete:
        verified, reason = False, "pagination_incomplete"
    elif failures:
        name, check = failures[0]
        reason = check.get("reason") or (name + "_" + str(_check_state(check)))
        verified = False
    else:
        verified, reason = True, None
    scope = {"restricted_to": list(CHECK_ORDER),
             "external_checks_executed": bool(checks["external"].get("checked")),
             "integrity_check_executed": bool(checks["integrity"].get("checked")),
             "conclusion": ("verified for the requested scope"
                            + ("; not asserted: " + ", ".join(not_checked)
                               if not_checked else "")
                            if verified else "not verified: " + str(reason))}
    return {"schema": "project-continuity/verification-record/v1", "verified": verified,
            "reason": reason, "complete": complete, "checks": checks,
            "required": required, "not_checked": not_checked,
            "scope": scope, "basis": "read_only_local"}

def _describe(row, position):
    reasons = []
    for item in row["withdrawn"]:
        for reason in item.get("reasons") or [item.get("reason")]:
            if reason and reason not in reasons:
                reasons.append(reason)
    return {"title": row["text"], "index": position, "ac_id": row["ac_id"],
            "task_id": row["task_id"], "covered": row["covered"],
            "recorded_covered": row["recorded_covered"],
            "baseline_verified": row["baseline_verified"],
            "baseline_reason": row["baseline_reason"],
            "verified": row["verified"], "contributors": row["contributors"],
            "recorded_contributors": row["recorded_contributors"],
            "withdrawn": row["withdrawn"], "withdrawn_reasons": reasons,
            "withdrawn_page": row["withdrawn_page"]}

def cursor_identity(source, task_filter, cursor=None):
    """One logical identity: revision, content hash, schema and filter.

    The values that describe the *data* are what a cursor is bound to; the
    cursor text itself is deliberately excluded so that a page cannot mint a
    new identity the next page would reject.
    """
    return {"revision": int(source["revision"]), "current_sha256": source["current_sha256"],
            "schema_version": source["schema_version"], "task_filter": task_filter}

def cursor_expectation(raw):
    """Accept only a well-formed cursor position or token."""
    if not isinstance(raw, str):
        return False, "acceptance cursor is not a string"
    if raw.startswith(CURSOR_PREFIX):
        parse_cursor(raw)
        return True, None
    parse_acceptance_cursor(raw)
    return True, None

def check_page_cursor(raw, current_identity, revision, expected=None):
    """Refuse an opaque cursor that was not minted for this document page.

    A readable ``task:offset`` position is a request for that position and is
    always acceptable; an opaque token asserts that the document identity is
    unchanged, and is refused by name otherwise.  Pages of different revisions
    are never concatenated.
    """
    if not isinstance(raw, str) or not raw.startswith(CURSOR_PREFIX):
        return raw
    payload = parse_cursor(raw)
    if int(payload["revision"]) != int(revision) \
            or payload["identity_sha256"] != p.digest(current_identity) \
            or (expected is not None and payload["acceptance_cursor"] != expected):
        # One named refusal: a token minted for another page, revision or task is
        # never treated as a page of this document, and never echoed back.
        raise RelayError(RELAY_PAGE_CURSOR_STALE,
                         "acceptance cursor is stale for this document")
    return payload["acceptance_cursor"]

def check_cursor(raw, current_identity):
    """Refuse a cursor minted for another revision, hash, schema, filter or task.

    Two forms are accepted: the opaque token minted by mint_cursor and the
    readable task:offset position.  A readable position is only accepted while
    the document identity that supplies its meaning is unchanged, so two
    revisions can never be concatenated into one conclusion.
    """
    if isinstance(raw, str) and not raw.startswith(CURSOR_PREFIX):
        task_id, _offset = parse_acceptance_cursor(raw)
        _require(task_id == current_identity["task_filter"],
                 "acceptance cursor belongs to a different task filter")
        return raw
    payload = parse_cursor(raw)
    _require(payload["identity_sha256"] == p.digest(current_identity),
             "acceptance cursor belongs to a different document identity")
    _require(payload["revision"] == current_identity["revision"],
             "acceptance cursor belongs to a different revision")
    return payload["acceptance_cursor"]

def parse_acceptance_cursor(raw):
    """Parse ``<task_id>:<offset>`` into (task_id, offset)."""
    if raw is None:
        return None, None
    _require(isinstance(raw, str) and ":" in raw, "invalid acceptance cursor")
    task_id, _, offset = raw.rpartition(":")
    _require(task_id != "" and offset.isdigit(), "invalid acceptance cursor")
    _require(int(offset) <= 1000000, "acceptance cursor offset is out of range")
    p.identifier(task_id)
    return task_id, int(offset)

def verify_scope(complete):
    """Why a handoff is or is not a complete view, in named terms."""
    if complete:
        return "complete"
    return "page_only: at least one list still holds unreturned items"

def mint_cursor(identity, task_id, offset):
    """Mint an opaque, self-describing cursor bound to one document identity."""
    _require(task_id is not None, "an acceptance cursor requires an addressed task")
    payload = {"revision": identity["revision"],
               "acceptance_cursor": task_id + ":" + str(offset),
               "identity_sha256": p.digest(identity)}
    return CURSOR_PREFIX + base64.urlsafe_b64encode(
        canonical(payload).encode("utf-8")).decode("ascii")

def parse_cursor(raw):
    """Decode one cursor; a malformed or foreign cursor is refused by name."""
    _require(isinstance(raw, str) and raw.startswith(CURSOR_PREFIX),
             "invalid acceptance cursor")
    body = raw[len(CURSOR_PREFIX):]
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RelayError(RELAY_PAGE_CURSOR_STALE, "acceptance cursor is not decodable") from exc
    _require(isinstance(payload, dict) and set(payload) == {
        "revision", "acceptance_cursor", "identity_sha256"},
        "acceptance cursor has invalid fields")
    return payload


def coverage(state, project_id, task_id=None, limit=20, baseline=None, current_sha256=None,
             cursor=None, integrity=None, flags=None, block=None, **kwargs):
    """evidence x acceptance-condition coverage from the shared judgement."""
    _require(not p.is_external(state), "coverage requires a resolved v4 state")
    limit = max(1, min(int(limit), 200))
    view = p.evidence_view(state, baseline)
    mapped = {}
    for entry in (state["extensions"].get("ac_map") or {}).get("entries", []):
        mapped.setdefault(entry["task_id"], set()).add(entry["ac_id"])
    base_identity = cursor_identity({"revision": kwargs.get("revision", 0),
                                     "current_sha256": current_sha256,
                                     "schema_version": kwargs.get("schema_version",
                                                                  COVERAGE_SCHEMA)},
                                    task_id)
    addressed = check_cursor(cursor, base_identity) if cursor is not None else None
    cursor_task, cursor_offset = parse_acceptance_cursor(addressed)
    if cursor_task is not None:
        _require(task_id is None or task_id == cursor_task,
                 "acceptance cursor conflicts with the task filter")
        task_id = cursor_task
    selected = [task for task in state["tasks"] if task_id is None or task["id"] == task_id]
    if cursor_task is not None:
        _require(any(task["id"] == cursor_task for task in selected),
                 "acceptance cursor task is missing")
    rows, span = [], 0
    for task in selected:
        task_rows = p.acceptance_coverage(state, task, view=view, limit=limit)
        span += len(task_rows)
        start = cursor_offset if cursor_task is not None else 0
        for position, row in enumerate(task_rows[start:start + limit]):
            covering = [item for item in view["records"]
                        if item["task_id"] == row["task_id"]
                        and row["text"] in item["acceptance"]]
            row = dict(row)
            row.update({
                "persisted": row["ac_id"] in mapped.get(row["task_id"], set()),
                "position": start + position,
                "evidence": [{"id": item["id"], "result": item["recorded_result"],
                              "recorded_result": item["recorded_result"],
                              "generation": item["generation"],
                              "generation_match": item["generation_match"],
                              "current": item["current"],
                              "superseded_by": item["superseded_by"],
                              "current_reasons": item.get("current_reasons", []),
                              "contributes": item["contributes"], "reason": item["reason"],
                              "baseline": item["baseline"],
                              "baseline_reason": item["baseline_reason"]}
                             for item in covering[:limit]],
                "evidence_total": len(covering), "truncated": len(covering) > limit,
                "evidence_page": _page(limit, 0, len(covering))})
            rows.append(row)
    offset = cursor_offset if cursor_task is not None else 0
    identity = dict(base_identity, task_filter=task_id)
    next_cursor = (mint_cursor(identity, cursor_task, offset + len(rows))
                   if cursor_task is not None and offset + len(rows) < span else None)
    checks = _verification_checks(state, view, rows, baseline, integrity, flags, block)
    record = _verification_record(checks, complete=True)
    return {"schema": COVERAGE_SCHEMA, "project_id": project_id, "task_filter": task_id,
            "content_sha256": current_sha256,
            "baseline_source": "captured" if baseline is not None else "not_checked",
            "evidence_counts": view["counts"],
            "pagination_identity": identity,
            "pagination": {"offset": offset, "limit": limit, "total": span,
                           "has_more": offset + len(rows) < span,
                           "cursor": addressed,
                           "next_offset": (offset + len(rows) if offset + len(rows) < span
                                           else None),
                           "next_cursor": next_cursor,
                           "identity_sha256": p.digest(identity)},
            "summary": {"rows": len(rows), "total_rows": span,
                        "covered": sum(1 for row in rows if row["covered"]),
                        "uncovered": sum(1 for row in rows if not row["covered"]),
                        "baseline_verified": sum(1 for row in rows if row["baseline_verified"]),
                        "withdrawn": sum(row["withdrawn_page"]["total"] for row in rows)},
            "verification": "verified" if record["verified"] else "unverified",
            "verification_record": record,
            "checks": record["checks"],
            "rows": rows, "count": len(rows)}


def handoff(state, meta, project_id, current_sha256, task_id=None, limit=25, offset=0,
            baseline=None, uncovered_offset=None, uncovered_limit=None, cursor=None,
            tasks_offset=None, evidence_offset=None, blockers_offset=None,
            evidence_limit=None, blockers_limit=None, integrity=None, flags=None,
            block=None, **kwargs):
    """Read-only derived handoff.  Its content is data, never an instruction."""
    _require(not p.is_external(state), "handoff requires a resolved v4 state")
    limit = max(1, min(int(limit), 200))
    # The bare offset addresses acceptance conditions only.  Every top-level list
    # has its own offset, so paging acceptance can never move a task out of the
    # result and make a truncated handoff look complete.
    offset = max(0, int(offset))
    tasks_offset = max(0, int(tasks_offset if tasks_offset is not None else 0))
    evidence_offset = max(0, int(evidence_offset if evidence_offset is not None else 0))
    blockers_offset = max(0, int(blockers_offset if blockers_offset is not None else 0))
    uncovered_offset = max(0, int(uncovered_offset if uncovered_offset is not None else offset))
    uncovered_limit = max(1, min(int(uncovered_limit if uncovered_limit is not None else limit),
                                 200))
    evidence_limit = max(1, min(int(evidence_limit if evidence_limit is not None else limit), 200))
    blockers_limit = max(1, min(int(blockers_limit if blockers_limit is not None else limit), 200))
    # A readable cursor names the task it addresses, so it also fixes the filter.
    readable_task = None
    if isinstance(cursor, str) and not cursor.startswith(CURSOR_PREFIX):
        readable_task, _position = parse_acceptance_cursor(cursor)
    base_identity = cursor_identity({"revision": meta["revision"],
                                     "current_sha256": current_sha256,
                                     "schema_version": meta["schema"]},
                                    task_id if task_id is not None else readable_task)
    addressed = check_cursor(cursor, base_identity) if cursor is not None else None
    view = p.evidence_view(state, baseline)
    cursor_task, cursor_offset = parse_acceptance_cursor(addressed)
    if cursor_task is not None:
        _require(task_id is None or task_id == cursor_task,
                 "acceptance cursor conflicts with the task filter")
        task_id = cursor_task
    selected = [task for task in state["tasks"] if task_id is None or task["id"] == task_id]
    if cursor_task is not None:
        _require(any(task["id"] == cursor_task for task in selected),
                 "acceptance cursor task is missing")
    acceptance_rows, tasks_all, uncovered_all, withdrawn_total = [], [], [], 0
    acceptance_span = 0
    returned_ids = []
    for task in selected:
        rows = p.acceptance_coverage(state, task, view=view, limit=limit)
        acceptance_span += len(rows)
        acceptance_rows.extend(rows)
        start = cursor_offset if cursor_task is not None else 0
        window = rows[start:start + limit]
        gaps = [row for row in rows if not row["covered"]]
        withdrawn_total += sum(row["withdrawn_page"]["total"] for row in rows)
        # Only gaps of the tasks actually returned on this page are reported: a
        # task that the caller cannot see yet is a pagination fact, not a gap of
        # this page.
        returned_ids.append(task["id"])
        uncovered_all.extend(_gap(row, task) for row in gaps)
        described = [_describe(row, start + position) for position, row in enumerate(window)]
        end = start + len(window)
        page = {"offset": start, "limit": limit, "total": len(rows),
                "has_more": end < len(rows), "cursor": addressed,
                "next_cursor": (mint_cursor(dict(base_identity, task_filter=task["id"]),
                                             task["id"], end) if end < len(rows) else None),
                "next_offset": end if end < len(rows) else None}
        tasks_all.append({"id": task["id"], "title": task["title"], "status": task["status"],
                          "generation": task["generation"],
                          "acceptance": [item["title"] for item in described],
                          "acceptance_items": described,
                          "acceptance_total": len(rows),
                          "acceptance_page": page,
                          "acceptance_covered": sum(1 for row in rows if row["covered"]),
                          "uncovered": [row["ac_id"] for row in gaps]})
    blockers_all = [{"id": blocker["id"], "task_id": blocker["task_id"],
                     "description": blocker["description"]}
                    for blocker in state["blockers"] if blocker["status"] == "open"
                    and (task_id is None or blocker["task_id"] == task_id)]
    evidence_all = [item for item in view["records"]
                    if task_id is None or item["task_id"] == task_id]
    tasks_page = tasks_all[tasks_offset:tasks_offset + limit]
    blockers_page = blockers_all[blockers_offset:blockers_offset + blockers_limit]
    uncovered_page = uncovered_all[uncovered_offset:uncovered_offset + uncovered_limit]
    evidence_page = evidence_all[evidence_offset:evidence_offset + evidence_limit]
    identity = dict(base_identity, task_filter=task_id)
    acceptance_page = _acceptance_page(acceptance_span, cursor_offset or 0, limit, addressed, identity)
    pages = {"tasks": _list_page(limit, tasks_offset, len(tasks_all), tasks_page, identity),
             "blockers": _list_page(blockers_limit, blockers_offset, len(blockers_all),
                                    blockers_page, identity),
             "uncovered_acceptance": _list_page(uncovered_limit, uncovered_offset,
                                                len(uncovered_all), uncovered_page, identity),
             "evidence": _list_page(evidence_limit, evidence_offset, len(evidence_all),
                                    evidence_page, identity),
             "acceptance": acceptance_page}
    complete = not any(page["has_more"] for page in pages.values())
    for page in pages.values():
        page.pop("page_items", None)
    checks = _verification_checks(state, view, acceptance_rows, baseline, integrity, flags, block)
    record = _verification_record(checks, complete=complete)
    covered_rows = sum(task["acceptance_covered"] for task in tasks_all)
    total_rows = sum(task["acceptance_total"] for task in tasks_all)
    next_cursor = None
    if cursor_task is not None and cursor_offset + len(acceptance_rows) < acceptance_span:
        next_cursor = mint_cursor(dict(base_identity, task_filter=cursor_task), cursor_task,
                                  cursor_offset + len(acceptance_rows))
    return {
        "schema": HANDOFF_SCHEMA,
        "content_is_data": True,
        "identity": identity,
        "revision": int(meta["revision"]),
        "schema_version": meta["schema"],
        "project_id": project_id,
        "current_sha256": current_sha256,
        "target": {"status": state["project"]["status"],
                   "current_task": state["project"]["current_task"],
                   "next_step": state["project"]["next_step"]},
        "source_identity": {"branch": meta.get("branch"),
                            "base_commit": meta.get("base_commit"),
                            "git_baseline": meta.get("git_baseline"),
                            "writer": meta.get("writer"),
                            "lease_until": meta.get("lease_until")},
        "tasks": tasks_page,
        "blockers": blockers_page,
        "acceptance": acceptance_page,
        "unverified": {
            "mapping_review_required": bool(
                (state["extensions"].get("migration") or {}).get("mapping_review_required")),
            "uncovered_acceptance": uncovered_page,
            "uncovered_page": pages["uncovered_acceptance"]},
        "coverage": {"acceptance_rows": total_rows, "covered": covered_rows,
                     "uncovered": len(uncovered_all), "withdrawn": withdrawn_total},
        "baseline": {"supplied": baseline is not None, "current": baseline,
                     "counts": {name: view["counts"].get(name, 0)
                                for name in ("effective", "revoked", "superseded")}},
        "acceptance_cursor": addressed,
        "next_cursor": next_cursor,
        "pages": pages,
        "page_complete": complete,
        "complete": complete,
        "complete_scope": verify_scope(complete),
        "verification": ("incomplete" if not complete else
                         "verified" if record["verified"] else "unverified"),
        "verification_record": record,
        "checks": record["checks"],
        "next_steps": [state["project"]["next_step"]],
        "permissions": {"write_entry_point": "scripts/write_current.py",
                        "network_calls": False, "deletes": False,
                        "second_authority": False},
        "evidence": evidence_page,
        "evidence_page": pages["evidence"],
    }
