#!/usr/bin/env bash
# Agent2Telegram one-command installer.
# Usage:  curl -fsSL <raw-url>/install.sh | bash      (or run it from a clone)
# It checks Python, installs the package for the current user, and launches setup.
set -euo pipefail

# Recover the working directory if it was deleted (e.g. you just uninstalled while sitting in
# the source clone) — otherwise git/curl fail with "cannot access parent directories: getcwd".
cd "$PWD" 2>/dev/null || cd "$HOME" 2>/dev/null || cd /

REPO="https://github.com/petrludwig-collab/Agent2Telegram.git"
NEED_PY_MAJOR=3
NEED_PY_MINOR=10

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
err() { printf '\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

# 1) Python check
PY="$(command -v python3 || true)"
[ -n "$PY" ] || err "python3 not found. Install Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ first."
"$PY" - <<'PYEOF' || err "Python ${NEED_PY_MAJOR}.${NEED_PY_MINOR}+ required."
import sys
sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)
PYEOF
say "Using $("$PY" --version)"

# 2) Get the code (clone if we're not already inside it)
if [ -f "pyproject.toml" ] && grep -q "agent2telegram" pyproject.toml 2>/dev/null; then
  SRC="$(pwd)"
  say "Installing from current directory"
else
  command -v git >/dev/null || err "git not found (needed to fetch the project)."
  SRC="${HOME}/.agent2telegram-src"
  if [ -d "$SRC/.git" ]; then say "Updating $SRC"; git -C "$SRC" pull --ff-only
  else say "Cloning into $SRC"; git clone --depth 1 "$REPO" "$SRC"; fi
fi

# 3) Make `agent2telegram` runnable. The core is pure standard library, so pip is OPTIONAL:
#    if it's available we install onto PATH; if not (common on a bare Debian/Ubuntu where pip
#    and ensurepip are absent), we just run from the clone — identical behavior, no install.
INSTALLED=0
if "$PY" -m pip --version >/dev/null 2>&1 || "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
  say "Installing the package"
  if "$PY" -m pip install --user --upgrade "$SRC" >/dev/null 2>&1 \
     || "$PY" -m pip install --user --break-system-packages --upgrade "$SRC" >/dev/null 2>&1; then
    INSTALLED=1
  fi
fi
if [ "$INSTALLED" = 1 ]; then
  RUN=("$PY" -m agent2telegram)
  HOW="agent2telegram"
else
  # No pip: drop a tiny launcher so `agent2telegram` is still a real command (not a long
  # PYTHONPATH line you have to remember for every update/connect).
  say "pip unavailable — installing a launcher in ~/.local/bin (dependency-free)."
  mkdir -p "$HOME/.local/bin"
  printf '#!/bin/sh\nexec env PYTHONPATH="%s" "%s" -m agent2telegram "$@"\n' "$SRC" "$PY" > "$HOME/.local/bin/agent2telegram"
  chmod +x "$HOME/.local/bin/agent2telegram"
  case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc";; esac
  export PATH="$HOME/.local/bin:$PATH"
  RUN=("$HOME/.local/bin/agent2telegram")
  HOW="agent2telegram"
fi

# 4) Launch the setup wizard.
# When invoked as `curl … | bash`, this script's stdin is the pipe, not your keyboard,
# so the interactive wizard must read from the controlling terminal (/dev/tty).
say "Run it later with:  $HOW run"
if [ -e /dev/tty ]; then
  say "Starting setup…"
  exec "${RUN[@]}" setup </dev/tty
else
  say "Installed. Finish setup with:"
  echo "    $HOW setup"
fi
