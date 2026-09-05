# Changelog

## 0.1.0-rc.1 - Unreleased

Release candidate pending project gates.

- Add the v2 structured JSON state and derived Markdown view.
- Add explicit init, read-only status/validation, leased writes, revision CAS,
  operation receipts, dry-run migration, and expired-lease recovery.
- Add deterministic source packaging and cross-platform CI definitions.
- Capture Git baselines from raw index/tree and worktree bytes with a bounded,
  fail-closed fingerprint instead of commands that may execute clean filters.

No claim is made that all CI jobs, live clients, or existing-project migration have been
completed.
