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

`.relay/CURRENT.md` contains schema `project-continuity/v2` (the default) or
`project-continuity/v3` (receipt-aware), metadata, one authoritative JSON block,
and one derived Markdown view. The view is regenerated from JSON and is never
parsed as state. The maximum document size is 64 KiB. History snapshots are
append-only records of prior revisions.

When the candidate reaches 80% of the limit, the CLI automatically keeps the
newest 32 operation receipts in `CURRENT.md` and writes older receipts as
content-addressed JSONL segments under `.relay/receipts/`. Explicit `migrate --to-v3 --apply` is required before automatic governance; old v2 clients reject v3 instead of silently losing
retry protection. Tasks, blockers, evidence, decisions, extensions, and custom
Markdown are never automatically removed or moved. The target after a
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

Typed changes are partial upserts by ID; they cannot delete records, remove
acceptance criteria, choose arbitrary document paths, rewrite evidence, or
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
executing configured clean filters during capture. At most 64 MiB of worktree
content is fingerprinted. Tracked symlinks, submodules, non-regular or linked
files, unmerged indexes, and roots that are not the worktree root are rejected.
CRLF conversion and repositories that rely on clean filters can be reported
conservatively as dirty because raw worktree bytes differ from indexed blobs.
An unavailable or over-budget baseline is an explicit error/unknown result, not
evidence that the tree is clean.


### Capacity retention semantics

The 32-receipt retention is a post-compaction target, not a validation limit.
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
at the byte threshold, not created on every update. There is no persistent
index, automatic archive deletion, or distributed locking. Export/verify use
matching uncompressed budgets; a project can outgrow the bundle budget even
while CURRENT remains small. Transfer must then be planned explicitly.
