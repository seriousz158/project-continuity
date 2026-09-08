# Changelog

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
