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
