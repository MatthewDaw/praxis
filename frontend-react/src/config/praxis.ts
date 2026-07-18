/**
 * SINGLE SOURCE OF TRUTH for the local Praxis repo path.
 *
 * This is the ONE place to edit if the repo moves or you set it up on another
 * machine. Every user-facing copy-paste command in the app (the MCP setup tab)
 * derives its absolute paths from here, so nothing else hard-codes the path.
 *
 * It must be an absolute, literal path (the browser can't discover the server's
 * filesystem location), because it is pasted verbatim into shells elsewhere —
 * e.g. `claude mcp add ... uv run --directory <REPO_DIR> ...`.
 */
export const REPO_DIR = "/Users/matthewdaw/Documents/official_repos/praxis";

/**
 * The agent-factory plugin lives as a self-contained subdirectory of the repo,
 * registered as a directory marketplace. Derived from REPO_DIR — never edit
 * separately.
 */
export const FACTORY_DIR = `${REPO_DIR}/agent_factory`;
