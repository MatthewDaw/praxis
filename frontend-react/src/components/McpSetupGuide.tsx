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

// One praxis MCP server per agent, each pinned to its own identity cache via
// PRAXIS_MCP_CACHE — so the two agents drive different orgs without clobbering
// each other's active-org file.
const MULTI_AGENT_CONFIG = `{
  "mcpServers": {
    "praxis": {
      "command": "uv",
      "args": ["run", "--directory", "C:/Users/mattd/Documents/gauntlet/praxis",
               "python", "-m", "knowledge.mcp"],
      "env": { "PRAXIS_MCP_CACHE": "C:/Users/mattd/.praxis/agentA.json" }
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
          backend. It gives Claude Code and Claude Desktop tools to{" "}
          <strong>read</strong> (<code>praxis_get_context</code>,{" "}
          <code>praxis_list_graph</code>), <strong>write</strong>{" "}
          (<code>praxis_add_insight</code> through the ingestion pipeline,{" "}
          <code>praxis_insert_fact</code> raw, <code>praxis_edit_fact</code>),{" "}
          <strong>manage fact lifecycle</strong>{" "}
          (<code>praxis_promote_fact</code>, <code>praxis_reject_fact</code>,{" "}
          <code>praxis_delete_fact</code>), <strong>resolve contradictions</strong>{" "}
          (<code>praxis_get_contradictions</code>,{" "}
          <code>praxis_resolve_contradiction</code>), and{" "}
          <strong>work with snapshots</strong> — save/load/list/delete the whole
          graph (<code>praxis_save_snapshot</code>, <code>praxis_load_snapshot</code>,{" "}
          <code>praxis_list_snapshots</code>, <code>praxis_delete_snapshot</code>),
          clear it (<code>praxis_clear_graph</code>), browse and fold in another
          member&apos;s snapshots (<code>praxis_list_org_sources</code>,{" "}
          <code>praxis_browse_snapshot</code>, <code>praxis_fold_in</code>), and{" "}
          <strong>mount snapshots as read-only overlays</strong> that are recalled at
          read time without being merged in or carried over on save
          (<code>praxis_mount_snapshot</code>, <code>praxis_unmount_snapshot</code>,{" "}
          <code>praxis_list_mounts</code>) — plus the login/org tools. They also drive
          the <strong>compounding loop</strong>: record H1 outcomes that tune fact
          trust/utility (<code>praxis_record_outcome</code>), append immutable H4
          episodes (<code>praxis_record_episode</code>), express and traverse H5
          derivations (<code>derived_from</code> on{" "}
          <code>praxis_add_insight</code>/<code>praxis_ingest</code>,{" "}
          <code>praxis_dependents</code>, <code>praxis_get_stale_derivations</code>),
          recall point-in-time or episodic context (<code>as_of</code> /{" "}
          <code>include_episodic</code> on <code>praxis_get_context</code>), and read a
          fact&apos;s full <code>meta</code> (<code>praxis_get_fact</code>). This is full
          parity with the dashboard&apos;s graph, Snapshots, and Context actions. Your tenant{" "}
          <code>(org_id, user_id)</code> is resolved
          from a cached Cognito login, so the local process never holds database
          credentials.
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
            <strong>A Postgres DSN</strong> (<code>PRAXIS_DB_URL</code>, or a resolvable
            AWS secret) — <code>praxis_get_context</code> and{" "}
            <code>praxis_add_insight</code> require it. Without a DSN the backend starts
            in JSON/offline mode and both tools return{" "}
            <code>503 "requires a database"</code>. Confirm with{" "}
            <code>GET /health</code> showing <code>"store":"postgres"</code>.
          </li>
          <li>
            <code>PRAXIS_API_BASE_URL</code> — the backend the tools call. Use{" "}
            <code>http://localhost:8000</code> for local dev (run the backend with{" "}
            <code>uv run python -m knowledge.serve</code>), or the deployed App Runner
            URL <em>once it has the <code>/insights</code> + <code>/context</code>{" "}
            endpoints deployed</em>. Tenant is derived from your login, <em>not</em> this
            var.
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
        <p className="muted small">
          Want a second, independent working graph inside an org (e.g. to keep an
          experiment separate)? Create one with <code>praxis_create_space</code>, list
          them with <code>praxis_list_space</code>, and switch with{" "}
          <code>praxis_select_space</code> (<code>praxis_select_space(&quot;&quot;)</code>{" "}
          returns to the default graph). The selected space is cached alongside the
          active org and rides the <code>X-Praxis-Space</code> header.
        </p>
      </div>

      <div className="mcp-guide__step">
        <h3>
          Run multiple agents on separate orgs (<code>PRAXIS_MCP_CACHE</code>)
        </h3>
        <p>
          The login + selected org are cached to a single file
          (<code>~/.praxis/mcp.json</code>) shared by <em>every</em> Praxis MCP server
          on the machine. So two agents that both use the default cache share one
          active org — whichever calls <code>praxis_select_org</code> last wins, and the
          other agent&apos;s writes silently land in the wrong tenant. (Tenancy itself is
          fully isolated server-side by <code>(org_id, user_id)</code>; the only shared
          thing is this client-side cache.)
        </p>
        <p>
          To drive a <strong>different org per agent at the same time</strong>, give each
          agent&apos;s <code>praxis</code> server its <strong>own</strong> cache file via the{" "}
          <code>PRAXIS_MCP_CACHE</code> environment variable. Point agent A at{" "}
          <code>agentA.json</code> and agent B at <code>agentB.json</code> (any paths):
        </p>
        <CommandBlock
          command={MULTI_AGENT_CONFIG}
          label="Agent A — .mcp.json / ~/.claude.json (Agent B: set agentB.json)"
        />
        <p className="muted small">
          Set it however your client passes env to an MCP server: the <code>env</code>{" "}
          block above (Claude Desktop / <code>~/.claude.json</code>), or{" "}
          <code>claude mcp add praxis --env PRAXIS_MCP_CACHE=C:/Users/mattd/.praxis/agentA.json -- uv run python -m knowledge.mcp</code>{" "}
          for Claude Code. Then, in each agent: <code>praxis_login</code> →{" "}
          <code>praxis_create_org</code> / <code>praxis_select_org</code> for that
          agent&apos;s org (the same user can belong to both — isolation is by org) →{" "}
          <code>praxis_whoami</code> to confirm. <strong>Reconnect <code>/mcp</code></strong>{" "}
          after editing the config so the new env takes effect. Each agent&apos;s caches start
          empty, so each logs in independently and pins its own org.
        </p>
        <p className="muted small">
          To run several agents in the <strong>same org</strong> on{" "}
          <strong>different working graphs</strong>, give each its own{" "}
          <em>space</em> rather than its own org: either select a different space in each
          agent&apos;s cache (<code>praxis_select_space</code>), or pin one per process
          without a select call via the <code>PRAXIS_SPACE</code> environment variable
          (it overrides the cached space for that server). Effective tenancy is{" "}
          <code>(org_id, user_id::space)</code>, so the graphs stay fully isolated
          server-side.
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
                <code>praxis_create_space(space_id, name?)</code> /{" "}
                <code>praxis_list_space()</code> /{" "}
                <code>praxis_select_space(space_id)</code>
              </td>
              <td>
                Create, list, and switch between separate working graphs inside the
                active org (a private second axis on tenancy). Select{" "}
                <code>&quot;&quot;</code> to return to the default graph; or pin one per
                process with the <code>PRAXIS_SPACE</code> env var.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_delete_space(space_id)</code>
              </td>
              <td>
                <strong>Destructive.</strong> Permanently delete one of your spaces and
                its entire working graph (facts, snapshots, mounts). If you delete the
                space you currently have selected, the cache falls back to the default
                graph automatically.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_delete_org(org_id)</code>
              </td>
              <td>
                <strong>Destructive &amp; owner-only.</strong> Permanently delete an org
                and ALL of its data for <em>every</em> member (knowledge graphs,
                snapshots, spaces, api keys, memberships). A non-owner member gets a
                clear refusal; non-members 404.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_get_context(query, top_k=8, as_of?, include_episodic=false)</code>
              </td>
              <td>
                Pull the active facts most similar to <code>query</code> into the session
                (an empty query returns recent facts). <code>top_k</code> is advisory —
                the returned context is similarity-ranked and token-bounded server-side.{" "}
                <code>as_of</code> is an ISO-8601 timestamp for point-in-time recall (the
                graph as it stood then); <code>include_episodic=true</code> opts episodes
                back into the results (category <code>episodic</code> is excluded by
                default). Hits do not include <code>meta</code> — use{" "}
                <code>praxis_get_fact</code> for that.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_add_insight(insight, derived_from?, raw=False)</code>
              </td>
              <td>
                Store a single fully-approved fact (confidence 1.0). Re-adds merge;
                conflicts force-overwrite the nearest contradicting fact. Pass{" "}
                <code>derived_from</code> (a list of source fact ids) to record H5{" "}
                <code>derived_from</code> edges so the new learning is traceable to — and
                invalidated with — its sources. Set <code>raw=True</code> for a{" "}
                <strong>fast trusted insert</strong> that skips dedup and the
                conflict/LLM steps (redaction still runs, so secrets are scrubbed) —
                ideal when you trust the input and per-item LLM checks would be too slow.
                (The optional <code>scope</code> / <code>category</code> /{" "}
                <code>source</code> args are accepted but not yet honored by the
                backend — scope and category are derived during ingestion.)
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_add_insights(insights, on_conflict?, raw=False)</code>
              </td>
              <td>
                Bulk sibling of <code>praxis_add_insight</code>: store many
                already-distilled insights in ONE round-trip (e.g. a whole session&apos;s
                learnings). <code>insights</code> is a list of{" "}
                <code>{`{ insight, scope?, category?, source?, meta?, derived_from? }`}</code>{" "}
                objects; <code>on_conflict</code> is batch-level
                (<code>auto_resolve</code> | <code>surface</code>). Returns one result per
                item (<code>ok</code>/<code>id</code>/<code>action</code>/<code>retrievable</code>);
                a bad item never aborts the rest. Set <code>raw=True</code> for a{" "}
                <strong>fast trusted bulk insert</strong> that skips dedup +
                conflict/LLM for the whole batch (keeps redaction) — the fast lane for
                large trusted loads that would otherwise time out on per-item LLM
                conflict checks.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_list_graph(state?)</code>
              </td>
              <td>
                List the <em>entire</em> graph (not similarity-ranked). Optionally filter
                by <code>state</code> (<code>active</code>, <code>proposed</code>,{" "}
                <code>decayed</code>). Use it to audit what is stored and find fact ids.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_insert_fact(title, content, provenance?)</code>
              </td>
              <td>
                <strong>Raw</strong> direct insert — bypasses the ingestion pipeline (no
                redact/dedup/conflict) and lands in <code>proposed</code> for review. For
                normal approved knowledge prefer <code>praxis_add_insight</code>.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_edit_fact(cid, title?, content?, provenance?)</code>
              </td>
              <td>Edit an existing fact in place; pass only the fields to change.</td>
            </tr>
            <tr>
              <td>
                <code>praxis_get_contradictions()</code>
              </td>
              <td>
                List flagged contradiction pairs (both sides kept until resolved), with
                each side&apos;s id, state, and content.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_resolve_contradiction(pair_id, keep_id?, custom_text?)</code>
              </td>
              <td>
                Settle a pair — keep one side (<code>keep_id</code>) or replace both with a
                single reconciled fact (<code>custom_text</code>).
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_promote_fact(cid, target_state?)</code>
              </td>
              <td>
                Promote a fact through its lifecycle (e.g. <code>proposed</code> →{" "}
                <code>active</code>); omit <code>target_state</code> to advance one step.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_reject_fact(cid, reason?)</code>
              </td>
              <td>
                Reject a fact so retrieval stops reading it (the row is kept in a rejected
                state); optional <code>reason</code> for the audit trail.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_delete_fact(cid)</code>
              </td>
              <td>
                Permanently remove a fact from the graph (unlike reject, the row is gone).
              </td>
            </tr>
            <tr>
              <td colSpan={2} className="mcp-tools-table__group">
                <strong>Compounding loop (H1 / H4 / H5)</strong>
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_record_outcome(fact_id, outcome)</code>
              </td>
              <td>
                <strong>H1 trust.</strong> Record whether acting on a fact{" "}
                <code>succeeded</code> or <code>failed</code> (bool-ish synonyms
                accepted). Outcomes feed each fact&apos;s Laplace-smoothed utility so
                verified facts rank higher in recall and repeatedly-failed ones drop.
              </td>
            </tr>
            <tr>
              <td>
                <code>
                  praxis_record_episode(text, alternatives?, outcome=&quot;pending&quot;,
                  derived_from?, decided_at?)
                </code>
              </td>
              <td>
                <strong>H4 episodes.</strong> Append an immutable entry to the episodic
                log (a decision, its alternatives, and how it turned out). Append-only —
                it skips the dedup/contradiction pipeline and is excluded from{" "}
                <code>praxis_get_context</code> unless <code>include_episodic=true</code>.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_ingest(..., derived_from?)</code>
              </td>
              <td>
                The full ingestion path also accepts <code>derived_from</code> (source
                fact ids), recording the same H5 <code>derived_from</code> edges as{" "}
                <code>praxis_add_insight</code>.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_ingest_session(narrative, source?)</code>
              </td>
              <td>
                Distill a solved-problem coding session (the problem, what failed, the
                fix, why it works) into <code>proposed</code> candidates staged for human
                review — NOT added active. The <code>/ce-compound</code>-style capture
                path; <code>source</code>, if given, must look like{" "}
                <code>session/&lt;id&gt;</code>.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_dependents(fact_id)</code>
              </td>
              <td>
                <strong>H5 traversal.</strong> List the downstream learnings derived from
                a fact (the other end of its <code>derived_from</code> edges).
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_get_stale_derivations()</code>
              </td>
              <td>
                <strong>H5 staleness.</strong> List facts whose derivation source was
                invalidated, so a stale learning can be re-derived or retired.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_get_fact(cid)</code>
              </td>
              <td>
                Full candidate detail for one fact, including its <code>meta</code> — the
                meta read path, since <code>praxis_get_context</code> hits omit{" "}
                <code>meta</code>.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_save_snapshot(name)</code>
              </td>
              <td>
                Dump the current live graph to a snapshot named <code>name</code> (creates
                or overwrites).
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_load_snapshot(name, mode="replace"|"add")</code>
              </td>
              <td>
                Load a snapshot into the live graph. <code>replace</code> (default)
                truncates first; <code>add</code> merges, replacing only shared ids.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_list_snapshots()</code>
              </td>
              <td>List your saved snapshots with node counts and creation times.</td>
            </tr>
            <tr>
              <td>
                <code>praxis_delete_snapshot(name)</code>
              </td>
              <td>Delete a saved snapshot (the live graph is unaffected).</td>
            </tr>
            <tr>
              <td>
                <code>praxis_clear_graph()</code>
              </td>
              <td>
                Truncate your entire live graph (destructive — save a snapshot first).
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_list_org_sources()</code>
              </td>
              <td>
                List org members and their snapshots you can browse and fold in.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_browse_snapshot(user_id, name)</code>
              </td>
              <td>
                Inspect a member&apos;s snapshot facts (grouped by folder) before folding
                them in.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_fold_in(source_user, snapshot, fact_ids, mode="add")</code>
              </td>
              <td>
                Copy chosen facts from a member&apos;s snapshot into your graph — deduped,
                with value conflicts flagged (never silently overwritten).
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_mount_snapshot(snapshot, source_user?)</code>
              </td>
              <td>
                Mount a snapshot (yours or a member&apos;s) as a <strong>read-only
                overlay</strong>: its facts are recalled by <code>praxis_get_context</code>{" "}
                but are not merged into your live graph and are not carried over when you
                save a snapshot.
              </td>
            </tr>
            <tr>
              <td>
                <code>praxis_unmount_snapshot(snapshot, source_user?)</code> /{" "}
                <code>praxis_list_mounts()</code>
              </td>
              <td>
                Remove a mounted overlay, or list what you currently have mounted.
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
        <CommandBlock command={'praxis_add_insight("use uv, not pip, in this repo")'} />
        <CommandBlock command={'praxis_get_context("how do I install deps in this repo?")'} />
      </div>
    </section>
  );
}
