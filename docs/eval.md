# Measuring accuracy with `jev eval`

Mocked tests prove the code works. They say nothing about how well Jev finds things **in your code**. Independent audits also found that Jev's probabilities aren't calibrated on unfamiliar data. So measure it.

## 1. Write a golden set (20-30 cases, ~15 minutes)

Each line of a `.jsonl` file is one case:

```json
{"find": "~/code/app/src/billing/invoice.ts", "query": "where is VAT added to the total?", "line": 212}
{"find": "~/code/app/src/billing/invoice.ts", "query": "where are refunds sent to Stripe?", "absent": true}
{"rank": "~/code/app", "pattern": "src/**/*.ts", "query": "where webhook signatures are verified", "file": "src/api/webhooks/stripe.ts"}
```

- `find` cases: the answer is at or near `line` (±2 lines counts), or `absent: true` if it's genuinely not in the file. Include **at least 10 answered and 6 absent** cases.
- `rank` cases: `file` is the right answer, relative to `root`.
- Use real questions you'd ask Claude, in your own words.

Start from [examples/golden.example.jsonl](../examples/golden.example.jsonl).

## 2. Run it

```bash
jev eval my-golden.jsonl
```

```text
jev eval: 26 cases (0 could not run), model jev-1.13.0
find: hit@1 15/18, hit@3 17/18, hit@8 18/18
rank: hit@1 5/8, hit@3 7/8, hit@10 8/8
thresholds fitted on half 1: absent=0.3, found=0.7
  half 2: wrongly 'NOT in file' on 0/9 answered cases
  half 2: wrongly 'answered' on 1/4 absent cases
```

(Illustrative output. Your numbers will differ.)

- **hit@k**: how often the answer was in the top *k* results. If hit@8 is low, raise `-k` or rephrase questions.
- **Fitted thresholds** come from one half of your cases and are checked on the other half, so they aren't graded on the same data they were fitted to.

## 3. Apply

```bash
jev tune          # applies the fitted values (bounded steps; see tuning-history.jsonl)
```

Re-run `jev eval` whenever the Jev model changes (`jev review` flags this), and consider pinning `TYPESAFE_DEFAULT_MODEL` once you're happy. Eval calls cost a few cents at most.

Please share your results in [Discussions](https://github.com/Dinesh-Sunny/jev-tokensaver/discussions). Real-world numbers help everyone.
