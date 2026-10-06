#!/usr/bin/env bash
# jev-tokensaver installer - https://github.com/Dinesh-Sunny/jev-tokensaver
#
#   curl -fsSL https://raw.githubusercontent.com/Dinesh-Sunny/jev-tokensaver/main/install.sh | bash
#
#   ... | bash -s -- --yes              no prompts (accept defaults; key from JEV_API_KEY if set)
#   ... | bash -s -- --uninstall        remove everything (backups of your config files are kept)
#   JEV_VERSION=v0.4.0 ... | bash       install a specific release
#   JEV_SOURCE=/path/to/checkout bash install.sh   install from a local clone (development)
#
# What it does: makes sure `uv` is installed, installs the `jev` command with
# `uv tool install`, then hands over to `jev setup` - a guided, idempotent setup that
# backs up every file it touches. Safe to re-run; that is also how you update.
set -euo pipefail

REPO="Dinesh-Sunny/jev-tokensaver"

main() {
  local version="${JEV_VERSION:-main}" source="${JEV_SOURCE:-}" yes="" uninstall="" setup_args=()

  while [ $# -gt 0 ]; do
    case "$1" in
      -y|--yes) yes=1; setup_args+=("--yes") ;;
      --uninstall) uninstall=1 ;;
      --version) version="${2:?--version needs a value, e.g. v0.4.0}"; shift ;;
      --source) source="${2:?--source needs a path}"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) setup_args+=("$1") ;;                       # passed through to `jev setup` (e.g. --no-desktop)
    esac
    shift
  done
  if [ -n "${CI:-}" ] || [ -n "${NONINTERACTIVE:-}" ]; then
    yes=1
    if [[ ! " ${setup_args[*]-} " == *" --yes "* ]]; then setup_args+=("--yes"); fi
  fi

  setup_colors
  if [ -n "$uninstall" ]; then do_uninstall; exit 0; fi

  printf '\n%sjev-tokensaver installer%s - save Claude tokens with TypeSafe Jev\n' "$BOLD" "$RESET"

  step "Checking your system"
  case "$(uname -s)" in
    Darwin) ok "macOS $(sw_vers -productVersion 2>/dev/null || true) ($(uname -m))" ;;
    Linux) ok "Linux ($(uname -m))" ;;
    *) die "Unsupported OS: $(uname -s). jev-tokensaver supports macOS and Linux (WSL works too)." ;;
  esac
  command -v curl >/dev/null 2>&1 || command -v uv >/dev/null 2>&1 \
    || die "curl is required to download uv. Install curl, then re-run."

  step "Python runner (uv)"
  ensure_uv

  step "Installing the jev command"
  local spec
  if [ -n "$source" ]; then
    spec="$source"
    info "from local source: $spec"
  else
    spec="git+https://github.com/${REPO}@${version}"
    info "from github.com/${REPO} @ ${version}"
  fi
  local log
  log="$(mktemp)"
  if ! uv tool install --force --quiet --python '>=3.10' "$spec" 2>"$log"; then
    cat "$log" >&2 || true
    rm -f "$log"
    die "Could not install jev-tokensaver (see the error above). Check your internet connection and re-run."
  fi
  rm -f "$log"
  local bin_dir jev
  bin_dir="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")"
  jev="$bin_dir/jev"
  [ -x "$jev" ] || die "Install finished but $jev was not found. Please open an issue: https://github.com/${REPO}/issues"
  ok "jev $("$jev" --version | awk '{print $2}') installed at $jev"

  case ":$PATH:" in
    *":$bin_dir:"*) ;;
    *)
      if [ -z "${JEV_NO_MODIFY_PATH:-}" ]; then
        uv tool update-shell >/dev/null 2>&1 || true
        warn "Added $bin_dir to your PATH - open a new terminal (or run: export PATH=\"$bin_dir:\$PATH\")"
      else
        warn "$bin_dir is not on your PATH (JEV_NO_MODIFY_PATH set) - add it yourself."
      fi
      export PATH="$bin_dir:$PATH" ;;
  esac

  # Hand over to the guided setup. Under `curl | bash` stdin is this script, so prompts read the terminal.
  if [ -n "$yes" ]; then
    "$jev" setup "${setup_args[@]}"
  elif [ -t 0 ]; then
    "$jev" setup "${setup_args[@]+"${setup_args[@]}"}"
  elif (exec </dev/tty) 2>/dev/null; then
    "$jev" setup "${setup_args[@]+"${setup_args[@]}"}" </dev/tty
  else
    warn "No terminal available for questions - using defaults (re-run \`jev setup\` any time to change them)."
    "$jev" setup --yes "${setup_args[@]+"${setup_args[@]}"}"
  fi
}

usage() {
  sed -n '2,15p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//' || true
  cat <<'TXT'
Options:  -y, --yes   --uninstall   --version <tag>   --source <dir>
Setup options passed through:  --no-hook --no-desktop --claude-md --new-key --skip-check
TXT
}

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then ok "uv $(uv --version | awk '{print $2}')"; return; fi
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin" /opt/homebrew/bin /usr/local/bin; do
    if [ -x "$d/uv" ]; then export PATH="$d:$PATH"; ok "uv found in $d"; return; fi
  done
  info "jev runs on uv (Astral's fast Python tool manager - it also fetches Python if needed)."
  if [ -z "$yes" ] && ! confirm "Install uv now?"; then
    die "uv is required. Install it (https://docs.astral.sh/uv/) and re-run."
  fi
  if command -v brew >/dev/null 2>&1; then
    brew install uv >/dev/null || die "brew install uv failed."
  else
    curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null || die "uv install failed - see https://docs.astral.sh/uv/"
    export PATH="$HOME/.local/bin:$PATH"
  fi
  command -v uv >/dev/null 2>&1 || die "uv was installed but isn't on PATH - open a new terminal and re-run."
  ok "uv $(uv --version | awk '{print $2}') installed"
}

do_uninstall() {
  if command -v jev >/dev/null 2>&1; then
    if [ -n "$yes" ]; then jev uninstall --yes; else jev uninstall </dev/tty 2>/dev/null || jev uninstall --yes; fi
  elif command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^jev-tokensaver'; then
    uv tool uninstall jev-tokensaver && ok "removed the jev command"
  else
    ok "jev-tokensaver is not installed - nothing to do."
  fi
}

confirm() {
  local a=""
  if [ -t 0 ]; then read -r -p "  ? $1 [Y/n] " a; elif (exec </dev/tty) 2>/dev/null; then read -r -p "  ? $1 [Y/n] " a </dev/tty; fi
  [[ ! "$a" =~ ^[Nn] ]]
}

setup_colors() {
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-}" != "dumb" ]; then
    BOLD=$'\033[1m'; BLUE=$'\033[1;34m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RED=$'\033[31m'; DIM=$'\033[2m'; RESET=$'\033[0m'
  else
    BOLD=""; BLUE=""; GREEN=""; YELLOW=""; RED=""; DIM=""; RESET=""
  fi
  STEP=0
}
step() { STEP=$((STEP + 1)); printf '\n%s[%s]%s %s%s%s\n' "$BLUE" "$STEP" "$RESET" "$BOLD" "$1" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
info() { printf '    %s%s%s\n' "$DIM" "$1" "$RESET"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1" >&2; }
die()  { printf '\n  %s✗ %s%s\n\n' "$RED" "$1" "$RESET" >&2; exit 1; }

main "$@"
