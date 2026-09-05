# Project Continuity

A small, standard-library-only Skill and CLI for authoritative current project progress
across coding agents. The v2 format stores authoritative JSON and a derived
human-readable view in `.relay/CURRENT.md`, protected by revision checks, writer
leases, operation receipts, local locks, atomic writes, and history snapshots.

**Version:** `0.1.0-rc.1` release candidate. Release gates are still pending.
The repository targets macOS, Linux, and Windows with Python 3.11, 3.12, and
3.13; this statement describes the CI matrix, not completed verification on all
nine combinations. No claim is made that live clients or existing projects have migrated.

## Why

Progress notes tend to become stale prose or competing sources of truth. After
explicit v2 initialization, `.relay/CURRENT.md` is the sole current
project-progress authority for a new project. The built-in migration converts
only the v1 relay format; it does not migrate arbitrary business JSON or
verifiers. Existing projects retain their prior business authority until field
mapping and reader cutover are reviewed and completed. Historical evidence and release, deployment, or experiment
artifacts stay separate; a done task does not imply those outcomes. The tool does
not run a watcher, commit, push, call a model, or perform external work.

## Quick start

Use the script from a checkout or installed Skill directory:

```bash
# Only an explicit init creates .relay/
python scripts/write_current.py init --root /path/to/project
python scripts/write_current.py status --root /path/to/project
python scripts/write_current.py validate --root /path/to/project
```

Read the revision from `status`, then acquire a lease. Every mutation needs a
fresh expected revision, stable writer ID, and unique operation ID.

```bash
python scripts/write_current.py resume --root /path/to/project \
  --writer agent-a --expected-revision 0 --operation-id resume-001

python scripts/write_current.py update --root /path/to/project \
  --writer agent-a --expected-revision 1 --operation-id update-001 \
  --input examples/change.json

python scripts/write_current.py save --root /path/to/project \
  --writer agent-a --expected-revision 2 --operation-id save-001 \
  --input - < another-change.json
```

`save` releases the lease. The default lease is 30 minutes. `--allow-drift` is
an explicit acknowledgement after Git drift review, not an automatic override.
See [the command reference](references/commands.md) and
[the protocol](references/protocol.md).

## Safety and limitations

- Once explicitly initialized or enabled, agents update continuity at task
  start, task completion, and blocker changes; `save` releases the lease. User
  manual-only overrides are honored. There is no per-message update or daemon.
- v1 documents stay read-only until an explicit `migrate --apply`; migration is
  a dry run by default, and apply requires the preview's `source_sha256`. Apply
  sets `extensions.migration.mapping_review_required=true`: report this as
  `UNVERIFIED`, not as completed project migration. Pause progress edits during
  cutover and never dual-write the old authority and `CURRENT.md`.
- `recover` works only with an expired lease and a recorded reason.
- The document limit is 64 KiB. Obvious secrets and unsafe paths are rejected,
  but `.relay/` must never be used as a credential store.
- OS file locks only coordinate local processes. They are not distributed locks.
- Git capture hashes raw index/tree and worktree bytes without invoking clean
  filters, with a 64 MiB total limit. Symlinks and submodules are rejected;
  CRLF or clean-filter repositories may be conservatively reported as dirty.
- The default `.relay/.gitignore` keeps runtime data local. If Git sharing is
  explicitly selected, share only `CURRENT.md` and the intentional ignore
  configuration, never the whole `.relay/` directory.

## Installation

Extract the versioned archive, then copy the complete `project-continuity`
directory to the Skills destination used by your client. Do not copy individual
files. Determine that destination from the client's current configuration; this
project does not assume a global path. If a destination directory already
exists, stop: inspect and back it up to a separate timestamped path before any
replacement, and never merge or overwrite it implicitly. Restart or reload the
client only when safe, then verify Skill discovery and run the tests from the
installed directory.

## Development and packaging

```bash
python -m unittest discover -s tests -v
python scripts/package_skill.py /tmp/project-continuity-v0.1.0-rc.1.zip
```

The packager uses an explicit repository allowlist, sorted archive records, fixed
metadata, and produces a SHA-256 sidecar. It refuses to overwrite either output.
The archive excludes Git data, caches, relay state, generated archives, and
private files. See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
and [NOTICE](NOTICE).

## License

MIT. Copyright (c) 2026 seriousz158. See [LICENSE](LICENSE). The
[NOTICE](NOTICE) conservatively preserves attribution for potentially reused
portions influenced by `liu676767/codex-project-checkpoint-memory`.
