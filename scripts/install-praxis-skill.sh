#!/usr/bin/env bash
# Wire a consumer repo up to the local Praxis knowledge loop.
#
# Run from the praxis repo. Given a target repo, this:
#   1. Junction-links the canonical Praxis skills (praxis-up, ce-compound-praxis)
#      into <target>/.claude/skills/ so edits to the canonical copies propagate live.
#   2. Gitignores those junctions in the target (they are machine-local links).
#   3. Appends a managed Praxis-knowledge block to <target>/CLAUDE.md (idempotent).
#   4. Adds the praxis MCP server to <target>/.mcp.json (idempotent).
#
# Windows note: uses directory junctions (mklink /J) — no admin / Developer Mode
# needed, unlike native symlinks (and git core.symlinks is false here anyway).
#
# Usage (from anywhere in the praxis repo):
#   scripts/install-praxis-skill.sh ~/repos/bridge-bidding-bot
#
# Re-running is safe: every step is idempotent.
set -euo pipefail

SKILLS=(praxis-up ce-compound-praxis)

CANON="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # praxis repo root
CANON_M="$(cygpath -m "$CANON")"                            # C:/Users/.../praxis

TARGET_ARG="${1:-}"
if [[ -z "$TARGET_ARG" ]]; then
  echo "usage: scripts/install-praxis-skill.sh <target-repo-path>" >&2
  exit 1
fi
if [[ ! -d "$TARGET_ARG" ]]; then
  echo "error: target repo not found: $TARGET_ARG" >&2
  exit 1
fi
TARGET="$(cd "$TARGET_ARG" && pwd)"
echo "praxis canon : $CANON"
echo "target repo  : $TARGET"

# --- 1. junction the skills ---------------------------------------------------
echo "skills:"
mkdir -p "$TARGET/.claude/skills"
for name in "${SKILLS[@]}"; do
  src="$CANON/.claude/skills/$name"
  link="$TARGET/.claude/skills/$name"
  if [[ ! -d "$src" ]]; then
    echo "  ! canonical skill missing, skipping: $src"
    continue
  fi
  if [[ -e "$link" ]]; then
    # rmdir removes a junction (or empty dir) without touching the link target;
    # it fails on a populated real dir, which we then leave untouched.
    if cmd.exe //c rmdir "$(cygpath -w "$link")" >/dev/null 2>&1; then
      :
    else
      echo "  ! $name exists and is not a removable junction; leaving it"
      continue
    fi
  fi
  if cmd.exe //c mklink //J "$(cygpath -w "$link")" "$(cygpath -w "$src")" >/dev/null; then
    echo "  linked $name -> $src"
  else
    echo "  ! failed to junction $name"
  fi
done

# --- 2. gitignore the machine-local junctions ---------------------------------
gi="$TARGET/.gitignore"
marker_gi="# praxis-integration (machine-local skill junctions)"
touch "$gi"
if grep -qF "$marker_gi" "$gi"; then
  echo "gitignore: already present"
else
  { printf '\n%s\n' "$marker_gi"
    for name in "${SKILLS[@]}"; do printf '/.claude/skills/%s/\n' "$name"; done
  } >> "$gi"
  echo "gitignore: added ${#SKILLS[@]} entr(ies)"
fi

# --- 3. append the CLAUDE.md block (idempotent via marker) --------------------
cm="$TARGET/CLAUDE.md"
marker_cm="<!-- praxis-integration:managed -->"
touch "$cm"
if grep -qF "$marker_cm" "$cm"; then
  echo "CLAUDE.md: block already present"
else
  cat >> "$cm" <<EOF

$marker_cm
## Praxis knowledge (local dev loop)

This repo can read and write durable lessons via the Praxis knowledge graph (MCP
server \`praxis\`, backed by the checkout at \`$CANON_M\`). Praxis is a dev-loop aid,
not a runtime dependency.

- **Before starting work** in a non-trivial or previously-touched area, call
  \`praxis_get_context\` with a query describing the task, and weave any returned
  active facts into your approach.
- If a \`praxis_*\` tool reports a connection error or 503, run the **\`praxis-up\`**
  skill to start the local backend (Postgres + FastAPI on :8000), then retry. The
  backend runs auth-disabled, so no \`praxis_login\` is needed.
- **After solving** something non-trivial and verified, offer to capture it with the
  **\`ce-compound-praxis\`** skill (\`praxis_ingest_session\`) so future sessions can
  retrieve it.
EOF
  echo "CLAUDE.md: appended praxis block"
fi

# --- 4. register the praxis MCP server in .mcp.json (idempotent) --------------
mj="$TARGET/.mcp.json"
( cd "$CANON" && uv run python - "$mj" "$CANON_M" <<'PY'
import json, os, sys
path, canon = sys.argv[1], sys.argv[2]
data = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception:
        data = {}
servers = data.setdefault("mcpServers", {})
servers["praxis"] = {
    "command": "uv",
    "args": ["run", "--directory", canon, "python", "-m", "knowledge.mcp"],
}
with open(path, "w") as f:
    json.dump(data, f, indent=2)
    f.write("\n")
print("  praxis server written")
PY
) && echo ".mcp.json: praxis MCP server registered" || echo ".mcp.json: ! merge failed (add the praxis server manually)"

echo "done."
