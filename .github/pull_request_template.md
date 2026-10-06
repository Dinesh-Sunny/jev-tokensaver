## What & why

<!-- One or two sentences. Link the issue if there is one. -->

## Checklist

- [ ] `uv run pytest` passes (and I added tests for new behaviour / failure modes)
- [ ] `uv run ruff check .` is clean; `shellcheck -S warning install.sh` if I touched it
- [ ] Ran `python scripts/sync_assets.py` if I edited `skills/jev/SKILL.md` or `hooks/alert-hook.sh`
- [ ] Still fails open and never sends secrets (see CONTRIBUTING ground rules)
- [ ] If I changed question wording: bumped `QUESTIONS_VERSION` and included `jev eval` before/after
- [ ] Updated `CHANGELOG.md` (Unreleased) and docs if user-facing
