#!/usr/bin/env bash
# Simple Bot Trader launcher
# Optional: run.sh --config-dir <path>  (isolated profile = separate exchange)
# Uses the venv created by setup.sh when present; otherwise system python3.
#
# MULTI-BOT RULE (user directive, 2026-09-27 — never regress to killing):
#   Double-clicking the launcher must NEVER kill a running bot. One bot per API
#   key, and the user opens as many bots as they have keys (or RAM). So this
#   launcher does NOT pkill anything. A second launch on an already-running
#   profile falls through to main.py, which shows "Already Running" and offers
#   "Open Another Instance" -> pick a SAVED API (next bot, one per key) or
#   "Add new bot…" (asks for another API). When the managed systemd service is
#   NOT running, a manual launch starts it (this keeps return-to-state alive);
#   when it IS running, we leave it alone (no restart -> no kill).
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

# MANAGED-LAUNCH CONVERGENCE (manual launch, unit enabled):
#   Start the unit ONLY when it is not already active. If it is active, do NOT
#   restart it (a restart would kill the running bot) — fall through to a plain
#   `main.py` launch so main.py's per-profile lock shows "Already Running /
#   Open Another Instance" instead of killing anything. The unit's own
#   ExecStart runs `run.sh --if-was-launched` (a relaunch), which skips this
#   block entirely.
if [ "$IS_RELAUNCH" != "yes" ] && systemctl --user list-unit-files "$UNIT" >/dev/null 2>&1 \
   && systemctl --user is-enabled "$UNIT" >/dev/null 2>&1 \
   && ! systemctl --user is-active --quiet "$UNIT"; then
  # The unit is enabled but NOT running -> write the launched.marker (manual
  # launch declares "this profile is now in use" so --if-was-launched opens it),
  # then start it via systemd (exactly one managed instance).
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
  systemctl --user start "$UNIT"
  exit 0
fi

# Otherwise: a plain launch. NEVER kill strays/other bots (multi-bot rule).
if [ -x "venv/bin/python" ]; then
  exec venv/bin/python main.py "${ARGS[@]:-}"
fi
exec python3 main.py "${ARGS[@]:-}"