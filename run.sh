#!/bin/sh
# Launcher for every command in the manifest.
#
# 1. `tick` is wired to events that fire constantly (each focus change, each
#    agent status flip). snooze.py deletes its state file whenever nothing is
#    snoozed, so no file means nothing to do: exit before paying for Python.
# 2. `python3` is often a version-manager shim (pyenv, asdf, mise) that costs
#    ~100ms per start — most of what a toggle takes. Resolve the real
#    interpreter once, cache its path in the state dir, and exec that.
dir=$(dirname "$0")
# Must resolve exactly as plugin_state_dir() in snooze.py does. If the two
# ever disagreed, Python would write the state file in one place while this
# looked in another — and every tick would exit here, silently, forever.
state="${SNOOZE_STATE_DIR:-${HERDR_PLUGIN_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/herdr/plugins/${HERDR_PLUGIN_ID:-herdr-snooze}}}"

# Each herdr session keeps its state in $state/sessions/<key>/ (snooze.py's
# state_dir()); 0.1.0 kept one file at $state/snoozed.json, which still needs
# Python until it's been recovered. Working out this session's key would cost
# a process, so look for a file in any of them: a cheap glob, and Python only
# starts while something is snoozed somewhere.
anything_snoozed() {
  [ -f "$state/snoozed.json" ] && return 0
  for f in "$state"/sessions/*/snoozed.json; do
    [ -f "$f" ] && return 0
  done
  return 1
}

if [ "$1" = "tick" ] && ! anything_snoozed; then
  exit 0
fi

# The cache says which program to run, so only a cache nobody else could have
# written is used: owned by us, in a directory owned by us, neither writable by
# group or others. Anything else is ignored and the interpreter found afresh.
cache_trusted() {
  [ -r "$state/python" ] && [ -O "$state/python" ] && [ -O "$state" ] || return 1
  [ -z "$(find "$state" "$state/python" -maxdepth 0 \( -perm -g=w -o -perm -o=w \) 2>/dev/null)" ]
}

py=""
if cache_trusted; then
  read -r py < "$state/python"
fi
if [ -z "$py" ] || [ ! -x "$py" ]; then
  py=$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null)
  if [ -n "$py" ] && [ -x "$py" ]; then
    (umask 077 && mkdir -p "$state" && chmod go-w "$state" && printf '%s\n' "$py" > "$state/python.tmp.$$" \
      && mv -f "$state/python.tmp.$$" "$state/python") 2>/dev/null
  fi
fi
exec "${py:-python3}" "$dir/snooze.py" "$@"
