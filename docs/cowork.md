# Using jev-tokensaver in Cowork / Claude Desktop

Cowork runs Claude in a cloud sandbox that **cannot reach TypeSafe's API**. jev-tokensaver solves this with an MCP server (`jev serve`) that **Claude Desktop starts on your Mac**. Claude calls the tools; the work happens on your machine.

## Setup

`jev setup` registers the server (step 5/5). Afterwards **quit and reopen Claude Desktop**. To check:

```bash
jev doctor     # "Claude Desktop / Cowork: jev registered"
```

In a Cowork chat, ask: *"Use jev_status"*. You should see your usage and alert status.

## Paths

Cowork shows your connected folders at sandbox paths like `/sessions/…/mnt/<folder>/file.ts`. The jev tools map these back to your Mac automatically by matching the folder name under your home folder, `~/Documents`, `~/CodeProjects` or `~/Desktop`. If a folder lives somewhere else, add it to `JEV_ROOTS` in the Desktop config:

```json
{ "mcpServers": { "jev": {
    "command": "/Users/you/.local/bin/jev", "args": ["serve"],
    "env": { "JEV_ROOTS": "/Volumes/Work/projects" } } } }
```

## Tools

`jev_prune`, `jev_find`, `jev_rank`, `jev_map`, `jev_ask`, `jev_feedback`, `jev_status`, `jev_control`. See [commands.md](commands.md#mcp-tools-cowork--claude-desktop).

## Alerts

Cowork has no hooks, so if Jev runs out of credit you'll see it (a) at the top of Claude's next reply after a Jev call and (b) as a macOS notification. Claude can run `jev_control("check" | "off" | "on" | "dismiss")` for you. It will never ask for your key in chat; use `jev key` in Terminal.
