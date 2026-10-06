# Examples

- **[golden.example.jsonl](golden.example.jsonl)** - template for `jev eval`; see [docs/eval.md](../docs/eval.md).
- **[event-triage.json](event-triage.json)** - questions for `jev map` to triage a list of events:

```bash
jev map --items events.json --id-field url --sort-by fit --out-csv ranked.csv -q @examples/event-triage.json
```
