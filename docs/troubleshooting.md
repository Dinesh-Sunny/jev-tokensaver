# Troubleshooting

**Always start with `jev doctor`.** It checks everything and prints a fix for each problem.

| Symptom | Fix |
|---|---|
| `jev: command not found` | `uv tool update-shell`, then open a new terminal. Or add `$(uv tool dir --bin)` to your PATH. |
| `FALLBACK: Jev not used (no TypeSafe API key …)` | `jev key` |
| `JEV ALERT … out of credit` | add credit at [console.typesafe.ai/settings/billing](https://console.typesafe.ai/settings/billing), then `jev check` |
| `JEV ALERT … rejected the API key` | create a new key at [console.typesafe.ai/settings/keys](https://console.typesafe.ai/settings/keys), then `jev key` |
| `FALLBACK … paused after repeated failures` | wait 5 minutes, or fix the cause and run `jev check` (it resets the pause immediately) |
| `FALLBACK … daily Jev budget … reached` | raise `JEV_DAILY_BUDGET_USD`, or wait until tomorrow |
| `FALLBACK … turned off for this folder (.jevoff)` | intended; delete the `.jevoff` file to allow Jev there |
| `FALLBACK … refusing to send a likely-secret file` | intended; read secret files normally (they never go to TypeSafe) |
| `QUESTION ERROR …` | your questions JSON is invalid; the message says what to fix (Jev wasn't called) |
| Lots of rate-limit errors in `jev review` | `export JEV_WORKERS=3` |
| Cowork doesn't show the jev tools | quit and reopen Claude Desktop; `jev doctor` should say "Claude Desktop / Cowork: jev registered"; check the `command` path exists |
| Cowork says "Not found: /sessions/…/mnt/…" | add your project's parent folder to `JEV_ROOTS` in the Desktop config ([cowork.md](cowork.md)) |
| `find` keeps missing | phrase the query as one concrete question; raise `-k`; add the case to your golden set and run `jev eval` |
| The installer can't ask questions (no terminal) | run with `--yes` and `JEV_API_KEY=…`, or run `jev setup` afterwards in a terminal |
| I want to undo the installer's changes | `jev uninstall` (backups `*.bak.<timestamp>` sit next to each changed file) |

Still stuck? [Open an issue](https://github.com/Dinesh-Sunny/jev-tokensaver/issues/new/choose) with the output of `jev doctor --offline`.
