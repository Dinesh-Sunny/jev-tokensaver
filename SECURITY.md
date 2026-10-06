# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private reporting instead:
**[Report a vulnerability](https://github.com/Dinesh-Sunny/jev-tokensaver/security/advisories/new)**. You'll get a reply within a few days.

In scope: anything that could send secrets or private code to a third party, leak the TypeSafe API key, run commands through the installer or hook, or let file content change what `jev` does on your machine.

## Supported versions

The latest release receives fixes.

## How jev-tokensaver handles your data

| What | How |
|---|---|
| **API key** | stored at `~/.config/typesafe/api_key` (file mode 600, folder 700); never printed in full (only `…1234`); never requested in chat by the Claude skill; the setup prompt uses hidden input |
| **What is sent to TypeSafe** | only the text being judged (a file chunk, a file excerpt, a list item, piped output) plus the question - after secret scrubbing |
| **Never sent** | `.env*`, `*.pem`, `*.key`, `*.p12`, `id_*` SSH keys, `.npmrc`, `.pypirc`, `.netrc`, `*credential*`, `*secret*`, `*.tfstate`, `*.kdbx`, SQLite databases, dotfiles/dot-folders, `.gitignore`d files, vendor folders, and anything under a folder containing `.jevoff` |
| **Scrubbed before sending** | Stripe/OpenAI/Anthropic-style `sk-…` keys, AWS access keys, GitHub tokens, Slack tokens, Google API keys, JWTs, private key blocks, and `password=` / `token:` / `api_key=`-style values |
| **Stored locally** | `~/.config/typesafe/jev.db` (mode 600): answer cache, call log (file names, short query notes, costs). `~/.cache/jev/prune/`: the last 50 pruned outputs. `jev uninstall --purge` removes both. |
| **Installer** | changes only `~/.claude/skills/jev`, `~/.claude/settings.json` (one hook entry), optionally `~/.claude/CLAUDE.md` (a marked block) and the Claude Desktop config (one `mcpServers.jev` entry). Each file is backed up first. |

Scrubbing is pattern-based and can't catch every secret format. For sensitive repositories, add a `.jevoff` file or run `jev off`.

TypeSafe's own data handling: <https://typesafe.ai/legal>.
