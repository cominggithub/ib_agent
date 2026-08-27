#!/usr/bin/env bash
# Stop the IBC-managed IB Gateway (and the Xvfb display it used).
set -euo pipefail

XVFB_DISPLAY="${XVFB_DISPLAY:-:99}"
STOP_XVFB="${STOP_XVFB:-yes}"

if pgrep -f 'ibcalpha.ibc.IbcGateway' >/dev/null 2>&1; then
  echo "stopping IB Gateway"
  pkill -f 'ibcalpha.ibc.IbcGateway' || true
  for _ in $(seq 1 20); do
    pgrep -f 'ibcalpha.ibc.IbcGateway' >/dev/null 2>&1 || break
    sleep 1
  done
  pgrep -f 'ibcalpha.ibc.IbcGateway' >/dev/null 2>&1 && pkill -9 -f 'ibcalpha.ibc.IbcGateway' || true
else
  echo "no IB Gateway process found"
fi

if [ "$STOP_XVFB" = "yes" ] && pgrep -f "Xvfb ${XVFB_DISPLAY}" >/dev/null 2>&1; then
  echo "stopping Xvfb ${XVFB_DISPLAY}"
  pkill -f "Xvfb ${XVFB_DISPLAY}" || true
fi

echo "stopped"
