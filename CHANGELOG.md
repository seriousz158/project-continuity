# Changelog

## Unreleased

- Explicit v6 `archive_decisions` patch operation on update/save (including
  save --acquire) and capacity previews; lossless records, immutable-ID and
  hot-reference guards reuse existing archive and transaction formats.
- Archive lookup by task, archived-decision discovery in handoff, capacity
  collection/record diagnostics and actionable preview hints. Brief reports
  remaining bytes; decision-share warnings are nonblocking.
- `validate --verify-integrity` now performs the existing deep check. Mapping
  review gets an explicit reason; prune requirements and concise decision
  recording guidance are documented.

- Coverage pagination (including offset/nested evidence pages) no longer
  produces a whole-scope verified conclusion from a truncated page.

- Snapshot recovery can repair damaged current-only objects after CAS, lease
  and receipt checks; snapshot dependencies are still fully validated.

- Protocol-object/digest assurance now checks ancestor links, object format,
  type and project binding through owned storage; plain path grading is unchanged.

- Status verification honors each check's verified flag, so a normal baseline
  pass is no longer misreported as baseline_pass failure.

The 0.6.0 candidate below improves ergonomics, lease responses and diagnostics;
its automatic archive still only handles finished-task decisions. Upgrading to
that baseline alone does not relieve an active task's decision capacity.
Unreleased explicit archival requires caller selection; it never silently hides
binding decisions. VERSION remains 0.6.0; these changes are not a tagged release.

## 0.6.0 - 2026-10-16

### Added

- `status --brief`: bounded progress counters, current task, lease/drift and
  recent evidence; full status adds `progress_summary` without removing fields.
- Stable automatic operation IDs for resume/update/save using the existing
  receipt hash. Explicit IDs and old receipt fingerprints are unchanged.
  Mutation responses include `operation_id`; replays include the current lease.
- `save --acquire`: acquire, apply and release under one lock / one revision,
  with the same drift and foreign-lease checks as resume.
- `capacity --acquire` forecasts a single save; automatic IDs also work in
  exact capacity forecasts. `--extension-limit` lists top user extension sizes,
  exact-shape tombstones, dangling keys and a v6 archival patch (advisory only).
- Existing-relay discovery and a smaller skill entrypoint; detailed invariants
  remain in protocol.md. No implicit initialization or migration.
- Metadata-only actionable revision, lease, change-field and acceptance errors.
- CI definition expands to 12 OS/Python combinations including Python 3.14,
  running both unittest and pytest. Remote run status must be checked separately.

### Changed

- Mutation responses now report an absent writer or lease as JSON `null`,
  consistently with status and operation replays (not the string `"null"`).
  Scripts that compare `writer == "null"` or `lease_until == "null"` must
  instead check for JSON `null`.

### Fixed

- Brief status includes `mapping_review_required` and `migration_required`;
  unresolved mapping review is null. Document `progress_summary.gap_count` as
  an alias for uncovered acceptance rows, not all project gaps.
- A validation time budget is exhausted at its deadline, including a zero
  budget on a coarse clock. Re-enable the Windows budget and bundle round-trip
  regressions instead of skipping known-defect paths.
- Reject oversized ZIP containers before parsing and excessive entry counts
  before member checks; encrypted members produce a controlled error, not a
  traceback. Limits: 64 MiB and 16,384 entries (central-directory parsing itself
  is not a streaming memory bound).
- Capacity-governance warnings only fire at the trigger or if compaction cannot
  fit; messages name the current schema. History/tombstone hints remain read-only.

No schema changes; v2-v6 remain readable. Distributed and cross-machine
validation, release publication and installation are separate gates.


## 0.5.0 - 2026-10-11

Capacity governance round: a lossless cold archive, explicit garbage
collection, graded evidence assurance and a one-command upgrade.

### Breaking changes

- `verified` is stricter.  The verification record of `coverage` and `handoff`
  now has a mandatory `references` check and an `assurance` grade
  (`claimed` < `ref_resolved` < `ref_bound` < `externally_checked`).
  `verified=true` additionally needs every contributing evidence reference at
  `ref_resolved` or better; a free-text or missing-path reference reports
  `verified=false` with `reason=evidence_self_reported` and the self-reported
  evidence ids.  Reports that were `true` for plain-text references become
  `false`.  The completion gate is unchanged: a claimed pass still lets a task
  become done.
- `init` writes `project-continuity/v6`.  `init --schema v2` keeps the legacy
  inline format.  Builds before 0.5.0 refuse v6 by name.
- `status`, `handoff` and `coverage` report an absent writer or lease as JSON
  `null` instead of the string `"null"`.
- On v6, `"extensions": {"key": null}` archives and removes that key instead
  of storing a null value.  Managed keys are refused.

### Added

- Schema `project-continuity/v6` with a cold archive.  A write that stays at
  or above the trigger after receipt compaction moves a referentially closed
  set (finished tasks, resolved blockers, and the evidence, decisions and
  corrections bound only to them) into content-addressed archive records,
  oldest first, until it is below the target.  Nothing is deleted: the archive
  is part of the logical state, `migrate_compare.py` folds it back in, and
  `--verify-integrity` re-hashes every archived object.
- `archive` command: page through the archive or print records verbatim with
  `--id`.  `status` and `handoff` carry an archive summary.
- `migrate --to-v6` (from v5) and `migrate --to-latest`, which previews the
  whole remaining chain and applies every step as its own source-bound commit.
- `gc` command: a report by default; `--apply` needs the live lease and
  deletes history snapshots beyond `--keep-history` (default 50) and objects
  that neither CURRENT nor a kept snapshot reaches.  Receipt segments and
  unrecognized files are never collected; an unparseable hand-named snapshot
  blocks object collection.
- `prune` (explicit, referentially closed deletion of enumerated records) and
  the verification-frequency rule ADR-T004-007, both previously shipped only
  in local installs.
- `examples/evidence.json`, a complete evidence patch for the example task.

### Fixed

- Receipt compaction no longer thrashes.  A document above the trigger whose
  receipts could not reach the target wrote one tiny segment per commit (one
  real project accumulated 208 segments).  Receipts now move only in batches of
  at least 16, when the hard limit forces it, or on `compact --apply`; v6
  keeps 16 inline instead of 32.
- Index leaves are written 16 entries wide, so appending one record rewrites
  one small leaf and its ancestors instead of a whole wide leaf (about 24 KiB per
  evidence append before).  Wide leaves written by 0.4 stay readable.
- Field errors name the missing fields and count the unexpected ones without
  echoing values; identifier errors name the field; symlink refusals name the
  offending path; a read-only `.relay` is reported as such.
- Hand-written v1 documents with empty `writer:`/`lease_until:` lines are read
  as having no lease.
- `capacity` models growth for v2/v3 documents and for projects whose first
  task is finished, instead of reporting the model as unavailable.
- The package allowlist now contains every test module, and a test enforces it.
- The Git fingerprint budget is 256 MiB (was 64 MiB).
- Windows: object names reachable from a document are `/`-separated on every
  platform, which repairs `export` and lets `gc` find reachable objects; the
  custom Markdown region of a v5/v6 document is read from CRLF files (for
  example a Git autocrlf checkout) like the managed blocks already were.

### Migration notes

- Run `migrate --to-latest` (dry run), review the steps, then apply with the
  reported `--source-sha256`.  Back up `.relay/` first.  v1 conversions still
  set `mapping_review_required` and stay `UNVERIFIED` until reviewed.
- After a migration, `recover --snapshot` accepts only snapshots of the new
  schema; older snapshots remain readable files.
- Expect `verified=false` with `evidence_self_reported` where evidence points
  at free text; point evidence at files inside the project, at relay objects,
  or acknowledge external checks explicitly.

## 0.4.0 - 2026-09-21

Relay capacity and deep-check round.

- Repair the deep integrity check.  `handoff --verify-integrity` and
  `coverage --verify-integrity` called an undefined `object_integrity` and
  raised `NameError`; both entry points now run the real deep check through
  `resolve`.  Every reachable object and index node is read and verified:
  content digest, object type and format, schema, project identity, index
  count/digest and the ordered binding of every record.  A missing, tampered,
  wrong-type, cross-project or cyclic object is refused by its named code; a
  check that exhausts its validation budget is reported as `budget_exceeded`
  (an incomplete check that can never be a pass); a schema without an object
  store is reported as `not_applicable`, never as verified.  A deep check that
  was requested is part of the required set of the verification record, so its
  degradation can no longer be absorbed into a verified conclusion.
- Check the acceptance map against the tasks.  The deep check re-derives
  `ac_map` from the acceptance conditions and refuses a document whose map has
  been rebound to different text (`RELAY_AC_MAP_MISMATCH`).
- Add schema `project-continuity/v5`: the project's custom Markdown and its
  acceptance map move into the content-addressed object store.  The document
  keeps a compact stub (section list, content digests, source revision and the
  restore command) plus two index references.  Resolving a v5 document produces
  exactly the logical state its v4 predecessor produced, and the original
  Markdown is reconstructed byte for byte by
  `write_current.py markdown --root <project>`.  `migrate --to-v5` is a dry
  run unless `--apply` is present, binds the exact source SHA-256, and never
  re-runs a completed v4 migration.  Older clients refuse v5 by name and cannot
  write to it.
- Keep the capacity contract.  32 KiB stays the hard v4/v5 document limit; the
  24 KiB size is an observation target that is now met and modelled rather than
  assumed.
- Add a uniform capacity receipt to every successful mutation.  `resume`,
  `update`, `save`, `compact` and `migrate` now report an additive `capacity`
  block: `used_bytes`, `limit_bytes`, `headroom_bytes`, `delta_bytes` (the
  CURRENT.md net change, which may be negative), `near_limit`,
  `recommended_action`, `object_store_new_bytes` (kept separate from
  `delta_bytes`) and `next_save_cycle_estimate`.  The estimate names its
  writer/operation-id/clock assumptions and what would invalidate it, or is
  reported as `not_computed` with a reason.  It is computed after a successful
  commit and never turns it into a failure; an idempotent replay reports the
  current occupancy with `committed=false`/`replayed=true`, never a fresh commit.
- Add a read-only patch preview.  `capacity --input change.json` reuses the
  commit planner, applies the real typed upsert and models `resume -> save`
  (no lease) or the lease holder's `save`.  It creates no lock, lease, object,
  receipt or history and never changes `CURRENT.md`; another writer's live lease
  is refused by name (`RELAY_PREVIEW_LEASE_CONFLICT`).  With a fixed input,
  state, clock and identity the predicted byte count equals the commit's.
- Add non-blocking long-record hints.  `capacity [--long-record-threshold N]`
  locates a large field by collection, record id, field and UTF-8 byte size
  without echoing the value; evidence `result` is an enum and is not treated as
  prose.  Hints never truncate, externalise or change the hard limit.  The
  docs and examples now recommend pointer-style records: keep identity, result,
  baseline and the acceptance linkage in `CURRENT.md`, and the detail in an
  evidence root.  A protocol object reference is provable by the resolver; a
  plain path is only a pointer and never proof that content was verified.
- Clarify the external Markdown provenance.  `extensions.external_markdown.
  source_revision` is the revision the text was first externalised from; a later
  content change updates `bytes`/`sections`/`text_sha256` but never advances it.
  The `markdown` command reports a `provenance_meaning` object so a consumer
  judges freshness by content identity and never reports the text as stale
  merely because `source_revision` is smaller than the current revision.

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
