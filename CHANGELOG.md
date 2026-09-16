# Changelog

## 0.3.3 - 2026-09-16

Second review follow-up: the publication-window repair is live code, the write
path waits on a real deadline, and an assembly gate makes the duplicate-
definition class of defect impossible to ship again.

- Make the read-side publication window real.  `scripts/storage.py` defined
  `read_bytes` twice; the later, window-unaware definition shadowed the repaired
  implementation, so reading a file that another writer was mid-publication on
  raised "exactly one link" instead of waiting.  The duplicate is gone and the
  surviving `read_bytes(path, max_bytes=65536, temporary_pattern=None)` waits out
  a live window inside `PUBLISH_WAIT_SECONDS` while re-validating the identity on
  every attempt.  A window that never closes (a crash that left the temporary
  link behind), a foreign hard link, a mismatched temporary name, a different
  inode, a symlink or a mode mismatch is refused by name.  The window test is the
  same two-condition test on the read and the write path: same directory, a
  matching `.<name>.*.tmp` name and the same `(st_dev, st_ino)`; an unrelated
  hard link is never accepted, and the wait budget is never spent on one.
- Give the write path a real deadline.  `immutable_bytes` retried three times
  (about 6 ms) and `_immutable_attempt` rejected an existing target with two
  links outright, so a same-content publication racing another creator failed
  intermittently under load.  Retries now run until a `PUBLISH_WAIT_SECONDS`
  deadline while re-verifying the extra link each time: identical content stays
  idempotent, conflicting content is named, and a stuck window fails closed with
  `immutable publish did not settle within the publication window`.
- Gate assembly integrity.  `tests/test_assembly.py` fails the suite on any
  duplicate top-level definition in `scripts/`, on an assembly residue left
  inside `scripts/`, and on a `read_bytes` signature that lost the
  publication-window parameter.
- Ship the publication-window contract as product tests.  `tests/test_storage.py`
  covers the live window, the explicit temporary pattern, the permanent leftover,
  the foreign hard link without waiting, and the write-side idempotent and
  conflicting cases.
- Correct the delivery material.  The deployment manifest matches the diff file
  set exactly, the assembly residue is no longer part of the candidate, and the
  reproduction document reports the Red runs the frozen baseline really
  produced.

## 0.3.2 - unreleased candidate

Review follow-up: current-environment checks, resumable pagination, honest
capacity accounting, the complete relationship graph, and a publication race.

- Capture the current environment per command.  `coverage`, `handoff` and
  `status` perform one read-only Git capture and report the *recorded* baseline,
  the *observed* environment and the comparison as three separate fields.
  A failed, timed-out, non-repository or drifted environment is named
  (`current_baseline_*`, `baseline_mismatch`) and never falls back to the
  historical baseline to claim a current check.  Covered and verified stay
  orthogonal: a stale baseline does not erase coverage.
- Derive verification from explicit checks.  `handoff` and `coverage` publish a
  `verification-record/v1` with named coverage, baseline, mapping, integrity and
  external checks; applicability is explicit, a not-checked check is never
  silently treated as passing, and a matching Git identity is not evidence that
  an external reference, seal, test suite or provider call was re-verified.
- Make acceptance independently addressable.  `handoff --cursor TASK:OFFSET`
  (and `coverage --acceptance-cursor`) read one acceptance condition without
  moving its task out of the result; a bare `--offset` now addresses acceptance
  only, every list has its own offset, and an opaque token from another revision
  is refused by name (`RELAY_PAGE_CURSOR_STALE`).  This release bumps the
  handoff and coverage view schemas to v3 and adds `page_complete` so that
  "no more pages" and "a complete handoff" are separate statements.
- Count what growth actually publishes.  Class reports separate planned bytes
  from the bytes a publish would really create, corrections count their object
  and rewritten index nodes, the first record of a class counts index creation,
  and the save cycle is modelled as two consecutive commits (resume then save,
  including the lease release a save performs).  An empty project gets a named
  model instead of an exception.
- Validate every relationship edge.  Supersession edges are collected before any
  dictionary is built, so a cycle can no longer hide behind the last edge for a
  target; conflicting replacements are refused
  (`RELAY_CORRECTION_TARGET_CONFLICT`), and a target that is both revoked and
  superseded keeps both reasons.
- Treat an active publication window as a wait, not a failure.  A competing
  creator may briefly hold a second name for the same inode; that documented
  window is waited out with a bounded retry whose identity is re-verified on
  every attempt.  Foreign hard links, symlinks and unresolvable links are still
  refused exactly as before.
## 0.3.1 - 2026-09-16

Effective-evidence consistency, verified correction targets and capacity growth.

- Unify the effective-evidence judgement.  The completion gate, `coverage`,
  `handoff` and `status` now share one evaluator in `progress.evidence_view` /
  `progress.acceptance_coverage`, so a revoked or superseded pass can no longer
  be rejected by the gate while still reporting `covered` and an empty
  uncovered list.  Each record reports its recorded result, its current state
  (effective/revoked/superseded), generation match, baseline status and a named
  reason for not contributing.
- Key correction relationships by (record_type, record_id).  Revoking a decision
  no longer removes an evidence record that shares the same string id.  A
  correction is immutable, may annotate another correction, and may not revoke
  or supersede one.
- Verify manifest correction targets instead of accepting a 64-hex string.  The
  only owned namespace is the object-store chunk manifest (`chunk_manifest`),
  resolved under an explicit root with existence, byte hash, schema, type,
  project and reachability checks; the stored digest is derived from the
  resolved descriptor.  `manifest` and other ambiguous names are refused by
  name (RELAY_CORRECTION_TARGET_UNSUPPORTED / RELAY_CORRECTION_TARGET_UNREACHABLE),
  and root-bound targets are re-verified inside the write lock.
- Report recorded coverage, current coverage and a verified baseline as three
  separate columns; an unchecked baseline is named and never implied.  A stale
  baseline does not delete coverage.
- Page every handoff list (tasks, acceptance, blockers, uncovered acceptance,
  evidence) with totals and a cursor, bind them to one content identity, and set
  `complete=false` when any page is truncated.
- Always model the next commit's own operation receipt, writer and lease in
  `capacity`, and add a per-class growth report.  The measured preview and the
  following commit produce identical byte counts for identical arguments.
- coverage-matrix and handoff-view are bumped to v2; the per-evidence `current`
  field changes meaning from a generation boolean to a named validity state.

## 0.3.0 - 2026-09-16

Explicit, lossless project-continuity/v4 external evidence objects.

- Add the v4 wire format: CURRENT.md carries a content-addressed evidence index
  reference, the index binds ordered immutable object references, and the
  objects hold the unmodified records.  CURRENT.md commits at or below 32768
  bytes (half of the 64 KiB protocol ceiling), including the new operation,
  writer/lease and migration metadata.
- Add .relay/objects/ with sharded directories, exclusive creation, 0600
  files, file and directory fsync, non-overwriting publication, same-name
  content verification, and rejection of symlinks, traversal, cross-project
  and unknown object types.  Objects and chunks are capped at 262144 bytes;
  larger payloads become ordered chunks with a content-addressed parent
  manifest.  Every object read passes an explicit max_bytes.
- Add the public resolver: resolve(document_text, project_root) -> complete
  logical state.  A v4 document without an explicit root is
  RELAY_SCHEMA_V4_REQUIRES_RESOLVER; an envelope or a status summary is never
  a completed state.
- Named integrity semantics: RELAY_OBJECT_MISSING,
  RELAY_OBJECT_HASH_MISMATCH, RELAY_OBJECT_SCHEMA_INVALID,
  RELAY_OBJECT_PROJECT_MISMATCH, RELAY_INDEX_INVALID, RELAY_CHUNK_MISSING,
  RELAY_REFERENCE_CYCLE, RELAY_OBJECT_LIMIT_EXCEEDED and
  RELAY_VALIDATION_BUDGET_EXCEEDED (an incomplete check, never corruption and
  never a PASS).  Storage refusals are RELAY_STORAGE_QUOTA_EXCEEDED and
  RELAY_DISK_SPACE_INSUFFICIENT, raised before any CURRENT replacement.
- Add explicit "migrate --to-v4", stable acceptance-condition identifiers
  (extensions.ac_map), immutable correction/revocation/supersession records,
  scoped blockers (extensions.blocker_scope), read-only coverage, handoff and
  capacity views, and v4-aware export/verify bundles that carry every
  reachable object.
- Add migrate_compare: an independent logical-equivalence checker that locates
  records by type + id, compares canonical per-record digests, and allows only
  the schema, storage references, managed metadata, revision, time, lease and
  this operation's receipt to differ.
- v2 and v3 keep their existing write behaviour; v4 is reachable only through
  an explicit migration, and an old client refuses a v4 document with its own
  unsupported-schema error.

## 0.2.0 - 2026-09-08

Automatic, lossless receipt-capacity governance.

- Keep the newest 32 operation receipts inline and archive older receipts in a
  content-addressed `.relay/receipts/` chain when a write reaches 80% of 64 KiB.
- Add `compact` preview/apply, explicit `migrate --to-v3`, archive integrity
  reporting, archived-operation retry lookup, controlled `export`/`verify`
  handoff bundles, and `--no-auto-compact` escape hatch for a single write.
- Preserve fail-closed CAS, lease, Git-drift, atomic/history ordering, secret
  scanning, and the rule that project records are never truncated.
- Add capacity, archive, v3 migration, and package regression coverage.

- Require explicit v2-to-v3 migration; normal v2 writes never change protocol.
- Use one candidate planner, full-parameter read-only previews, and lossless
  compact managed rendering without changing custom Markdown.
- Verify handoff schema and exact reachable archives using one ZIP read.

Explicit protocol migration is required for existing projects. Publication
and disk installation do not imply live project migration or client hot reload.
See CI and release notes for platform and package verification evidence.

## 0.1.1 - 2026-09-06

Documentation-focused patch release. CLI and `project-continuity/v2` protocol
behavior are unchanged.

- Redesign the English and Chinese READMEs around the single-file continuity
  workflow, with clearer positioning, quick-start commands, and common flows.
- Add a Mermaid lifecycle diagram plus concise tables for the data model,
  guarantees, compatibility, and safety boundaries.
- Clarify installation, migration, verification evidence, and the distinction
  between project progress, source artifacts, and historical evidence.

## 0.1.0 - 2026-09-05

First stable release.

- Add the v2 structured JSON state and derived Markdown view.
- Add explicit init, read-only status/validation, leased writes, revision CAS,
  operation receipts, dry-run migration, and expired-lease recovery.
- Add deterministic source packaging and cross-platform CI definitions.
- Capture Git baselines from raw index/tree and worktree bytes with a bounded,
  fail-closed fingerprint instead of commands that may execute clean filters.

- Verify the generic migration design with a private, isolated complex-project
  rehearsal: the v2 reader added no verifier regressions while the project's
  existing failures remained unchanged. This does not authorize live cutover.

Live-client hot loading and migration of existing projects remain separately
scoped validations; they are not implied by this release.