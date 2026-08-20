#!/usr/bin/env bash
# Launch exactly the two owner-approved Codex campaign queues, each under its own queue lock.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEX_BIN="${CODEX_BIN:-$(command -v codex)}"

for session in ml-queue-contact ml-queue-assoc ml-supervise-contact_point ml-supervise-association; do
  if tmux has-session -t "$session" 2>/dev/null; then
    echo "refusing: tmux session $session already exists" >&2
    exit 7
  fi
done

common="AF_ML_BACKEND=codex CODEX_BIN=$CODEX_BIN AF_CODEX_MODEL=gpt-5.6-sol AF_OMP_NUM_THREADS=7"

tmux new-session -d -s ml-queue-contact -c /workspace/praxis \
  "env $common AF_QUEUE_LOCK=/tmp/af-ml-queue-contact.lock $HERE/af-ml-agent-queue.sh --only contact_point"
tmux new-session -d -s ml-queue-assoc -c /workspace/praxis \
  "env $common AF_QUEUE_LOCK=/tmp/af-ml-queue-assoc.lock $HERE/af-ml-agent-queue.sh --only association"

echo "started contact_point and association Codex queues (OMP_NUM_THREADS=7)"
