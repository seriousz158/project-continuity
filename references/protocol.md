# Protocol and data model

## Storage

`.relay/CURRENT.md` contains schema `project-continuity/v2`, metadata, one
authoritative JSON block, and one derived Markdown view. The view is regenerated
from JSON and is never parsed as state. The maximum document size is 64 KiB.
History snapshots are append-only records of prior revisions.

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
