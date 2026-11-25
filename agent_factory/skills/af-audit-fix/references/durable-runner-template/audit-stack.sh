#!/bin/bash
# usage: audit-stack.sh start|stop|restart|status [root]  (default root: website-2, branch consolidation)
# Audit stack: API :18090, web :5190, DB mvpvu_audit. Tracks its own pids in $D/audit-*.pid; never touches :5174/:18081.
ACT=${1:-start}; ROOT=${2:-/Users/matthewdaw/orca/workspaces/sports_analysis/website-2}; R=$ROOT/mvpvu
D=/private/tmp/claude-501; B=$HOME/backups/mvpvu-stack-ops
E=$D; [ -f $D/api-audit.env ] || E=$B
mkdir -p $D
LOCK=$D/audit-stack.lock
alive() { [ -f "$1" ] && kill -0 "$(cat $1)" 2>/dev/null; }
killpid() { # only pids we recorded
  if alive $1; then kill "$(cat $1)" 2>/dev/null; for i in 1 2 3 4 5; do alive $1 || break; sleep 1; done; alive $1 && kill -9 "$(cat $1)" 2>/dev/null; fi
  rm -f $1; }
adopt() { # record the listener on a port if no pid file yet (stack started by an older script)
  [ -f $2 ] || { p=$(lsof -nP -iTCP:$1 -sTCP:LISTEN -t | head -1); [ -n "$p" ] && echo $p > $2; }; }
stop() { adopt 18090 $D/audit-api.pid; adopt 5190 $D/audit-web.pid; killpid $D/audit-api.pid; killpid $D/audit-web.pid; }
start() {
  alive $D/audit-api.pid && alive $D/audit-web.pid && { echo "audit stack already running"; status; return 0; }
  set -a; . $E/api-audit.env; set +a; export mvpvu_DATABASE_BOOTSTRAPPED=1
  (cd $R/apps/api && node scripts/db-migrate.ts 2>&1 | grep -E "applied every|rror")
  alive $D/audit-api.pid || { cd $R/apps/api && nohup node src/index.ts > $D/api-audit.log 2>&1 < /dev/null & echo $! > $D/audit-api.pid; }
  set -a; . $E/web-audit.env; set +a
  alive $D/audit-web.pid || { cd $R/apps/web && nohup node ../../node_modules/.bin/vite --port 5190 --strictPort --host 127.0.0.1 > $D/web-audit.log 2>&1 < /dev/null & echo $! > $D/audit-web.pid; }
  for i in $(seq 1 60); do
    [ "$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:18090/v1/health)" = 200 ] && [ "$(curl -s -o /dev/null -w %{http_code} http://127.0.0.1:5190/)" = 200 ] && break; sleep 2; done
  status
}
status() { curl -s -o /dev/null -w "audit api %{http_code}\n" http://127.0.0.1:18090/v1/health; curl -s -o /dev/null -w "audit web %{http_code}\n" http://127.0.0.1:5190/; }
# serialise concurrent callers
n=0; until mkdir $LOCK 2>/dev/null; do n=$((n+1)); [ $n -gt 150 ] && { echo "lock busy"; exit 1; }
  [ -f $LOCK/pid ] && ! kill -0 $(cat $LOCK/pid) 2>/dev/null && rm -rf $LOCK; sleep 2; done
echo $$ > $LOCK/pid; trap 'rm -rf $LOCK' EXIT
case $ACT in start) start;; stop) stop;; restart) stop; start;; status) status;; esac
