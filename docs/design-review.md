# Design review - research, devil's advocate vs angel's advocate, and what was fixed

Date: 2026-10-06. Inputs: an independent code review of v0.2 (each claim reproduced against a mocked API),
plus about 25 public Jev projects, audits and articles (sources at the end).

---

## 1. What people are doing with Jev (patterns)

| Pattern | Who does it | Fits our setup? | Status in jev |
|---|---|---|---|
| **Prune long tool/Bash output** before it reaches the agent | jev-pruner, winnow, fast-jev-compaction | Yes - test/build logs are the biggest context hogs | ✅ `jev prune` (new) |
| **Classify first, read selectively** | jev-sift | Yes | ✅ `rank`, `map` |
| **Semantic search by behaviour, not strings** | jevgrep | Yes | ✅ `find` |
| **Verify / screen / audit** (claims vs evidence, injection screening) | jkudish/jev-mcp | Partly | `ask` covers it; dedicated tools later if needed |
| **PreToolUse safety gates, Stop-hook "is it really done?" checks** | the-jev-enator, jev-guard, clear-head, jev-preflight | Robustness rather than tokens | ⏭ optional next |
| **Per-turn model routing** (Haiku/Sonnet/Opus) | jev-router, agent-router | Biggest $ lever on API billing; intrusive, prompts go to TypeSafe | ⏭ only if you move to API billing |
| **History compaction proxies** | Yoshi | Intrusive (proxy in front of Claude) | ✗ not now |
| **Shadow mode + golden eval + recalibration** | beri.net, jev-align, jeval, JevScope | Yes - the community's #1 lesson | ✅ `jev eval` (new) |
| **Decompose one big question into atomic ones** | TypeSafe docs, beri.net (62.6% → 95%) | Yes | ✅ skill + map/ask guidance |
| **Deterministic policy owns the decision; fail open** | Canny, pi-jev, riz1.dev | Yes | ✅ everywhere |

### What the audits warn about (and what we changed because of it)
- **Calibration is weak on new data.** Reported findings: a confidence of exactly 1.0 on 51.5% of answers; 8 yes/no probabilities summing to 125%; a calibration error 4.4x the noise floor; P(heads) = 0.92 for a fair coin. → Results are labelled "rankings", thresholds are tuned only from your labels (`eval` / feedback), in small steps.
- **High confidence on out-of-scope input.** → find also checks the "where" probability before saying "answered"; the map/ask validator warns when a choice has no `other`/`none` option.
- **"Mocks prove code, not accuracy."** → `jev eval` on your own files, and `review` nags until you have run it.
- **Pin versions; aliases move.** → The cache and tuning are keyed to the resolved model version; review flags version changes.
- **Hooks without fail-open freeze the agent.** → Every path returns FALLBACK and the work continues.

---

## 2. Devil's advocate vs angel's advocate

### Q1. Does jev actually save Claude tokens?
**Devil:** Tool schemas sit in every Cowork context (about 2.2k tokens). The footers and feedback calls cost tokens. At a 300-line file it barely breaks even. `map`/`ask` on text Claude has already read is a net loss. The "saved" number counted whole files Claude would never have opened.
**Angel:** For big files, long logs and long lists, the saving is large. One 1,500-line file dropped from about 31k tokens to about 420 for $0.0014.
**Verdict and fixes:** Use it only where it clearly wins.
- The threshold is raised to ~1,000 lines / 40KB with no grep keyword.
- The skill states the rule: only data that has *not* passed through Claude.
- Inline `items_json` and `--state` claim 0 savings.
- "Saved" is now net and conservative: capped at Claude's read limit, counted only when find actually located the answer, with call overhead subtracted. rank counts only files 3-5 that Claude would otherwise have opened.
- The footer is shortened to `[jev id · $ · saved]`.
- MCP schemas are cut by half (8.8k → 4.4k characters; 7 tools instead of 8).
- The real test is an A/B comparison on your own tasks (see "Next").

### Q2. Can it give confidently wrong answers?
**Devil (verified):** Lines were cut at 300 characters, so a minified bundle gave "probably NOT in this file".
**Fix:** Long lines are split into 300-character segments (`L00012s3`) that all map back to the same line. Characters beyond 400k per line are reported as not searched. The "NOT" verdict only appears when everything was searched; otherwise the result says "unclear". "answered" also needs the best line to score at least 0.3.

### Q3. Could it leak secrets?
**Devil (verified):** `rank` sent `.env` and `id_rsa` to TypeSafe. A global CLAUDE.md rule meant client/NDA code went too.
**Fixes:**
- A deny-list for secret-looking files: `.env*`, keys, `*.pem`, credentials, tfstate, databases.
- Dotfiles and vendor folders are skipped, and `.gitignore` is respected via `git ls-files`.
- A regex scrub removes Stripe/AWS/GitHub/Slack/Google keys, JWTs, private keys and `password=` values. Line counts are kept so line numbers stay correct.
- find and ask refuse secret files.
- rank refuses `/` and your home folder as root.
- A `.jevoff` file excludes any folder tree.
- The installer explains this before adding the global rule.

### Q4. Do failures cascade?
**Devil (verified):** Three malformed questions paused Jev for 5 minutes, and Claude was told the service was down.
**Fixes:**
- Questions are validated locally, giving `QUESTION ERROR` (exit 3) with no API call.
- The breaker counts only real outages (connection, 5xx, auth/credit) by exception type. 429 rate limits never trip it.
- A chunk rejected for size is split in half and retried.

### Q5. Can it overflow Jev's input limit?
**Devil:** Chunks were capped by characters, so CJK/Indic text could reach about 58k tokens against a 32k limit.
**Fix:** Token estimate = ASCII/3.2 + non-ASCII × 1.3. Chunks are capped at 20k estimated tokens, and the small last chunk is rebalanced. ask state and map items are capped at 24k tokens, with a warning.

### Q6. Is the self-tuning safe?
**Devil (verified):** 5 easy labels moved `absent` 0.35 → 0.50 and `top_k` 8 → 3. That feeds on itself: once Claude stops looking, misses never get labelled.
**Angel:** The formula direction was right; the problem was sample size and selection bias.
**Fixes:**
- Tuning needs ≥20 answered and ≥10 absent cases, or ≥10 ranks.
- Thresholds move at most ±0.05 per tune. `absent` is capped at 0.35, so tuning can only make "NOT" rarer.
- `top_k` never drops below its default.
- Only labels from the current model version are used.
- Auto-tune only **proposes**; you apply it with `jev tune`.
- Feedback is now for misses only, which costs fewer tokens.
- The unbiased data source is `jev eval`: fit on one half, validate on the other.

### Q7. Does the MCP server behave well in Cowork?
**Devil (verified):**
- Synchronous tools blocked the event loop.
- `call_id: 42` and `questions` as an object were rejected.
- Cowork's `/sessions/.../mnt/<folder>` paths don't exist on the Mac.

**Fixes:**
- Tools are async and run in worker threads.
- Inputs accept `int | str` and `dict | str`.
- VM paths are mapped to the matching Mac folder (via `JEV_ROOTS` or a search of your home/Documents/CodeProjects/Desktop).
- Every error comes back as text.

### Q8. Smaller correctness bugs (all fixed and tested)
- rank silently dropped files after 400. It now warns, prunes the walk, and judges large files on their head *plus* their definition lines.
- rank feedback compared relative paths with absolute ones, and `endswith` matched `data.ts` to `a.ts`. It now resolves both paths and matches on a `/` boundary.
- `splitlines()` shifted line numbers at form-feed characters. It now splits on `\n` only.
- A BOM in a CSV broke `--id-field`. Files are now read as `utf-8-sig`.
- A JSON object input took the first list it found. It now prefers `items`/`events`/`data`/... or the longest list of objects.
- A scalar JSON value was iterated character by character.
- Sorting by a choice question was meaningless. It now uses `choice=label`, and an unknown `sort_by` gives a warning.
- `ask` could hang on stdin. stdin is now read only with `--state -`, and an empty state is refused.
- `map` truncated silently. It now warns.
- `last` could grab another session's call. It now means the latest call with no feedback, within the past hour.
- The cache served stale answers after a model change. The cache key now includes the resolved version, the questions version, and a 7-day TTL with pruning.
- **A cache race we found while testing:** parallel requests could store answers under a stale key the first time the version became known. Fixed.
- CLI startup imported FastMCP (0.7s). It is now imported lazily: `jev --version` takes 0.09s.
- The kill switch couldn't be flipped for the MCP server. The file `~/.config/typesafe/disabled` now works without a restart.
- install.sh:
  - It now asks you to quit Claude Desktop before editing its config, and writes the config atomically.
  - `umask` is applied in a subshell.
  - A failed warm-up gives a clear message.
  - It uses brew rather than `curl | sh`.

### Q9. What the angel's advocate rightly defended
- The core design is TypeSafe's own semantic-find cookbook, at fractions of a cent.
- It fails open everywhere: the worst case is a little overhead, never a broken task.
- SQLite held up under 6 processes × 6 threads.
- Prompt injection through file text matters less here, because Jev only outputs numbers and Claude reads the same lines anyway.
- One second of uv start-up is small next to a Claude turn.

### Q10. What is still open (honest list)
- **Tests use a mocked API.** They prove the code; real-world accuracy comes from `jev eval` on your own code (please share results).
- **Jev's accuracy on code** (rather than prose) is unproven, which is why `jev eval` exists. Write 20-30 cases from your repos.
- Budget accounting misses spend from a process killed mid-run, such as one hitting Claude Code's 2-minute Bash timeout. That spend is small, and the console billing page is the source of truth.
- Implicit feedback (a hook that watches which lines Claude reads after `find`) would remove manual labelling. It was left out because a hook on every Read adds latency.
- An A/B test on real tasks, with and without jev, comparing session token totals, is the only trustworthy measure of savings.

---

## Sources
- [InfoQ - TypeSafe AI releases Jev](https://www.infoq.com/news/2026/10/typesafe-ai-jev-released/)
- [Jev × Claude Code: four integration paths](https://redreamality.com/blog/jev-claude-code-10x-and-25-lines/)
- [madewithjev - 15 Jev MCP servers](https://madewithjev.com/jev-mcp)
- [awesome-jev](https://github.com/AppitStudio/awesome-jev)
- [jkudish/jev-mcp](https://github.com/jkudish/jev-mcp)
- [kbhuw/jev-sift](https://github.com/kbhuw/jev-sift)
- [Alex Molas - Jev can't be calibrated](https://www.alexmolas.com/2026/09/23/jev-cant-be-calibrated.html)
- [Dr Borchers - field test on fresh data](https://www.drborchers.com/en/blog/jev-field-test/)
- [beri.net - 62.6% asked once, 95% split five ways](https://www.beri.net/article/typesafe-jev-typed-decision-model-calibration-decomposition-shadow-eval)
- [OrcaRouter - what the audits found](https://www.orcarouter.ai/blog/jev-typesafe-system-one-what-we-know)
- [riz1.dev - how to use Jev properly](https://riz1.dev/en/blog/how-to-use-jev-properly)
- [DEV - 25 lines of Python](https://dev.to/jamilxt/typesafes-jev-costs-400x-less-than-an-llm-these-25-lines-of-python-do-the-same-job-for-free-3ddd)
- [Tom's Hardware - Jev claims](https://www.tomshardware.com/tech-industry/artificial-intelligence/typesafe-ais-jev-offers-an-alternative-to-llms-that-claims-to-be-193x-faster-and-445x-cheaper-system-one-type-model-is-bespoke-for-probabilistic-decision-making)
- [TypeSafe docs - Jev with coding agents](https://docs.typesafe.ai/introduction/coding-agents)
