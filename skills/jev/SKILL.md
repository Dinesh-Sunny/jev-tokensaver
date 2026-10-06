---
name: jev
description: Save Claude tokens with TypeSafe Jev - prune long command output, find the relevant lines in big files, rank many files, triage long list files - instead of reading everything into context.
---

# Jev token saver

<!-- From jev-tokensaver: https://github.com/Dinesh-Sunny/jev-tokensaver -->

Jev (TypeSafe) is a fast, very cheap judgment model ($0.042 per 1M input tokens, ~100-500 ms). It never writes
text; it answers typed questions with probabilities. Here it works as a pre-reader: it decides what to SHOW you;
nothing is deleted (full text stays on disk).

## How to call it
- **Claude Code:** the `jev` command in Bash (`jev <cmd> -h` for flags).
- **Cowork / Claude Desktop:** MCP tools `jev_prune`, `jev_find`, `jev_rank`, `jev_map`, `jev_ask`, `jev_feedback`, `jev_status`, `jev_control`.
- Paths must be on the user's machine; absolute is safest (Cowork `/mnt/<folder>/...` paths are mapped automatically).
- If `jev` is missing or broken, carry on normally and suggest `jev doctor` (or the install command from the repo).

## The one rule: it only saves tokens if the data has NOT passed through you yet
Good: output piped straight in, files on disk you haven't opened, list files written by a script.
Bad: re-sending text you already read, or writing items out yourself (`items_json`, `--state "<text>"`) - that costs MORE.

## When to use it
| Situation | Do this |
|---|---|
| A command will print a lot (tests, build, install, logs, `git log`) | `set -o pipefail; npm test 2>&1 \| jev prune "which tests failed and why"` |
| A text file > ~1,000 lines / 40KB, and no obvious keyword to grep | `jev find FILE "plain-language question"` - hits are shown; read beyond them only if they don't answer it |
| More than ~5 files might be relevant, or you don't know the keyword | `jev rank "what I'm looking for" --root /abs/project --pattern "src/**/*.ts"` then open the top 2-3 |
| A list FILE of many items to filter/score (events, jobs, emails, CSV rows) | `jev map --items FILE -q '{...}' --sort-by ID --out-csv out.csv`; read only the top rows |
| A yes/no or category call on something on disk or piped (`git diff \| jev ask -q '{...}' --state -`) | `jev ask` |

## When NOT to use it
- Small files, or when a grep keyword exists -> grep + targeted read is cheaper.
- Exact strings, maths, counting, dates, generation, multi-step reasoning -> do it yourself / in code.
- Files you already have in context. Images/PDFs (text only).
- Secret files (.env, keys) - jev refuses them anyway and scrubs secrets from everything it sends.
- Folders containing a `.jevoff` file (client/NDA code) - jev returns FALLBACK; work normally.

## Reading results
- `JEV ALERT ...` (exit 4) - or a JEV ALERT note added to the user's message - means an account problem (out of credit, key expired/rejected, no key).
  START your reply with it: one or two plain lines saying Jev has stopped and why, then the numbered options it lists. Then carry on with the task without Jev.
  If the user picks an option you can run: `jev check` (after they top up), `jev off`, `jev on`, `jev dismiss` (MCP: `jev_control`).
  Never ask for the API key in chat - the user runs `jev key` in Terminal (hidden input).
- `FALLBACK: ...` (exit 2): Jev wasn't used (down, paused, budget, no key, switched off). Work normally - never stop the task.
- `QUESTION ERROR` / `ERROR` (exit 3): fix your input; Jev was not called.
- `WARNING: ... NOT searched / NOT ranked / ERROR rows / truncated`: use the partial result, cover the listed parts yourself.
- find verdicts: "answered" -> trust the hits; "partially" / "unclear" -> check, then grep/read; "probably NOT" -> likely absent, but grep if it matters.
- Numbers are rankings, not exact probabilities (Jev is not calibrated on this data until `jev eval` is run).
- prune: the footer gives the full log path - read it if something seems missing.

## Writing questions (map / ask)
`{"id": {"type": "noul|choice|score", "instructions": "the full question", "criteria": ...}}`
1. One snap judgment per question; split big judgments into several small ones and combine yourself (decomposed questions score far better than one big question).
2. The full question goes in `instructions` (the id is not sent). Be literal; put edge cases in criteria.
3. noul: yes = the thing is true; optional criteria `{"true": "...", "false": "..."}`.
4. choice: `{"option": "meaning" or null}`, 2-255 options; always include `"other"` or `"none"`.
5. score: ordered list of 2-10 levels, each describing a situation; results are levels 0..n.
6. Ask many questions in one call - extra questions are nearly free.

Example (Luma events saved to a file by a script):
```bash
jev map --items events.json --id-field url --sort-by fit --out-csv ~/Desktop/events-ranked.csv -q '{
 "fit": {"type":"noul","instructions":"Is this a free, in-person AI or software engineering event in London?"},
 "kind": {"type":"choice","instructions":"What kind of event is this?","criteria":{"talk":null,"workshop":null,"hackathon":null,"networking":null,"other":null}},
 "depth": {"type":"score","instructions":"How technical is the content?","criteria":["No technical content","Some technical talks for a general audience","Hands-on or deeply technical, aimed at engineers"]}}'
```

## When Jev was wrong - one short line, misses only
- find missed: `jev feedback <id> bad --line <where it really was>`; answer not in the file: `jev feedback <id> absent`
- rank missed: `jev feedback <id> bad --file <right file>`; prune hid what you needed: `jev feedback <id> bad`
The id is in the result footer `[jev id=42 ...]` (or use `last`). Don't log successes - it costs tokens.
Tuning only proposes changes; the user applies them (`jev tune`), and `jev eval` on a golden set is the real accuracy check.
