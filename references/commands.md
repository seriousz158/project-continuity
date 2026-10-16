# Command reference

Run from the installed skill directory, replacing `<project>`, IDs, and
revision numbers with values you have actually read.

```bash
# Explicit initialization only.  Writes v6; --schema v2 keeps the legacy
# inline format.
python scripts/write_current.py init --root <project>

# Read-only; neither command initializes state
python scripts/write_current.py status --root <project>
python scripts/write_current.py validate --root <project>

# Acquire a 30-minute lease after reviewing status and Git drift
python scripts/write_current.py resume --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Retain the lease while applying typed partial upserts
python scripts/write_current.py update --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  --input change.json

# Apply changes, archive the prior state, and release the lease
python scripts/write_current.py save --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  --input - < change.json

# Explicitly prune a referentially closed historical set.  The manifest must
# enumerate every affected task, blocker, evidence and decision ID plus a
# target revision and reason.  A dry run performs no write; --apply retains the
# lease so a following save can release it.  External object files are kept.
python scripts/write_current.py prune --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  --input prune-manifest.json
python scripts/write_current.py prune --apply --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  --input prune-manifest.json

# v6 only: archive one user extension key instead of writing a tombstone.
# Managed keys (compaction, ac_map, external_markdown, blocker_scope,
# recovery_log, migration*) are refused.
#   {"extensions": {"r1012": null}}

# Read-only: list the v6 cold archive, or print archived records verbatim by
# archive id or by original record id.
python scripts/write_current.py archive --root <project> --limit 50 --offset 0
python scripts/write_current.py archive --root <project> --id <record-id>

# Garbage collection: a report by default, which writes nothing.  --apply needs
# the live lease; it deletes history snapshots beyond --keep-history (default
# 50) and objects that neither CURRENT nor a kept snapshot reaches.  Receipt
# segments and unrecognized files are never collected.
python scripts/write_current.py gc --root <project> --keep-history 50
python scripts/write_current.py gc --apply --root <project> --keep-history 50 \
  --writer <writer-id> --expected-revision <n>

# Read-only capacity/receipt forecast. It never creates .relay or writes files.
python scripts/write_current.py compact --root <project>

# Explicit compaction (normally automatic at the 80% threshold). Acquire a
# lease first; this retains the lease for the next update.
python scripts/write_current.py compact --apply --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Read-only: the exact custom Markdown of a document (v4 or v5).  For v5 the
# text is rebuilt byte for byte from the bound objects and the digest of the
# whole text (and of each section) is reported, so a restore can be verified.
python scripts/write_current.py markdown --root <project>
python scripts/write_current.py markdown --root <project> --section <md-id>

# Upgrade to the latest schema.  The dry run lists every remaining step,
# previews the first one exactly and reports source_sha256.  --apply runs each
# step as its own commit (operation ids <unique-id>.<step>); later steps bind
# to the document the previous step committed.
python scripts/write_current.py migrate --root <project> --to-latest
python scripts/write_current.py migrate --root <project> --to-latest --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Single steps.  Migration is a dry run unless --apply is present.  Without a
# flag, migrate converts a v1 document to v2.
python scripts/write_current.py migrate --root <project>
python scripts/write_current.py migrate --root <project> --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Externalise the custom Markdown and the acceptance map (v4 -> v5).  A dry
# run reports the candidate size and the exact source SHA-256 that --apply
# requires; the v4 migration is never repeated.
python scripts/write_current.py migrate --root <project> --to-v5
python scripts/write_current.py migrate --root <project> --to-v5 --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Add the v6 cold archive to a v5 document.
python scripts/write_current.py migrate --root <project> --to-v6
python scripts/write_current.py migrate --root <project> --to-v6 --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Explicitly enable the receipt-aware v3 format for an existing v2 document.
python scripts/write_current.py migrate --root <project> --to-v3
python scripts/write_current.py migrate --root <project> --to-v3 --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Recovery is allowed only after lease expiry
python scripts/write_current.py recover --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  --reason "reviewed interrupted write"

# Optional controlled handoff package. It contains CURRENT.md, the receipt
# segments reachable from its chain and the reachable objects; history, inbox,
# and locks stay local. The destination must be a new .zip file.
python scripts/write_current.py export --root <project> \
  --output /path/to/project-continuity-handoff.zip
python scripts/write_current.py verify --root <project> \
  --bundle /path/to/project-continuity-handoff.zip
```

Add `--lease-minutes <positive-integer>` when the default 30 minutes is not
appropriate. Add `--allow-drift` only after reviewing reported Git drift. For
snapshot recovery, add `--snapshot <history-filename>`; never pass a path.
Writes automatically archive older operation receipts when the candidate file
is at or above 80% of its limit (64 KiB for v3, 32 KiB from v4), keeping the
newest 32 receipts in `CURRENT.md` (16 on v6). Receipts move in batches of at
least 16 unless the hard limit forces a smaller one. On v6 a candidate still
at or above the trigger then moves finished records into the cold archive.
Use `--no-auto-compact` on `resume`, `update`, or `save` for a single write when
you need the pre-v3 fail-closed behavior. It never truncates project data; a
candidate that still exceeds 64 KiB is rejected.

Every new mutating operation advances the revision; replay does not. Reuse the
returned revision (current_revision on replay) for the next mutation. Do not
reuse an explicit operation ID for different input.
Input files whose path or ancestors are symbolic links are rejected; use
reviewed standard input when a trusted regular file path is unavailable.

`migrate` converts only a v1 relay document to the v2 relay format. After apply,
`extensions.migration.mapping_review_required=true` means project field mapping
and reader cutover remain `UNVERIFIED`. It cannot import arbitrary business JSON
or verifier behavior. Keep the prior business authority until reviewed cutover;
pause ongoing edits and do not dual-write both authorities during that cutover.


### Capacity preview accuracy

`compact` without writer/revision/operation arguments is a read-only size
estimate excluding the next operation. Supply all three arguments (and omit
`--apply`) to validate lease, CAS and the complete candidate using the same
planner as the commit; the measured preview and the following commit produce
identical byte counts for identical arguments on an unchanged document. Full `migrate --to-v3` previews additionally require
`--source-sha256`. Previews do not create locks, archives or history. Apply
always rereads under lock; a preview is not a reservation or drift approval.

### v4 external evidence objects

```bash
# Explicit, dry-run-first upgrade of a v2/v3 document.  After --apply the
# project keeps writing v4; objects are published and fsynced before the
# history snapshot and the CURRENT replacement, never after it.
python scripts/write_current.py migrate --root <project> --to-v4
python scripts/write_current.py migrate --root <project> --to-v4 --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  [--allow-drift] [--lease-minutes <n>]

# Read-only derived views: no state, no second authority, no writes.
# coverage reports the evidence x acceptance matrix from the shared judgement;
# every row separates recorded coverage from current coverage and from a
# verified baseline.  Rows and the verification record carry an assurance
# grade (claimed < ref_resolved < ref_bound < externally_checked); verified=true
# needs ref_resolved or better, otherwise reason=evidence_self_reported.
# --acknowledge-external declares that external references were checked
# outside the CLI and grades them externally_checked.
python scripts/write_current.py coverage --root <project> [--task <id>] [--limit 20] \
  [--acknowledge-external] [--verify-integrity]

# handoff pages tasks, blockers, uncovered acceptance and evidence separately
# and reports total/has_more for each.  complete=false whenever any page is
# truncated, so a truncated read can never look like a successful handoff.
python scripts/write_current.py handoff  --root <project> [--task <id>] [--limit 25] [--offset 0] \
  [--uncovered-limit <n>] [--uncovered-offset <n>]

# capacity also reports a modelled next commit (including its own operation
# receipt, writer and lease) and the marginal cost of one more record of each
# class under "growth".
python scripts/write_current.py capacity --root <project>

# With all three transaction arguments, capacity uses the complete commit
# planner for the realistic next write (a resume) and reports the objects the
# next commit would publish:
python scripts/write_current.py capacity --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Read-only preview of a real typed patch. With no live lease it models
# resume->save for ordinary patches; a prune manifest models
# resume->update(prune)->save.  While --writer holds the lease it models that
# writer's save (or update->save for prune).
# Another writer's live lease is refused by name (RELAY_PREVIEW_LEASE_CONFLICT).
# It creates no lock, lease, object, receipt or history and never changes
# CURRENT.md; the real commit rereads and revalidates under the lock.
python scripts/write_current.py capacity --root <project> \
  --writer <writer-id> --input change.json

# A non-blocking long-record hint locates a large field by collection/id/field
# and byte size; it never echoes the value and never truncates or externalises.
python scripts/write_current.py capacity --root <project> --long-record-threshold 4096
```

Every successful mutation additionally reports an additive `capacity` block:
`used_bytes`, `limit_bytes`, `headroom_bytes`, `delta_bytes` (CURRENT.md only,
may be negative), `near_limit`, `recommended_action`, `object_store_new_bytes`
(kept separate from `delta_bytes`) and `next_save_cycle_estimate` (the next
ordinary resume->save cycle, with its writer/operation/clock assumptions, or a
named `not_computed` reason). An idempotent replay reports the current
occupancy with `committed=false` and `replayed=true`; it is never presented as
a fresh commit.

A v4, v5 or v6 write commits CURRENT.md at or below 32768 bytes.  `capacity` always
carries the next operation receipt in its model; a preview that omitted the
operation metadata is never presented as proof that the next commit fits.
`handoff` fails by name when a bound object or index reference is missing; it
never emits a successful handoff.  A missing or damaged object is reported
read-only as degraded and blocks mutation; an exhausted deep check is an
incomplete check and is never a PASS.

Correction targets whose bytes live outside the document (`target_type`
`chunk_manifest`) are verified against the real object before anything is
published and re-verified inside the write lock.  Every other manifest-shaped
target type is refused by name with RELAY_CORRECTION_TARGET_UNSUPPORTED.

Before the first v4 object write, confirm the relay is protected by local
ignore rules (`git check-ignore`, `git status --porcelain -- .relay`,
`git ls-files .relay`) and that no writer lease is live.

### Independent migration comparison

```bash
python scripts/migrate_compare.py --before <pre-CURRENT.md> \
  --after <post-CURRENT.md> [--root <project>]
```

Exit status 0 means logically equivalent; 1 means a business difference was
found (reported per record collection); 2 means the documents could not be
compared.  Only the schema, storage references, managed metadata, revision,
timestamps, lease and this operation receipt may differ; business fields are
never on an ignore list.


## v0.6 convenience parameters

```bash
python scripts/write_current.py status --brief --root <project>
python scripts/write_current.py save --acquire --root <project> \
  --writer <writer-id> --expected-revision <n> --input change.json
python scripts/write_current.py capacity --acquire --root <project> \
  --writer <writer-id> --expected-revision <n> --input change.json
python scripts/write_current.py capacity --extension-limit 10 --root <project>
```

Only resume/update/save permit omitted operation IDs; the existing canonical
receipt hash supplies `auto-` plus 24 hex characters. It covers command, writer,
expected revision, normalized input and semantic flags (including acquire).
It is a 96-bit prefix, not a mathematical collision-free identity. The full
hash is still checked for receipt reuse; a collision fails closed. Explicit IDs
retain precedence and legacy explicit receipts remain replayable. Keep all
retry arguments unchanged. After a replay use current_revision and the returned
current writer/lease, not the old receipt revision.

`save --acquire` accepts absent/expired or own live leases, refuses live foreign
leases, reviews Git drift exactly as resume does, and releases the lease in one
commit. Default save still requires an owned live lease. `capacity --acquire`
models this one commit, not a resume/save cycle. Capacity with writer and
expected revision can derive the exact next resume ID without --operation-id.
Normal defaults and all prior field meanings are preserved; full status adds
progress_summary, and mutation responses add operation_id/current replay lease.
