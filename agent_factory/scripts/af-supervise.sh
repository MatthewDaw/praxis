#!/usr/bin/env bash
# Restart failed drivers; a successful drain or explicit operator stop stays stopped.
set -u
[ "$#" -gt 0 ] || { echo 'usage: af-supervise.sh <driver> [args...]' >&2; exit 2; }
child=""; stopping=0; delay=${AF_SUPERVISOR_RETRY_S:-30}
stop(){ stopping=1; [ -z "$child" ] || kill -TERM "$child" 2>/dev/null || true; }
trap stop TERM INT
while :; do
  "$@" & child=$!
  wait "$child"; result=$?
  if [ "$stopping" = 1 ]; then wait "$child" 2>/dev/null || true; exit 130; fi
  child=""
  [ "$result" -ne 0 ] || exit 0
  printf '[recovery] driver exited %s; retry in %ss; pending verification is retained\n' "$result" "$delay" >&2
  sleep "$delay" & child=$!
  wait "$child" || true
  [ "$stopping" = 0 ] || exit 130
  child=""
  delay=$((delay * 2)); [ "$delay" -le 300 ] || delay=300
done
