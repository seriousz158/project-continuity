# Changelog

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
