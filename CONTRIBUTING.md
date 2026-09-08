# Contributing

Use Python 3.11 or newer and the standard library only. Keep changes scoped,
preserve `.relay/CURRENT.md` as the sole current-progress authority after v2 initialization, and add a regression
test for behavior changes.

```bash
python -m unittest discover -s tests -v
python scripts/package_skill.py /tmp/project-continuity-v0.2.0.zip
```

Do not commit generated archives, `.relay/` state, caches, credentials, or
private client data. Before proposing a release, inspect the final diff and
report actual platform coverage rather than inferring it from the CI matrix.
Publishing a release must wait for user confirmation of the final archive
manifest and destination; building an archive is not publication.
