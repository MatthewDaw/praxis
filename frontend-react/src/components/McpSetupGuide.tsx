import { useState } from "react";

/** A copy-to-clipboard command block. */
function CommandBlock({ command, label }: { command: string; label?: string }) {
  const [copied, setCopied] = useState(false);

  function handleCopy() {
    void navigator.clipboard?.writeText(command).then(() => {
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }

  return (
    <div className="mcp-command">
      {label ? <span className="mcp-command__label">{label}</span> : null}
      <div className="mcp-command__row">
        <pre className="mcp-command__code">
          <code>{command}</code>
        </pre>
        <button
          type="button"
          className="btn secondary mcp-command__copy"
          onClick={handleCopy}
          aria-label={`Copy command: ${command}`}
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

const DESKTOP_CONFIG = `{
  "mcpServers": {
    "praxis": {
      "command": "uv",
      "args": ["run", "python", "-m", "knowledge.mcp"],
      "cwd": "C:/Users/mattd/Documents/gauntlet/praxis"
    }
  }
}`;

/**
 * Standalone documentation tab: how to install and use the Praxis MCP server
 * (the local knowledge-graph client for Claude Code / Desktop).
 */
export function McpSetupGuide() {
  return (
    <section className="mcp-guide" aria-label="MCP server setup guide">
      <header className="mcp-guide__intro">
        <h2>Install the Praxis MCP server</h2>
        <p className="muted">
          The Praxis MCP server is a thin, authenticated HTTP client over the FastAPI
          backend. It gives Claude Code and Claude Desktop two tools —{" "}
          <code>praxis_get_context</code> (read your active facts) and{" "}
          <code>praxis_add_insight</code> (write a fully-approved fact) — plus the{" "}
          login/org tools. Your tenant <code>(org_id, user_id)</code> is resolved from a
          cached Cognito login, so the local process never holds database credentials.
        </p>
      </header>

      <div className="mcp-guide__step">
        <h3>0. Prerequisites</h3>
        <ul>
          <li>
            <strong>Python 3.12+</strong> and project deps from the repo root.
          </li>
          <li>
            <code>OPENROUTER_API_KEY</code> in <code>.env</code> — the backend embeds
            insights and queries.
          </li>
          <li>
            <code>COGNITO_USER_POOL_ID</code> / <code>COGNITO_CLIENT_ID</code> /{" "}
            <code>COGNITO_REGION</code> in <code>.env</code> (already present).
          </li>
          <li>
            <code>PRAXIS_API_BASE_URL</code> — the backend the tools call. Use{" "}
            <code>http://localhost:8000</code> for local dev (run the backend with{" "}
            <code>uv run python -m knowledge.serve</code>), or the deployed App Runner
            URL. Tenant is derived from your login, <em>not</em> this var.
          </li>
        </ul>
        <CommandBlock command="uv sync" label="Install dependencies (repo root)" />
      </div>

      <div className="mcp-guide__step">
        <h3>1. Register with Claude Code</h3>
        <p>
          Run this <strong>inside the repo</strong> so <code>uv</code> resolves the
          project venv and <code>.env</code> loads. That is the only setup step — login
          happens through the MCP tools, so there is no separate CLI login command.
        </p>
        <CommandBlock command="claude mcp add praxis -- uv run python -m knowledge.mcp" />
        <CommandBlock command="claude mcp list" label="Verify it registered" />
      </div>

      <div className="mcp-guide__step">
        <h3>2. Register with Claude Desktop (alternative)</h3>
        <p>
          Add this to <code>claude_desktop_config.json</code>, setting <code>cwd</code> to
          your repo path so <code>.env</code> loads, then restart Claude Desktop.
        </p>
        <CommandBlock command={DESKTOP_CONFIG} label="claude_desktop_config.json" />
      </div>

      <div className="mcp-guide__step">
        <h3>3. Log in (inside a session — no CLI)</h3>
        <p>
          Just ask Claude to log you in; it calls the <code>praxis_login</code> tool. A
          refresh token + selected org are cached to <code>~/.praxis/mcp.json</code>{" "}
          (mode 600) — your password is never stored.
        </p>
        <blockquote className="mcp-guide__quote">
          “Log me into Praxis: email me@example.com, password ……”
        </blockquote>
        <p className="muted small">
          One org → auto-selected. Several → Claude lists them and you pick with{" "}
          <code>praxis_select_org</code>. No org yet? Use <code>praxis_create_org</code>{" "}
          (you set its join password) or <code>praxis_join_org</code>. Check state any
          time with <code>praxis_whoami</code>.
        </p>
      </div>

      <div className="mcp-guide__step">
        <h3>4. The commands &amp; tools</h3>
        <table className="mcp-tools-table">
          <thead>
            <tr>
              <th>Command / tool</th>
              <th>What it does</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>
                <code>claude mcp add praxis -- uv run python -m knowledge.mcp</code>
              </td>
              <td>Register the server with Claude Code (run once, in the repo).</td>
            </tr>
            <tr>
              <td>
                <code>claude mcp list</code>
              </td>
              <td>List registered MCP servers to confirm <code>praxis</code> is wired up.</td>
            </tr>
            <tr>
              <td>
                <code>uv run python -m knowledge.serve</code>
              </td>
              <td>Run the FastAPI backend locally on port 8000 (what the tools call).</td>
            </tr>
            <tr>
              <td>
                <code>praxis_login(email, password, org_id?)</code>
              </td>
              <td>Authenticate against Cognito and cache a refresh token + active org.</td>
            </tr>
            <tr>
              <td>
                <code>praxis_whoami()</code>
              </td>
              <td>Show current login, active org, and your memberships.</td>
            </tr>
            <tr>
              <td>
                <code>praxis_select_org(org_id)</code>
              </td>
              <td>Set the active org for subsequent calls.</td>
            </tr>
            <tr>
              <td>
                <code>praxis_create_org(org_id, password, name?)</code> /{" "}
                <code>praxis_join_org(org_id, password)</code>
              </td>
              <td>Bootstrap or join org membership, then select it.</td>
            </tr>
            <tr>
              <td>
                <code>praxis_get_context(query, top_k=8)</code>
              </td>
              <td>
                Pull the active facts most similar to <code>query</code> into the session
                (an empty query returns recent facts).
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_add_insight(insight, scope?, category?, source?)</code>
              </td>
              <td>
                Store a single fully-approved fact (confidence 1.0). Re-adds merge;
                conflicts force-overwrite the nearest contradicting fact.
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <div className="mcp-guide__step">
        <h3>5. Verify end to end</h3>
        <p>
          In a Claude session (ask Claude to log you in first), add a fact and read it
          back:
        </p>
        <CommandBlock command={'praxis_add_insight("use uv, not pip, in this repo", scope="global", category="constraint")'} />
        <CommandBlock command={'praxis_get_context("how do I install deps in this repo?")'} />
      </div>
    </section>
  );
}
