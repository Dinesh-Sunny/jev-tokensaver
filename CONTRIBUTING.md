# Contributing to jev-tokensaver

Thanks for helping. Bug reports, `jev eval` results from real repos, docs fixes and PRs are all welcome.

## Quick dev setup

```bash
git clone https://github.com/Dinesh-Sunny/jev-tokensaver && cd jev-tokensaver
uv run pytest                 # offline tests - the TypeSafe API is mocked, no key needed
uv run ruff check .
uv tool install --force .     # try your changes as the real `jev` command
jev doctor
```

You'll need [uv](https://docs.astral.sh/uv/). No other setup is required.

## How the code is laid out

| Path | What |
|---|---|
| `jev.py` | everything: CLI, MCP server, Jev calls, cache, safety, setup/doctor/uninstall (single file, PEP 723 header so `uv run --script jev.py` also works) |
| `skills/jev/SKILL.md` | the Claude skill (embedded into `jev.py`) |
| `hooks/alert-hook.sh` | the Claude Code alert hook (embedded into `jev.py`) |
| `install.sh` | the `curl \| bash` bootstrap - keep it thin; logic belongs in `jev setup` |
| `tests/test_jev.py` | offline tests with a mocked API, including failure modes |
| `examples/` | golden-set example for `jev eval` |

**After editing `SKILL.md` or `alert-hook.sh`, run `python scripts/sync_assets.py`.** CI fails if the embedded copies drift.

## Ground rules

1. **Fail open.** Any Jev problem must end in `FALLBACK:` (or a partial result that says what wasn't covered), never in a blocked task.
2. **Never leak secrets.** Anything sent to TypeSafe goes through the deny-list and `_scrub`. Add a test for new inputs.
3. **Deterministic first.** If code can decide it (regex, counting, dates), don't ask Jev.
4. **Question wording changes need evidence.** If you change a question template (`find_questions`, `RANK_QUESTIONS`), bump `QUESTIONS_VERSION` and include `jev eval` numbers before/after in the PR.
5. **Be honest about savings.** Don't inflate the "tokens saved" estimate.
6. Keep `install.sh` passing `shellcheck -S warning`, and keep `ruff check` clean.

## Releasing (maintainers)

1. Update `CHANGELOG.md`, and bump `version` in `pyproject.toml` and `VERSION` in `jev.py` (a test checks they match).
2. `git tag vX.Y.Z && git push --tags`. The release workflow runs the tests and publishes a GitHub Release with notes taken from the changelog.

## Code of conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind.
