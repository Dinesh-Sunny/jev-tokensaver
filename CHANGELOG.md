# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.4.0] - 2026-10-06
First public release.

### Added
- **One-line installer** (`curl … | bash`): installs `uv` if needed (with consent), installs the `jev` command with `uv tool install`, then runs the guided `jev setup`. Works under `curl | bash` (prompts read from the terminal), with `--yes` for no prompts, `JEV_VERSION` to pin a release and `--uninstall`.
- `jev setup`: 5 clear steps; backs up every file it changes; idempotent; flags `--no-hook`, `--no-desktop`, `--claude-md`, `--new-key`, `--skip-check`.
- `jev doctor` (health check with a fix for every problem) and `jev uninstall` (restores your config; `--purge` also removes the key and history).
- **Account alerts**: if Jev runs out of credit or the key is rejected/expired, a Claude Code hook shows a ⚠️ warning on your next message, Claude leads its reply with the options, every result carries the alert, and macOS shows a notification. New commands `jev check`, `jev key`, `jev off`/`jev on`, `jev dismiss`, `jev status`; MCP tool `jev_control`.
- `jev prune`: lossless filtering of long command output (error lines, head and tail are always kept; full output saved to disk).
- `jev eval`: golden-set accuracy (hit@k), thresholds fitted on one half and validated on the other.
- Packaging: `pyproject.toml`, so `uv tool install git+https://github.com/Dinesh-Sunny/jev-tokensaver` works; the skill and hook are embedded in `jev.py`.

### Changed
- Bounded self-tuning: needs at least 20 answered + 10 absent labels; moves at most ±0.05 per step; "not in this file" can only get rarer; `top_k` never drops below its default; uses current-model labels only; auto-tune only *proposes*.
- Honest savings metric: net of overhead, capped at Claude's read limit, counted only when Jev found the answer; inline inputs count as zero.

### Fixed
- Long/minified lines are split into segments instead of truncated (no more false "not in this file").
- Secret files are never sent (deny-list, dotfiles, `.gitignore`), and everything sent is scrubbed of keys, tokens, JWTs and private keys.
- Circuit breaker counts only real outages; malformed questions are rejected locally (`QUESTION ERROR`).
- Token-aware chunking (safe for CJK/Indic text); oversize chunks are split and retried.
- MCP tools run off the event loop, accept loose input types and map Cowork `/mnt/<folder>` paths.
- Race conditions in the answer cache and on first database creation under parallel requests.

## [0.2.0] - 2026-10-06 (pre-release, private)
- First working CLI + MCP server: `find`, `rank`, `map`, `ask`, cache, circuit breaker, budget, feedback-driven tuning.

[Unreleased]: https://github.com/Dinesh-Sunny/jev-tokensaver/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Dinesh-Sunny/jev-tokensaver/releases/tag/v0.4.0
