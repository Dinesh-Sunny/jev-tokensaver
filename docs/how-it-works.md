# How it works

## The idea

Claude is excellent at reasoning but expensive at *reading*. Jev (TypeSafe's "System One" model) is fast and very cheap at snap judgments such as *"which of these 200 lines answers this question?"*, but it never writes text. jev-tokensaver uses Jev as a **pre-reader**: Jev decides what to show, and Claude reads only that.

## What happens on a call

```mermaid
sequenceDiagram
    participant C as Claude
    participant J as jev (your machine)
    participant T as TypeSafe Jev API
    C->>J: jev find big.ts "where is VAT added?"
    J->>J: kill switch / .jevoff / secret-file checks
    J->>J: split into ≤200-line, ≤20k-token chunks (long lines → 300-char segments)
    J->>J: scrub secrets (keys, tokens, JWTs, private keys)
    J->>J: cache lookup (model version + question version + text)
    J->>T: per chunk: "which line answers…?" (choice) + "does any line answer…?" (yes/no)
    T-->>J: probabilities
    J->>J: score = P(answered) × P(this line); verdict; budget + breaker bookkeeping
    J-->>C: verdict + top lines with context + footer [jev id · $ · tokens saved]
```

If anything fails (no key, out of credit, network, rate limit, kill switch, budget), Claude gets `FALLBACK: …` and reads the file the normal way. If only some chunks fail, the output lists the line ranges that weren't searched.

## The commands, in one line each

- **prune** - keeps error-looking lines plus the first 5 and last 30 lines deterministically, adds Jev's top hits with context, and saves the full text to `~/.cache/jev/prune/`. Output shorter than 120 lines passes through untouched (no API call).
- **find** - the flow above. Says "probably NOT in this file" only when every line was searched; otherwise "unclear".
- **rank** - one yes/no question per file, judged on its path plus the first ~5k characters and the definition lines (`def`, `class`, `function`, `export`…) further down. Respects `.gitignore` and skips dotfiles, secrets and vendor folders.
- **map** - your typed questions are asked once per item of a list file. All questions are batched in one request per item, since extra questions are nearly free.
- **ask** - a single call; questions are validated locally first.

## Claude Code vs Cowork

| | Claude Code | Cowork / Claude Desktop |
|---|---|---|
| How Claude calls jev | `jev …` in Bash (CLI) | MCP tools (`jev serve`, launched by Claude Desktop on your Mac) |
| Context cost when unused | none | the tool list (~1k tokens) |
| Why | pipes, scripts, zero overhead | Cowork's cloud sandbox can't reach TypeSafe; your Mac can |
| Account alerts | ⚠️ hook warning on your next message, plus the alert in results | alert in results, plus a macOS notification |

## Safety mechanisms

| Mechanism | What it does |
|---|---|
| Fail-open | every failure becomes `FALLBACK` or a partial result that says what's missing |
| Circuit breaker | 3 real outages (connection, 5xx, auth/credit) within 2 minutes pause Jev for 5 minutes; your own malformed questions and 429s never trip it; `jev check` resets it |
| Budget | the daily $ cap is checked before each live request (cache hits are free) |
| Secrets | file deny-list + regex scrub; line numbers stay correct after redaction |
| Cache | 7 days, keyed to the *resolved* model version and question version, so a model upgrade never serves stale answers |
| Local validation | choice 2-255 options, score 2-10 levels, instructions required → `QUESTION ERROR` with no API call |

## Learning from mistakes

Every result has an id. When Jev misses, Claude records it in one line (`jev feedback <id> bad --line N`). `jev eval` runs your golden set. `jev tune` fits thresholds from both sources, with guardrails: minimum sample sizes, ±0.05 steps, "not in this file" can only get rarer, and current-model labels only. Automatic tuning only *proposes*; you apply it. Full reasoning: [design-review.md](design-review.md).
