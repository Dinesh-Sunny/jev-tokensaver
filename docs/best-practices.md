# Best practices: robust, scalable, enhanceable, self-evolving

Two scopes:
- **A. This jev tool** (Claude Code + Cowork token saver) - status of each practice.
- **B. Jev inside your own apps** - the checklist to follow when you swap LLM calls for Jev. That's where most of the money is saved.

Legend: ✅ built in jev.py · ⏭ recommended next

---

## A. The jev tool

### 1. Robustness - it must never block Claude
| Practice | Why | Status |
|---|---|---|
| Retries with exponential backoff + jitter, honour `Retry-After` | rate limits (429) and overload (529) are normal and temporary | ✅ SDK RetryPolicy, 4 retries, 90 s total budget |
| Per-request isolation | one failed chunk/file/row must not sink the batch | ✅ partial results + "NOT searched / ERROR" lists |
| Graceful degradation (FALLBACK) | Claude always gets a usable instruction, never a crash | ✅ `FALLBACK:` text + exit code 2 |
| Circuit breaker | stop waiting on a dead service on every call | ✅ 3 real outages (connection/5xx/auth) in 2 min -> paused 5 min; 429s never trip it |
| Don't trip on your own bugs | bad question JSON is not an outage | ✅ validated locally -> `QUESTION ERROR` (exit 3), no API call; oversize chunks split + retried |
| Kill switch | turn Jev off instantly without uninstalling | ✅ `JEV_DISABLE=1`, file `~/.config/typesafe/disabled` (no restart), per-folder `.jevoff` |
| Budget cap | a runaway `map` can't burn your credit | ✅ `JEV_DAILY_BUDGET_USD`, checked per live request (cache hits are free) |
| Input guards | stay inside Jev limits (255 options, 10 levels, 32k state) | ✅ token-aware chunks (≤20k est., CJK-safe), long lines split into segments, state/item caps with warnings |
| Secrets hygiene | your code's secrets must never leave the Mac | ✅ secret-file deny-list, dotfiles/.gitignore skipped, regex scrub of keys/tokens/JWTs/private keys; API key in a 600-mode file |
| Confidence surfaced, not hidden | a "probably not here" must lead Claude to check itself | ✅ verdict needs exists + where; "NOT" only when everything was searched, else "unclear" |
| Lossless by design | a filter must never hide something for good | ✅ `prune` keeps error lines + head/tail deterministically and saves the full log |

### 2. Scalability - cheap and fast as usage grows
| Practice | Status |
|---|---|
| Batch all questions about one state into one request (12x cheaper, 10x faster, same answers) | ✅ every tool |
| Bounded concurrency (public endpoint dislikes >8 parallel) | ✅ `JEV_WORKERS` |
| Answer cache keyed on resolved model version + question version + state | ✅ SQLite, 7-day TTL, pruned automatically |
| Send only relevant context (Jev suffers context rot) | ✅ chunking, excerpts, skip `node_modules`/`.git` |
| Results to file for big lists, summary to Claude | ✅ `map --out-csv`, `top_n` |
| MCP tools never block the server loop | ✅ async tools in worker threads |
| Fast CLI start | ✅ MCP imported lazily (0.09 s) |
| ⏭ Async client + streaming progress for 10k+ item maps | next, if map jobs get big |
| ⏭ Two-stage search for very large inputs (pick window, then lines; pick folder, then files) | next |

### 3. Enhanceability - easy to change safely
| Practice | Status |
|---|---|
| All knobs in one settings block; env overrides | ✅ top of jev.py |
| Question wording in one place (templates) | ✅ `find_questions`, `RANK_QUESTIONS` |
| Thresholds in data, not code | ✅ `tuning.json`, versioned history |
| Same core for CLI and MCP (one code path to fix) | ✅ |
| Offline test suite with a mocked API, incl. failure modes | ✅ `tests/` |
| Log the resolved model version | ✅ shown in `jev review` |
| ⏭ Pin `TYPESAFE_DEFAULT_MODEL=jev-1.13.0` once `jev eval` looks good | after your first eval |
| Golden eval set (`jev eval`): real cases with known answers; fit on half, validate on half | ✅ built - you supply 20-30 cases ([examples/golden.example.jsonl](../examples/golden.example.jsonl)) |

### 4. Self-evolving - learns from its errors
| Practice | Status |
|---|---|
| Every call logged with id, outcome, errors, cost, model | ✅ `calls` table |
| Feedback capture - misses only (cheap) + where the answer really was | ✅ `jev feedback` |
| Re-tuning with guardrails: ≥20/10 labels, ±0.05 per step, "NOT" can only get rarer, top_k never shrinks, current model only, auto-tune only proposes | ✅ `jev tune` |
| Tuning history (audit + rollback) | ✅ `tuning-history.jsonl` (delete `tuning.json` = back to defaults) |
| Failure-pattern review with concrete fixes | ✅ `jev review` (rate limits, auth/credit, bad questions, budget, model drift, low feedback) |
| ⏭ Weekly scheduled `jev review` summary sent to you | next - can be a scheduled task |
| ⏭ Query rewriting from misses (learn which phrasings fail, suggest better ones) | next, once there are ~50 "bad" labels |

---

## B. Jev inside your own apps (Next.js / Node, `@typesafe-ai/sdk`)

1. **Use code first.** Deterministic rules, maths, dates, counting stay in TypeScript. Jev only where judgment is needed.
2. **Decompose.** Many atomic questions (noul / choice / score) combined in code with weights you own - change a number, not a prompt.
3. **Fan out.** Ask every question you might need in one call, including speculative ones.
4. **Route on confidence.** High -> act automatically; medium -> confirm or flag; low -> human or Claude. Destructive actions need ~0.85-0.9.
5. **Cascade.** Jev first; escalate to Claude only when a check fires or confidence is low (SDE-cascade pattern - most of the big model's quality at a fraction of the cost).
6. **Shadow mode before switching.** Run Jev alongside the existing Claude call for a week, compare, then switch the route. Keep Claude as the fallback path.
7. **Questions + thresholds in one file per feature** (`jev-questions.ts`), reviewed like code.
8. **Pin the model version** in production; log `response.model` and `requestId`.
9. **Retries + timeouts + circuit breaker + kill switch** (feature flag) around every Jev call; fallback = old Claude path.
10. **Cache** by hash(model + state + questions).
11. **Treat state as untrusted** (prompt injection can move answers): explicit criteria, edge-case tests; use the guardrails cookbook pattern for user input.
12. **Golden tests in CI**: a labelled set per feature; fail the build if accuracy drops when wording or model changes.
13. **Feedback loop in-product**: log decisions + outcomes (user corrections, escalations), re-fit thresholds monthly from that data.
14. **Cost/latency dashboard**: tokens, $ per feature, p95 latency, fallback rate, confidence distribution.

15. **Calibrate on your data.** Audits found Jev's probabilities poorly calibrated out of distribution (confidence 1.0 on half of answers; yes/no sets summing to 125%). Treat them as rankings; fit thresholds on half your labelled data, validate on the other half; recalibrate (e.g. Platt scaling) with a few hundred labels before automating.
16. **Always offer an escape option** (`other` / `none`) in choices - Jev returns high confidence even on out-of-scope input.

See [design-review.md](design-review.md) for the community patterns, audits and sources.

Good first targets: intent routing in front of an LLM, triage of listings/tickets/emails, scoring of events or leads, news/signal classification, passage relevance for RAG.
