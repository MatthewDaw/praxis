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

/**
 * SINGLE SOURCE OF TRUTH for the REMOTE deployed Praxis backend the setup prompt
 * points every agent at — so nobody has to boot a local backend. Edit these here
 * (and nowhere else) if the deployment moves.
 *
 * PRAXIS_API_BASE_URL is the AWS App Runner service that serves the FastAPI
 * backend (`knowledge/serve`). It is the URL the MCP tools + agent-factory gate
 * call over HTTP — verify with `curl <url>/health` returning
 * `{"status":"ok","store":"postgres"}` (JSON, NOT the SPA's <!doctype html>). If
 * the backend is redeployed to a new App Runner service the host changes; refresh
 * it with `aws apprunner list-services`.
 *
 * These are hard-coded literals (like REPO_DIR) rather than read from
 * import.meta.env on purpose: the prompt must ALWAYS point at remote, even when
 * the dashboard itself is running against a local `VITE_PRAXIS_API_BASE_URL`.
 */
export const PRAXIS_API_BASE_URL = "https://uvrzcth5sx.us-east-1.awsapprunner.com";

/**
 * The human-facing frontend (CloudFront-served React SPA). Reference only — this
 * is NOT an API base. Hitting /health or /spaces here returns HTML, so it must
 * never be used as PRAXIS_API_BASE_URL.
 */
export const PRAXIS_FRONTEND_URL = "https://djuqmwjrcs2yx.cloudfront.net";

/**
 * Cognito pool that `praxis_login` authenticates against (the deployed backend's
 * PraxisAuthUserPoolStack). Pool id + client id are public client identifiers
 * (they ship in the frontend bundle), not secrets.
 */
export const COGNITO_USER_POOL_ID = "us-east-1_dqDCickOP";
export const COGNITO_CLIENT_ID = "3v12800pitg5l6tm4er2qati8g";
export const COGNITO_REGION = "us-east-1";
