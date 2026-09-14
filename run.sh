#!/usr/bin/env bash
# Simple Bot Trader launcher
# Optional: run.sh --config-dir <path>  (isolated profile = separate exchange)
# Uses the venv created by setup.sh when present; otherwise system python3.
#
# SINGLE-INSTANCE GUARD (user rule, 2026-09-04):
#   A bot must never orphan a competing process. systemd only manages the PID
#   it spawned — a plain `python3 main.py` launched from a desktop shortcut or
#   terminal can outlive `systemctl restart` and keep holding instance.lock
#   (the "Already Running" popup + stale GUI never updating). So:
#     * When this profile's systemd unit exists, the icon / shortcut / terminal
#       all converge on that ONE unit (restart it, killing any stale orphans).
#     * We enumerate + kill ANY stray main.py on the SAME profile first so the
#       lock is always freed before the managed instance starts.
set -u

cd "$(dirname "$0")"
UNIT="simple-bot-trader.service"

# arg parsing: pull out --config-dir <path> to key the unit + profile markers
CFGDIR=""
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --config-dir)
      shift; CFGDIR="$1" ;;
    *)
      ARGS+=("$1") ;;
  esac
  shift
done

if [ -n "$CFGDIR" ]; then
  UNIT="simple-bot-trader-$(basename "$CFGDIR").service"
fi

# Is this a boot / auto-update relaunch? (systemd passes --if-was-launched)
IS_RELAUNCH=no
for a in "${ARGS[@]:-}"; do
  [ "$a" = "--if-was-launched" ] && IS_RELAUNCH=yes
done
[ "${ARGS[*]:-}" = "" ] 2>/dev/null && IS_RELAUNCH=no

# Helper: kill any stray main.py (any launch style) that is not this invisible
# shell stack. This clears orphans holding the lock so a fresh instance wins.
kill_strays() {
  pkill -f "python3 main.py" 2>/dev/null
  pkill -f "python main.py" 2>/dev/null
  sleep 1
}

# SYSTEMD CONVERGENCE: if the unit is enabled, route through it so there is
# exactly one managed instance (never an unmanaged orphan). This is a MANUAL
# launch (icon / drawer / terminal) — the user wants the bot OPEN regardless
# of whether it was open at the last boot. The unit runs `run.sh
# --if-was-launched`, which EXITS SILENTLY when the launched.marker is absent
# (e.g. after a graceful Close cleared it) — so we must write the marker FIRST
# or the bot never opens after a manual close. That marker is exactly what the
# manual launch is declaring: "this profile is now in use."
if [ "$IS_RELAUNCH" != "yes" ] && systemctl --user list-unit-files "$UNIT" >/dev/null 2>&1 \
   && systemctl --user is-enabled "$UNIT" >/dev/null 2>&1; then
  kill_strays
  # Write the launched.marker for THIS profile before the unit checks it.
  if [ -n "$CFGDIR" ]; then
    MARKER="$CFGDIR/config/launched.marker"
  else
    MARKER="${XDG_CONFIG_HOME:-$HOME/.config}/simple-bot-trader/config/launched.marker"
  fi
  if [ -n "$MARKER" ]; then
    mkdir -p "$(dirname "$MARKER")" 2>/dev/null || true
    if [ ! -f "$MARKER" ]; then
      printf '{"pid": %s, "ts": %s}\n' "$$" "$(date +%s)" > "$MARKER" 2>/dev/null || true
    fi
  fi
  systemctl --user restart "$UNIT" || systemctl --user start "$UNIT"
  exit 0
fi

# No usable systemd unit (fresh dev build before enable): fall back to a plain
# launch but STILL clear orphans so the lock is free.
kill_strays

if [ -x "venv/bin/python" ]; then
  exec venv/bin/python main.py "${ARGS[@]:-}"
fi
exec python3 main.py "${ARGS[@]:-}"