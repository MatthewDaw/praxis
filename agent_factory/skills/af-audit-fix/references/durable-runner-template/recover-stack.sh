#!/bin/bash
# Recover a missing audit component on the already-created disposable database.
set -euo pipefail
set +x
umask 077
[[ $# == 3 && "$2" =~ ^[0-9a-f]{40}$ && ( "$3" == api || "$3" == web ) ]]
worktree="$1"
build="$2"
component="$3"
root="$(cd "$(dirname "$0")/.." && pwd)"
[[ "$worktree" == "$root/worktree-${build:0:12}" ]]
[[ "$(git -C "$worktree" rev-parse HEAD)" == "$build" ]]
stack="$root/stack-post-adoption-${build:0:12}"
[[ -d "$stack" ]]
port=5190
if [[ "$component" == api ]]; then port=18091; fi
if lsof -nP -a -iTCP:"$port" -sTCP:LISTEN -t >/dev/null; then
  exit 2
fi

set -a
source "$HOME/backups/mvpvu-stack-ops/web-audit.env"
source "$HOME/backups/mvpvu-stack-ops/api-audit.env"
set +a
export DATABASE_URL="$(node - "${build:0:12}" <<'NODE'
const url = new URL(process.env.DATABASE_URL);
if (url.hostname !== "127.0.0.1" || url.port !== "5435" || url.pathname !== "/mvpvu_audit") process.exit(2);
url.pathname = "/mvpvu_audit_" + process.argv[2];
process.stdout.write(url.href);
NODE
)"
unset MIGRATION_DATABASE_URL
[[ -n "$DATABASE_URL" && "$mvpvu_AUTH_PROVIDER" == local ]]
origin=http://127.0.0.1:5190
export HOST=127.0.0.1 PORT="$port" mvpvu_BUILD_ID="$build" mvpvu_DATABASE_BOOTSTRAPPED=1
export mvpvu_ALLOWED_ORIGINS="$origin" mvpvu_PUBLIC_BASE_URL="$origin"
export mvpvu_API_PROXY_TARGET=http://127.0.0.1:18091
export mvpvu_MAIL_TRANSPORT=sink mvpvu_SMS_TRANSPORT=disabled
export mvpvu_MAIL_SINK_FILE="$stack/mail.jsonl"
mock="$root/providers/active-993b78598d2c.private.json"
if [[ "$build" == 993b78598d2c3b82bf569b441f2b4836a2e63a07 && -f "$mock" ]]; then
  export MVPVU_AUDIT_MOCK_CONFIG="$mock"
  export MVPVU_AUDIT_PROVIDER_STRIPE_PROXY=http://127.0.0.1:18191
  source "$root/providers/provider-env.sh"
  if [[ "$component" == api ]]; then
    cd "$worktree/mvpvu/apps/api"
    exec node --import "$root/providers/audit-provider-preload.mjs" src/index.ts
  fi
  cd "$worktree/mvpvu/apps/web"
  exec node ../../node_modules/.bin/vite --config .audit-provider-vite.config.mjs --port "$PORT" --strictPort --host 127.0.0.1
fi
if [[ "$component" == api ]]; then
  cd "$worktree/mvpvu/apps/api"
  exec node src/index.ts
else
  cd "$worktree/mvpvu/apps/web"
  [[ -f .audit-vite.config.mjs ]]
  exec node ../../node_modules/.bin/vite --config .audit-vite.config.mjs --port "$PORT" --strictPort --host 127.0.0.1
fi
