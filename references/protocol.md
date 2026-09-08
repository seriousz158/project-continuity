# Protocol and data model

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


### Long-running projects

Archive validation is linear in committed receipt history. Segments are batched
at the byte threshold, not created on every update. There is no persistent
index, automatic archive deletion, or distributed locking. Export/verify use
matching uncompressed budgets; a project can outgrow the bundle budget even
while CURRENT remains small. Transfer must then be planned explicitly.
