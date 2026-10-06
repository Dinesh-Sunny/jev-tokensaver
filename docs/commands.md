# Commands

Exit codes for every command: `0` ok · `1` crashed/usage · `2` FALLBACK (do it the normal way) · `3` bad input (`QUESTION ERROR` / `ERROR`) · `4` account alert (`JEV ALERT`).

## Saving tokens

| Command | Example | Notes |
|---|---|---|
| `jev prune "question"` | `set -o pipefail; npm test 2>&1 \| jev prune "which tests failed and why"` | reads stdin; output ≤120 lines passes through; full log path in the footer |
| `jev find FILE "question" [-k N] [-c N]` | `jev find server.log "why did the webhook fail?"` | `-k` hits (default 8, tunable), `-c` context lines (default 2) |
| `jev rank "what" [--root DIR] [--pattern GLOB] [-k N] [FILES…]` | `jev rank "pricing table component" --root . --pattern "src/**/*.tsx"` | refuses `/` and your home folder as root; max 400 files (warns beyond that) |
| `jev map -q QUESTIONS --items FILE [--id-field F] [--sort-by Q or Q=label] [-n N] [--out-csv F] [--context TEXT]` | see below | `.json` / `.jsonl` / `.csv` / `.tsv` / `.txt`; max 2,000 items (warns) |
| `jev ask -q QUESTIONS (--state TEXT \| --state-path FILE \| --state -)` | `git diff \| jev ask -q @q.json --state -` | `-q @file.json` reads questions from a file |

### Question format (map / ask)

```json
{
  "fit":   {"type": "noul",   "instructions": "Is this a free in-person AI event in London?"},
  "kind":  {"type": "choice", "instructions": "What kind of event is this?",
            "criteria": {"talk": null, "workshop": null, "hackathon": null, "other": null}},
  "depth": {"type": "score",  "instructions": "How technical is it?",
            "criteria": ["No technical content", "Some technical talks", "Hands-on, for engineers"]}
}
```

- **noul** returns P(yes). Optional `criteria: {"true": "...", "false": "..."}`.
- **choice** has 2-255 options. Always include `other` or `none`, because Jev must pick one.
- **score** has 2-10 levels, ordered low to high and described as situations. The result is a level 0..n.

Rules of thumb: one snap judgment per question; put the full question in `instructions`; keep maths, dates and counting in code; ask many questions at once.

## Accuracy & learning

| Command | What |
|---|---|
| `jev eval golden.jsonl [--apply]` | hit@k on your cases; thresholds fitted on one half, validated on the other ([eval.md](eval.md)) |
| `jev feedback ID bad\|absent\|good [--line N] [--file F] [--note TEXT]` | record a miss (`last` = latest unlabelled call in the past hour) |
| `jev tune [--dry-run]` | apply thresholds fitted from eval + feedback (bounded) |
| `jev review [--days N]` | failure patterns and recommendations |
| `jev usage [--days N]` | spend and estimated net Claude tokens saved |

## Account & installation

| Command | What |
|---|---|
| `jev setup [--yes] [--no-hook] [--no-desktop] [--claude-md] [--new-key] [--skip-check]` | guided setup; idempotent; backs up files |
| `jev doctor [--offline]` | checks everything and prints a fix for each problem |
| `jev check` | live key + credit test (also clears a fixed alert and resets the breaker) |
| `jev key` | save a new API key (hidden input) and test it |
| `jev status` | current account alert, if any |
| `jev off` / `jev on` | pause / resume Jev (Claude works normally while it's off) |
| `jev dismiss` | hide the current alert for 12 hours |
| `jev uninstall [--yes] [--purge] [--keep-cli]` | remove everything; `--purge` also deletes the key, history and cache |
| `jev serve` | run the MCP server (what Claude Desktop launches) |

## MCP tools (Cowork / Claude Desktop)

| Tool | Same as |
|---|---|
| `jev_prune(path, query)` | `jev prune` on a log/output file |
| `jev_find(path, query, top_k, context_lines)` | `jev find` |
| `jev_rank(query, root, pattern, paths, top_k)` | `jev rank` |
| `jev_map(questions, items_path, …)` | `jev map` |
| `jev_ask(questions, state, state_path)` | `jev ask` |
| `jev_feedback(call_id, outcome, answer_line, answer_file, note)` | `jev feedback` |
| `jev_status(days)` | `jev usage` + `jev review` |
| `jev_control(action)` | `check` / `off` / `on` / `dismiss` |
