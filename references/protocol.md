# Protocol and data model
## v4 external evidence objects

Schema "project-continuity/v4" is an explicit upgrade: an existing v2 or v3
document becomes v4 only through "migrate --to-v4".  The shared tooling still
reads v2, v3 and v4; nothing is upgraded implicitly.

CURRENT.md keeps the same front matter, managed JSON block and derived view,
but the evidence collection is no longer inline:

    CURRENT.md -> evidence_index_ref -> immutable evidence index -> objects

The JSON "evidence" value is an index reference
({schema, index, count, sha256}) whose count and digest are derived from the
bound records; it is never an independently editable list.  A "corrections"
collection of the same shape carries immutable corrections.

Objects live under .relay/objects/ and every file name is the SHA-256 of that
file's exact bytes:

    objects/evidence/<aa>/<object_sha>.json
    objects/correction/<aa>/<object_sha>.json
    objects/manifest/<aa>/<manifest_sha>.json
    objects/chunk/<aa>/<chunk_sha>.bin
    objects/index/evidence/<aa>/<index_sha>.json
    objects/index/correction/<aa>/<index_sha>.json

An object envelope binds schema, project id, record type and content format
version, and preserves the record verbatim under "payload".  Objects larger
than 262144 bytes are split into ordered raw chunks with a content-addressed
parent manifest that records the total length and the overall digest; every
chunk is verified before the payload is parsed.  Index nodes are leaves
(bounded entries) or branches (bounded children) with a maximum depth, and a
node visited twice is RELAY_REFERENCE_CYCLE.

Resolution is explicit:

    resolve(document_text, project_root) -> complete logical state

The project root is always supplied by the caller.  A v4 document without one
is RELAY_SCHEMA_V4_REQUIRES_RESOLVER; a read-only envelope parse (front matter
plus the managed JSON) exists for diagnostics but never decides completion.

Integrity and budget semantics are deliberately distinct:

  * a missing, mismatched, cross-project, unreadable, cyclic or over-limit
    object/index/chunk is KNOWN CORRUPTION, named
    (RELAY_OBJECT_MISSING, RELAY_OBJECT_HASH_MISMATCH,
    RELAY_OBJECT_SCHEMA_INVALID, RELAY_OBJECT_PROJECT_MISMATCH,
    RELAY_INDEX_INVALID, RELAY_CHUNK_MISSING, RELAY_REFERENCE_CYCLE,
    RELAY_OBJECT_LIMIT_EXCEEDED, RELAY_OBJECT_PATH_INVALID,
    RELAY_OBJECT_TYPE_INVALID) and reported as a read-only degraded state;
    mutations and completion claims fail closed;
  * a correction target that this build cannot bind to a real object is refused
    by name before anything is published: RELAY_CORRECTION_TARGET_UNSUPPORTED
    (no owned namespace) or RELAY_CORRECTION_TARGET_UNREACHABLE (the object is
    outside the traceable range).  A syntactically valid digest is never
    accepted as a verified target;
  * exhausting the deep-validation time or IO budget is
    RELAY_VALIDATION_BUDGET_EXCEEDED: the check is INCOMPLETE, never
    corruption and never a PASS.  Any state commit or completion judgement
    that depends on it is refused.

CURRENT.md commits at or below 32768 bytes (V4_MAX_BYTES) after every
successful v4 write, including the operation receipt, writer/lease and
migration metadata.  The 64 KiB protocol ceiling is unchanged; v4 simply never
spends the upper half.  Receipt governance uses the same archive chain, with
the compaction trigger scaled to the v4 limit.

Before publishing anything, a write estimates the unique new objects, the
history snapshot and the candidate document under the single-writer lock.
RELAY_STORAGE_QUOTA_EXCEEDED and RELAY_DISK_SPACE_INSUFFICIENT are raised
before the CURRENT replacement, so a refused write leaves the committed
document untouched and never reports a partial success.  Read-only diagnostics
never require writing to a full disk.

Optional v4 extensions:

  * extensions.ac_map — deterministic, persistent acceptance-condition ids
    (ac-<128-bit prefix of sha256(task_id + NUL + condition text)>), the
    original text unchanged; "coverage" prints the
    evidence x acceptance x generation matrix.
  * extensions.blocker_scope — a versioned scope for a blocker.  A blocker
    without an entry keeps its original scope (its own task); an entry may only
    widen it and must still cover the declared task.  No new task status word
    is introduced and no historical blocker is narrowed automatically.
  * "corrections" — immutable correction/revocation/supersession records bound
    to the exact digest of their target.  Revoked or superseded evidence never
    satisfies a completion gate; coverage and handoff apply the same judgement,
    and the target records stay queryable.  See "Effective evidence" below.

## v5 external Markdown and acceptance map

Schema "project-continuity/v5" is a second explicit upgrade, reached only
through "migrate --to-v5" from a v4 document.  It externalises two more
collections that a long-lived project accumulates:

    CURRENT.md -> markdown index ref -> immutable section objects
    CURRENT.md -> ac_map index ref   -> one immutable acceptance-map record

The unmanaged Markdown region of the document is replaced by a compact stub
that lists each section with its stable id, byte length and content digest, and
that names the restore path and the source revision.  Nothing is deleted:
joining the stored sections in ordinal order reproduces the previous region
byte for byte, and the "markdown" command prints the restored text together
with the digest of the whole text so the restore can be verified.

`extensions.external_markdown.source_revision` is the revision the text was
**first** externalised from.  A later section add or replace updates the bound
`bytes`, `sections` and `text_sha256` but never advances `source_revision`, so a
consumer must judge freshness by the content identity (`sha256`/`bytes`/
`sections`) and must not report the Markdown as stale merely because
`source_revision` is smaller than the current revision.  The `markdown` command
reports a `provenance_meaning` object that states this contract.

The acceptance map keeps its v4 meaning: stable acceptance identifiers derived
from the task id and the condition text, in task order.  Its record is bound to
the digest of the acceptance conditions it was minted from, so a task change
publishes a new record instead of reusing a stale map, and a document whose
bound map no longer matches its tasks is refused by name
(RELAY_AC_MAP_MISMATCH).

Resolution keeps its shape and its result:

    resolve(document_text, project_root) -> complete logical state

A v5 document resolves to exactly the logical state its v4 predecessor
resolved to: the acceptance map returns to "extensions.ac_map" and the
Markdown text is reconstructed separately by resolve_markdown() or the
"markdown" command.  Both new collections use the existing object, index and
budget machinery, so v5 inherits the named corruption codes and the rule that
an exhausted budget is an incomplete check and never a pass.  A build that does
not implement v5 refuses the document by name and writes nothing.

The document limit is unchanged: 32768 bytes (V4_MAX_BYTES) remains the hard
limit for v4 and v5, the 64 KiB protocol ceiling is untouched, and the 24576
byte governance target stays an observation target rather than a limit that
would justify dropping history.

## v6 cold archive

Schema "project-continuity/v6" adds one collection to v5, `archive`, an index
reference of the same shape as `evidence`.  `init` writes v6, and
`migrate --to-v6` (from v5) or `migrate --to-latest` (from any older schema)
reaches it explicitly.  Older builds refuse v6 by name and write nothing.

    CURRENT.md -> archive index ref -> immutable archive records

Each archive record (`project-continuity/archive-record/v1`) keeps one hot
record verbatim with `collection`, `record_id`, `archived_at_revision` and
`reason`; its id is derived from the collection, the record id and the
revision, so the same move is never stored twice.  Archiving is lossless: the
logical state is the hot document plus its archive, `archive --id <id>` prints a
record exactly as it was, and `migrate_compare.py` folds the archive back in
before comparing.  Appending reads only index nodes; `--verify-integrity`
re-hashes every archived object.

A v6 write whose candidate stays at or above the trigger after receipt
compaction archives a closed set, oldest first, until it is below the target:

  * done or cancelled tasks that are not `current_task`, that no hot task
    depends on and that no open blocker names in its scope;
  * resolved blockers (also those of live tasks) and every blocker of an
    archived task, together with their `blocker_scope` entries;
  * the evidence of archived tasks, decisions whose `task_ids` are all
    archived, and corrections bound only to archived records.

A decision or correction that spans a hot and a cold record keeps the whole set
hot; no hot record ever points into the archive.  The acceptance map is rebuilt
from the remaining tasks.

Explicit decision archival uses the patch-only `archive_decisions` list on
update/save, without changing this wire schema or archive-record/v1. It preserves
each full record, including task_ids, and uses an explicit reason distinct from
automatic archival. No age/size heuristic selects decisions. Hot incoming
relationships must be closed first; this operation refuses them without a
cascade. Ordinary immutable upserts and existing transaction/replay guarantees
are unchanged. See commands.md for usage and discovery through `archive --task`.

User extensions are archived only on request: on v6 an update with
`"extensions": {"key": null}` moves that key into the archive and removes it.
Managed keys (`compaction`, `ac_map`, `external_markdown`, `blocker_scope`,
`recovery_log`, `migration*`) are refused.  Before v6 a null extension value is
still stored as a value.

v6 keeps 16 receipts inline after a compaction (32 before v6), and every schema
compacts receipts only in batches of at least 16, when the hard limit forces
it, or on `compact --apply`, so a document hovering above the trigger no longer
writes one tiny segment per commit.

## Evidence assurance

`coverage` and `handoff` grade each evidence reference, and each coverage row
and the verification record carry the lowest grade among their contributors:

  * `claimed`: free text, or a path that does not exist inside the project;
  * `ref_resolved`: every path of the reference (`;`-separated, with `#anchor`
    or `:line` suffixes ignored) exists inside the project root.  Only
    existence is checked, never content;
  * `ref_bound`: the reference names a content-addressed object or manifest
    that resolved and verified;
  * `externally_checked`: the caller passed `--acknowledge-external`.

The `references` check is mandatory.  `verified=true` therefore needs the
coverage, baseline and mapping checks and an assurance of `ref_resolved` or
better; otherwise the record reports `reason=evidence_self_reported` and lists
the self-reported evidence ids.  The completion gate is unchanged: a claimed
pass still lets a task become done.  The gate decides whether work may be
recorded as finished; the verification record reports how much of that claim
the tool could check.

## Effective evidence and correction targets

Four judgements are kept separate and never substitute for each other:

  1. recorded result — the pass/fail/not_run written in the record; never
     rewritten in place;
  2. current validity — whether a correction/revocation/supersession still lets
     the record count, with the target relationship proven;
  3. acceptance coverage — whether the task's acceptance conditions are covered
     by a currently effective, generation-matching pass;
  4. runtime/external verification — whether the recorded baseline matches the
     environment and whether an external check ran to completion.

A recorded pass is never reported as currently effective, and current coverage is
never reported as verified.  When a baseline or an external check was not
examined the result is named (baseline_not_checked, baseline_mismatch,
baseline_unavailable, current_baseline_not_git) rather than implied by a null or
an empty list.

Relationship identity is the pair (record_type, record_id).  A revocation of a
decision never affects an evidence record that happens to share the same string.
Supported kinds and targets:

  * "correction" annotates; it changes no validity and may annotate another
    correction.  A relationship may not revoke or supersede a relationship.
  * "revocation" removes its target from the effective set.
  * "supersession" targets evidence only, names an existing evidence replacement,
    and is rejected when the replacement chain cycles.

Corrections are immutable: the same id with different content is refused, and a
correction can never target itself.

Manifest target types are not interchangeable:

  * target_type "chunk_manifest" is the only manifest namespace this build owns.
    It is resolved under an explicit project root at
    objects/manifest/<aa>/<sha>.json from the committed evidence/corrections
    index; the file must exist, its bytes must hash to the claimed digest, its
    schema and type must match, its project id must match, and it must be inside
    the traceable range.  The stored target_sha256 is derived from the resolved
    manifest descriptor, never copied from the input.
  * "manifest" and other ambiguous or unowned names (handoff bundle manifests,
    external evidence manifests) are refused by name.  Because every field of a
    valid evidence or correction record is bounded, no record envelope accepted
    by the validator can reach the 262144-byte chunk threshold, so no valid
    document can currently reference a chunk manifest at all; the resolver path
    exists, is verified against real published bytes, and fails closed.

A root-bound target is re-verified inside the write lock immediately before the
CURRENT replacement, so verification and commit cannot drift apart.

## Storage

`.relay/CURRENT.md` contains a schema from `project-continuity/v2` to
`project-continuity/v6` (`init` writes v6; `init --schema v2` the legacy inline
format), metadata, one authoritative JSON block, and one derived Markdown view. The view is regenerated from JSON and is never
parsed as state. The maximum document size is 64 KiB. History snapshots are
append-only records of prior revisions.

When the candidate reaches 80% of the limit, the CLI automatically keeps the
newest 32 operation receipts in `CURRENT.md` and writes older receipts as
content-addressed JSONL segments under `.relay/receipts/` (v6 keeps 16).
Receipt-aware schemas (v3 and later) are required before automatic governance;
old v2 clients reject them instead of silently losing retry protection. Before
v6, tasks, blockers, evidence, decisions, extensions, and custom Markdown are
never automatically moved; v6 moves finished records into its lossless cold
archive (see "v6 cold archive") and never removes anything. The target after a
successful compaction is 70%; if business content alone does not fit, the
write fails closed.

After explicit initialization, `CURRENT.md` is the sole authority for a new
project's current progress. For an existing project, built-in migration handles
only the v1 relay document format; it cannot map arbitrary business JSON or
replace project verifier readers. `migrate --apply` sets
`extensions.migration.mapping_review_required=true`. Report that state as
`UNVERIFIED`, retain the prior business authority, and do not call the project
migration complete until mapping and reader cutover are reviewed and performed.
Pause ongoing progress edits during cutover and never dual-write both authorities.
After the explicit project cutover, `CURRENT.md` is the sole current-progress
authority. Historical evidence and release, deployment, and experiment
artifacts remain separate; task completion cannot imply those outcomes.

Version 1 is read-only until `migrate --apply`. This is relay-format migration,
not arbitrary project-state migration. Always inspect the migration dry
run and pass its `source_sha256` back with `--source-sha256` when applying it;
this binds approval to the exact v1 input. Migration cannot invent evidence: imported narrative is
reported conservatively and must not be presented as verified v2 evidence.

## State

- `project`: name, goal, status, current task, next step, and separately named
  merge/release/deploy/external outcomes.
- `tasks`: stable ID, title, status, owner, dependencies, acceptance conditions,
  and a managed generation. Status is `todo`, `doing`, `blocked`, `done`, or
  `cancelled`.
- `blockers`: stable ID, task ID, description, open/resolved status, and a
  resolution when resolved.
- `evidence`: immutable ID, task ID, check, result, timestamp, reference,
  acceptance coverage, and CLI-recorded Git baseline/generation.
- `decisions`: immutable ID, related task IDs, conclusion, and reason.
- `extensions`: namespaced project-specific data.

Typed changes are partial upserts by ID; they cannot delete records (on v6 a
null extension value archives that key), remove acceptance criteria, choose arbitrary document paths, rewrite evidence, or
rewrite decisions. Reopening a final task requires a reason and increments its
generation, invalidating earlier evidence for completion.

## Receipt archives and recovery

The `extensions.compaction` object stores only the archive schema, chain head,
cumulative archived count, and retention count. `status` reports both the
inline/current and archived receipt counts. Each segment contains a project
ID, previous-segment link, operation ID/hash/revision records, and is named by
the SHA-256 of its exact bytes. Only segments reachable from the current chain
head are committed history; orphan files after a failed pre-commit are not
imported or deleted automatically. `status` reports a degraded read-only state
for a missing or damaged chain; `validate` and all mutations fail closed until
the archive is repaired or an explicit history recovery is performed.

`compact` is read-only by default. `compact --apply` requires the same writer,
revision, lease, and operation-ID checks as other mutations. `migrate --to-v3`
explicitly upgrades a v2 document without changing project data. A single
`--no-auto-compact` write can retain the old v2 behavior, but it cannot bypass
the hard 64 KiB limit.

`export --output HANDOFF.zip` is an explicit, no-overwrite operation that
packages `CURRENT.md`, a manifest, and only chain-reachable receipt segments.
It never includes `history/`, `inbox/`, locks, or backups. `verify --bundle`
validates entry paths, checksums, schema, and the receipt chain in a temporary
directory without modifying the target project.

## Garbage collection

History snapshots and object files are never deleted implicitly.  `gc` reports
what an explicit collection would free and writes nothing.  `gc --apply` needs
`--writer` holding the live lease and `--expected-revision`, runs under the
relay lock, re-plans there, and never changes `CURRENT.md`:

  * it keeps the newest `--keep-history` snapshots named `r<rev>-<sha>.md`
    (default 50) and deletes older ones;
  * it deletes an object file only when neither CURRENT nor any kept snapshot
    reaches it, so `recover --snapshot` keeps working for every kept snapshot;
  * receipt segments, files that are not named like relay objects or
    snapshots, links and directories are never collected;
  * a reachable object that is already missing stops the whole collection, and
    a hand-named history file that cannot be parsed blocks object collection
    because its references cannot be proven.

Recovery from a snapshot still requires the same external schema, so after a
migration only the snapshots written since then can be recovered with
`--snapshot`; older ones remain readable files.

## Concurrency and recovery

Revision comparison-and-swap rejects stale writers. A non-expired lease rejects
other writers. Operation IDs provide idempotent retry receipts: reuse is valid
only for the identical operation. Local OS locks prevent cooperating processes
on one machine from racing, but are not distributed locks across shared disks
or hosts.

After a crash, inspect `status` and `validate`. `recover` requires an expired
lease and an audit reason. If selecting a snapshot, use only a filename already
reported from `.relay/history/`. Recovery is explicit repair, not a way to take
over a current writer.

## Git sharing

The default initialization keeps relay runtime data local through
`.relay/.gitignore`. If a team deliberately shares current state through Git,
share only `CURRENT.md` and its intentional ignore configuration, never the
whole `.relay/` directory. Review content for sensitive data and treat Git
conflicts as protocol conflicts. The tool never commits or pushes.

## Git baseline limitations

Git identity is computed read-only from raw index/tree records and raw worktree
bytes (`ls-files` and `ls-tree`), rather than `status` or `diff`; this avoids
executing configured clean filters during capture. At most 256 MiB of worktree
content is fingerprinted. Tracked symlinks, submodules, non-regular or linked
files, unmerged indexes, and roots that are not the worktree root are rejected.
CRLF conversion and repositories that rely on clean filters can be reported
conservatively as dirty because raw worktree bytes differ from indexed blobs.
An unavailable or over-budget baseline is an explicit error/unknown result, not
evidence that the tree is clean.


### Capacity retention semantics

The 32-receipt retention (16 on v6) is a post-compaction target, not a validation limit.
Below 52,429 bytes, v3 may accumulate more receipts without archiving.
Governance may compact managed JSON and use a compact derived view; custom
Markdown remains unchanged. Compact previews without operation parameters are
estimates and exclude the new operation metadata, not commit guarantees.
Fully parameterized previews use the commit planner without writing files;
apply repeats validation under lock.

Every successful mutation reports an additive `capacity` receipt.  `delta_bytes`
is the net CURRENT.md change only (and may be negative after compaction);
object-store bytes are reported separately as `object_store_new_bytes` and are
never added into `delta_bytes`.  `recommended_action` names the next sensible
step ("none", compact/migrate, or a replay note).  `next_save_cycle_estimate`
models the next ordinary resume->save cycle with its writer, operation-id,
lease and clock assumptions and names what would invalidate it; when it cannot
be built the estimate is reported as `not_computed` with a reason rather than a
reassuring zero.  The receipt is computed after a successful commit and never
turns it into a failure.  An idempotent replay reports the current observed
occupancy with `committed=false`/`replayed=true` and no fresh `delta_bytes`.

`capacity --input` is a read-only preview of a real typed patch that reuses the
commit planner (so a fixed input, state, clock and identity predicts the exact
commit bytes).  It creates no lock, lease, object, receipt or history, never
changes CURRENT.md and never initialises `.relay`.  A live lease held by another
writer is refused by name (`RELAY_PREVIEW_LEASE_CONFLICT`) instead of being
predicted; a preview is never a reservation, and apply always rereads under the
lock.

A long-record hint is a non-blocking, read-only diagnostic measured in UTF-8
bytes.  It names the collection, record id, field and size and never echoes the
value, never truncates or externalises content, and never changes the hard
capacity limit.  Evidence `result` is an enum and is not treated as prose.
Detailed logs, matrices and report bodies belong in an evidence root; relay
state keeps identity, result, baseline and the acceptance linkage.  A protocol
object reference is provable by the resolver, while a plain file path is only a
pointer and is never proof that the content was verified.

`capacity` never presents an estimate that omits the next operation metadata as
proof that the next commit fits.  Its estimate models a resume, including the
new receipt, the writer/lease fields and the revision change, and reports the
model it used plus the per-byte sensitivity of the writer and operation ids.
With writer/revision/operation arguments it uses the commit planner and the
resulting byte count equals the following commit's byte count for the same
arguments on an unchanged document.  `capacity` also reports, per record class
(evidence, correction, task, acceptance, decision, blocker, operation receipt
and a full resume/save cycle), the marginal document bytes and the marginal
object-store bytes, so growth is measured rather than assumed.  Externalising a
collection removes its growth from the document but not its growth in the object
store.


### Long-running projects

Archive validation is linear in committed receipt history. Segments are batched
(at least 16 receipts unless the hard limit forces a write), not created on
every update. Index leaves are written 16 entries wide, so an append rewrites
one small leaf and its ancestors. Nothing is deleted automatically; `gc` is the
explicit way to drop old history and unreachable objects. There is no
distributed locking. Export/verify use
matching uncompressed budgets; a project can outgrow the bundle budget even
while CURRENT remains small. Transfer must then be planned explicitly.


## Lightweight reads and extension hygiene (v0.6)

Start an existing relay with read-only `status --brief`; absence never authorizes
`init`. The brief is a projection, not a completion gate. `progress_summary`
is additive in full status: `progress_summary.gap_count` is an alias for
`uncovered_acceptance`, the total uncovered acceptance rows (the same judgement
as handoff), not all project gaps or a verification verdict. It does not count
blockers, drift, mapping review or integrity warnings. Counts are null when
resolution is incomplete. Brief includes `mapping_review_required` and
`migration_required`; the mapping flag is null if state could not be resolved,
not false. Recent evidence is the last three by
recorded timestamp, with insertion order breaking ties. Use `handoff` and
`coverage` for the details; brief reduces stdout bytes, not integrity I/O.

Mutation responses report an absent `writer` or `lease_until` as JSON `null`,
not the string `"null"`. On an idempotent replay, `revision` is the original
receipt revision; `current_revision` is the current document revision.
The returned `writer` and `lease_until` describe the current lease, not the
lease at the time of the original commit. Use those current fields when
planning the next write; a replay does not reacquire or renew a lease.

Record each round as evidence with `ref` pointing at the report. Extensions
hold pointers, not report bodies, full result matrices or per-round logs.
`capacity --extension-limit N` lists the largest user keys and their estimated
serialized entry bytes (not an exact post-archive saving). Tombstones have
exactly `archived: true` (boolean) and a nonempty string `archive_ref`.
Managed namespaces and referenced target keys are excluded. Keep the reported
`preserve_keys`, including aggregate archive pointers. A dangling reference is
still a tombstone, not proof that the original payload can be recovered.

On v6 the suggested `{"extensions":{"key":null}}` patch moves the existing
value to the cold archive; inspect/confirm it before applying. v2-v5 need an
explicit migration first: null is not archival there. Archiving a tombstone
preserves only that marker. External manifest contents, paths and integrity
remain separately unverified. No hint performs a write or garbage collection.
Status only hints at tombstones when their entries total at least 1 KiB or the
document exceeds its trigger. A history count over twice the default retention
suggests a read-only `gc` preview, never automatic deletion.


## Detailed integrity and relationship invariants

- Relationship identity is `(record_type, record_id)`; a correction is immutable and may not revoke another correction. A manifest correction target is accepted only after the object is resolved and verified under an explicit project root; ambiguous manifest names are refused by name.

- Receipt archives are content-addressed and local under `.relay/receipts/`; only
  the chain reachable from `CURRENT.md` is committed history. Missing or
  corrupted archives permit read-only inspection but block mutations.

- v4 evidence lives in content-addressed objects under `.relay/objects/`, a
  v5 document additionally externalises its custom Markdown sections and its
  acceptance map the same way, and a v6 document keeps its cold archive there
  too (each archived record verbatim; archiving never deletes).  Resolve them only with
  `resolve(document_text, project_root)`; an explicit project root is
  mandatory, an envelope is never a completed state, a missing object is named
  corruption (read-only degraded), and an exhausted deep check is an
  incomplete check that can never be reported as PASS.  `--verify-integrity`
  is the explicit deep check: it reads every reachable object and index node,
  names every failure, and refuses to report a document whose custom Markdown
  stub, acceptance map or object digests disagree with the bound bytes.

- Verification frequency (2026-10-04, ADR-T004-007): `--verify-integrity` and digest
  recomputation are boundary operations, not per-write rituals. Run the deep check at
  handoff boundaries — export/import, cross-agent or cross-machine transfer, migration,
  recovery, a suspected disagreement — and when the user or project explicitly asks. Do not
  re-verify the same content twice in one session, and do not paste hash lists into reports
  or into `CURRENT.md`: a re-hash of content the same agent just wrote is not independent
  evidence.
