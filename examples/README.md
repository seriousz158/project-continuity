# Examples

`change.json` is a minimal typed partial update. It intentionally does not mark
the task done: completion also requires immutable passing evidence covering its
acceptance condition at the task's current generation.

Use it only with an active writer lease and a freshly read expected revision:

```bash
python scripts/write_current.py update --root <project> \
  --writer agent-a --expected-revision <n> --operation-id <unique-id> \
  --input examples/change.json
```

Before writing, preview the exact patch without touching anything:

```bash
python scripts/write_current.py capacity --root <project> \
  --writer agent-a --input examples/change.json
```

## Pointers, not payloads

`CURRENT.md` stays small by keeping identity and short summaries, not logs. Put
the detail in an evidence root and reference it:

```json
{
  "evidence": [
    {
      "id": "ev-r10-matrix",
      "task_id": "parser-tests",
      "check": "180-cell acceptance matrix",
      "result": "pass",
      "at": "2026-09-21T00:00:00Z",
      "ref": "r10/evidence/matrix/REPORT.md",
      "acceptance": ["Malformed input is rejected"]
    }
  ]
}
```

A relay object reference (for example `objects/evidence/<aa>/<sha>.json`) is
provable by the resolver; a plain path such as `r10/evidence/...` is only a
pointer and does not prove the content was verified. Keep test matrices, full
report bodies and seal member lists in the evidence root, and reference the
seal/manifest by identity. `capacity --long-record-threshold <bytes>` reports a
large field by collection/id/field/size without echoing its value.
