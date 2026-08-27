#!/usr/bin/env bash
# Start IB Gateway headlessly under IBC, idempotent.
#
# Once this succeeds the Gateway stays logged in, so every later portfolio read
# is just a TCP connect to $IB_PORT: no login, no prompt, no human in the loop.
#
# Credentials are NOT here; they come from IBC's config.ini.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# shellcheck disable=SC1091
[ -f "$PROJECT_DIR/.env" ] && set -a && . "$PROJECT_DIR/.env" && set +a

IBC_PATH="${IBC_PATH:-/opt/ibc}"
IBC_INI="${IBC_INI:-$HOME/ibc/config.ini}"
TWS_PATH="${TWS_PATH:-$HOME/Jts}"
IB_PORT="${IB_PORT:-4001}"
IB_HOST="${IB_HOST:-127.0.0.1}"
XVFB_DISPLAY="${XVFB_DISPLAY:-:99}"
XVFB_GEOMETRY="${XVFB_GEOMETRY:-1920x1080x24}"
WAIT_SECONDS="${GATEWAY_WAIT_SECONDS:-180}"
LOG_DIR="${IB_LOG_DIR:-$PROJECT_DIR/logs}"
mkdir -p "$LOG_DIR"
RUN_LOG="$LOG_DIR/gateway-start.log"

port_open() {
  # bash's /dev/tcp avoids depending on nc being installed
  (exec 3<>"/dev/tcp/$IB_HOST/$IB_PORT") 2>/dev/null && exec 3<&- && return 0
  return 1
}

if port_open; then
  echo "gateway already listening on $IB_HOST:$IB_PORT"
  exit 0
fi

if pgrep -f 'ibcalpha.ibc.IbcGateway' >/dev/null 2>&1; then
  echo "IBC gateway process exists but port $IB_PORT is closed (login may still be in progress)"
else
  # --- headless X display -------------------------------------------------
  if ! pgrep -f "Xvfb ${XVFB_DISPLAY}" >/dev/null 2>&1; then
    echo "starting Xvfb on ${XVFB_DISPLAY}"
    nohup Xvfb "${XVFB_DISPLAY}" -screen 0 "${XVFB_GEOMETRY}" -nolisten tcp \
      >"$LOG_DIR/xvfb.log" 2>&1 &
    sleep 2
  fi

  # --- launch gateway via IBC --------------------------------------------
  echo "starting IB Gateway via IBC (log: $RUN_LOG)"
  DISPLAY="${XVFB_DISPLAY}" \
  IBC_INI="$IBC_INI" \
  TWS_PATH="$TWS_PATH" \
    nohup "$IBC_PATH/gatewaystart.sh" -inline >"$RUN_LOG" 2>&1 &
fi

# --- wait for the API socket ---------------------------------------------
# GATEWAY_NO_WAIT=1 (or --no-wait) returns as soon as the JVM is launched. The
# 2FA path needs that: the port cannot open until a code has been typed into the
# dialog, and `ib-agent gateway up` is the thing that types it.
if [ "${GATEWAY_NO_WAIT:-0}" = "1" ] || [ "${1:-}" = "--no-wait" ]; then
  echo "gateway launching; not waiting for port $IB_PORT"
  exit 0
fi

echo -n "waiting for API port $IB_PORT "
deadline=$((SECONDS + WAIT_SECONDS))
while [ "$SECONDS" -lt "$deadline" ]; do
  if port_open; then
    echo " ready"
    exit 0
  fi
  if grep -qiE 'Second Factor Authentication|security code' "$RUN_LOG" 2>/dev/null; then
    echo
    echo "IBKR is asking for two-factor authentication."
    echo "Approve the push notification in the IBKR Mobile app; this is needed"
    echo "only for this login, not for each portfolio read."
  fi
  echo -n "."
  sleep 3
done

echo " timeout"
echo "--- last 30 lines of $RUN_LOG ---"
tail -n 30 "$RUN_LOG" || true
exit 1
