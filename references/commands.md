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

# Migration is a dry run unless --apply is present
python scripts/write_current.py migrate --root <project>
python scripts/write_current.py migrate --root <project> --apply \
  --source-sha256 <hash-from-dry-run> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id>

# Recovery is allowed only after lease expiry
python scripts/write_current.py recover --root <project> \
  --writer <writer-id> --expected-revision <n> --operation-id <unique-id> \
  --reason "reviewed interrupted write"
```

Add `--lease-minutes <positive-integer>` when the default 30 minutes is not
appropriate. Add `--allow-drift` only after reviewing reported Git drift. For
snapshot recovery, add `--snapshot <history-filename>`; never pass a path.

Every successful mutating operation advances the revision. Re-read status
before the next mutation. Do not reuse an operation ID for different input.
Input files whose path or ancestors are symbolic links are rejected; use
reviewed standard input when a trusted regular file path is unavailable.

`migrate` converts only a v1 relay document to the v2 relay format. After apply,
`extensions.migration.mapping_review_required=true` means project field mapping
and reader cutover remain `UNVERIFIED`. It cannot import arbitrary business JSON
or verifier behavior. Keep the prior business authority until reviewed cutover;
pause ongoing edits and do not dual-write both authorities during that cutover.
