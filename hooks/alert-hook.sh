#!/bin/sh
# Claude Code UserPromptSubmit hook (installed by install.sh).
# If Jev has an account problem (no credit, key rejected/expired, no key), jev writes
# ~/.config/typesafe/alert-hook.json; this prints it so you see a warning immediately and
# Claude leads its reply with the problem and your options. Must stay fast and never fail.
f="$HOME/.config/typesafe/alert-hook.json"
[ -f "$f" ] || exit 0
cat "$f" 2>/dev/null
[ -f "$f.once" ] && rm -f "$f" "$f.once"
exit 0
