# Command reference

Run from the installed skill directory, replacing `<project>`, IDs, and
revision numbers with values you have actually read.

```bash
# Explicit initialization only
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

# Read-only capacity/receipt forecast. It never creates .relay or writes files.
python scripts/write_current.py compact --root <project>

# Explicit compaction (normally automatic at the 80% threshold). Acquire a
# lease first; this retains the lease for the next update.
python scripts/write_current.py compact --apply --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Migration is a dry run unless --apply is present
python scripts/write_current.py migrate --root <project>
python scripts/write_current.py migrate --root <project> --apply \
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

# Optional controlled handoff package. It contains CURRENT.md and only the
# receipt segments reachable from its v3 chain; history, inbox, and locks stay
# local. The destination must be a new .zip file.
python scripts/write_current.py export --root <project> \
  --output /path/to/project-continuity-handoff.zip
python scripts/write_current.py verify --root <project> \
  --bundle /path/to/project-continuity-handoff.zip
```

Add `--lease-minutes <positive-integer>` when the default 30 minutes is not
appropriate. Add `--allow-drift` only after reviewing reported Git drift. For
snapshot recovery, add `--snapshot <history-filename>`; never pass a path.
Writes automatically archive older operation receipts when the candidate file
is at or above 80% of 64 KiB, keeping the newest 32 receipts in `CURRENT.md`.
Use `--no-auto-compact` on `resume`, `update`, or `save` for a single write when
you need the pre-v3 fail-closed behavior. It never truncates project data; a
candidate that still exceeds 64 KiB is rejected.

Every successful mutating operation advances the revision. Re-read status
before the next mutation. Do not reuse an operation ID for different input.
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
planner as the commit. Full `migrate --to-v3` previews additionally require
`--source-sha256`. Previews do not create locks, archives or history. Apply
always rereads under lock; a preview is not a reservation or drift approval.
