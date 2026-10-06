# Configuration

All settings are optional environment variables. Set them in your shell profile (CLI), or in the `env` block of the Claude Desktop `mcpServers.jev` entry (Cowork).

| Variable | Default | What it does |
|---|---|---|
| `TYPESAFE_API_KEY` | - | API key. Takes priority over the saved file `~/.config/typesafe/api_key` |
| `JEV_API_KEY` | - | used by `jev setup` / the installer to save a key without prompting |
| `TYPESAFE_DEFAULT_MODEL` | `jev-latest` | Jev model; pin a version such as `jev-1.13.0` once `jev eval` looks good |
| `JEV_DAILY_BUDGET_USD` | `0.20` | daily spend cap; above it calls return FALLBACK (`0` = no cap) |
| `JEV_MONTHLY_CREDIT_USD` | `5.0` | one-time heads-up at 80% of this monthly spend (estimated from this machine; `0` = off) |
| `JEV_WORKERS` | `6` | parallel requests; lower to 3-4 if `jev review` shows rate limits |
| `JEV_CACHE_DAYS` | `7` | answer cache lifetime (`0` = off) |
| `JEV_AUTOTUNE_EVERY` | `10` | propose a re-tune every N feedback labels (`0` = off) |
| `JEV_AUTOTUNE_APPLY` | off | `1` = apply automatic tunes instead of only proposing them |
| `JEV_DISABLE` | - | `1` = kill switch (same as `jev off`) |
| `JEV_ROOTS` | - | extra folders (`:`-separated) used to map Cowork `/mnt/<folder>/…` paths to your Mac |
| `JEV_CONFIG_DIR` | `~/.config/typesafe` | where the key, database and tuning live |
| `JEV_PRUNE_DIR` | `~/.cache/jev/prune` | where full outputs from `jev prune` are kept (last 50) |
| `JEV_NO_NOTIFY` | - | `1` = no macOS notifications |
| `NO_COLOR` | - | no colours in setup/doctor output |

## Installer variables

| Variable | What it does |
|---|---|
| `JEV_VERSION` | install a tag or branch (default `main`) |
| `JEV_SOURCE` | install from a local checkout |
| `JEV_NO_MODIFY_PATH` | don't add uv's bin folder to your shell profile |
| `CI` / `NONINTERACTIVE` | no prompts (same as `--yes`) |

## Per-folder opt-out

Create an empty file called `.jevoff` in any folder. Jev won't read or send anything at or below that folder, which is useful for client or NDA code.

## Files jev-tokensaver touches

| Path | Why |
|---|---|
| `~/.local/bin/jev` (via `uv tool`) | the command |
| `~/.config/typesafe/api_key`, `jev.db`, `tuning.json`, `alert*.json` | key, cache and history, tuning, alert state |
| `~/.cache/jev/prune/` | full outputs from `jev prune` |
| `~/.claude/skills/jev/SKILL.md` | the Claude skill |
| `~/.claude/settings.json` | one `UserPromptSubmit` hook entry (alerts) |
| `~/.local/share/jev-tokensaver/alert-hook.sh` | the hook script (prints a file; never touches the network) |
| `~/.claude/CLAUDE.md` | optional rule, inside `<!-- jev-tokensaver:start/end -->` markers |
| `~/Library/Application Support/Claude/claude_desktop_config.json` | one `mcpServers.jev` entry |

Each of these is backed up before it changes, and `jev uninstall` reverts them.
