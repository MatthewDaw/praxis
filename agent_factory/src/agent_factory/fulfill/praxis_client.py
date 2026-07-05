"""U5 — the runtime Praxis client (writes + reads), under the existing fail-closed contract.

``hooks/_praxis.py`` is read+outcome focused and lives in ``hooks/`` (a Stop-hook subprocess). The
af-fulfill runtime additionally needs *writes* (create a session space, seed requirement facts, bind
``renders`` edges), so this client mirrors ``_praxis.py``'s auth / header / fail-closed transport
shape rather than importing across the ``hooks/`` ↔ ``src/`` boundary (KTD7 — unifying the two is a
deferred follow-up).

FAIL-CLOSED CONTRACT (same as ``_praxis.py``): Praxis is a HARD dependency. Unreachable / unauth /
server-error → every method raises :class:`PraxisUnreachable`. A session that cannot prove its
completeness state must BLOCK, never fail open.

Per-session isolation (KTD5): a :class:`FulfillPraxis` instance carries a ``space`` — set it to the
session's space id and it rides as the ``x-praxis-space`` header on every call, so the session's
reads/writes hit exactly that space's live graph. ``space=None`` means the default graph.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_API_BASE = "http://localhost:8000"
DEFAULT_ORG = "agent-factory"
_CACHE_PATH = Path.home() / ".praxis" / "mcp.json"
_HTTP_TIMEOUT_S = 30


class PraxisUnreachable(RuntimeError):
    """Praxis could not be reached / authenticated / queried. Callers MUST fail closed (BLOCK)."""


def _load_dotenv() -> None:
    """Load repo-root ``.env`` into ``os.environ`` (without overriding already-set vars).

    Mirrors ``_praxis._load_dotenv``: a runtime entry point may not inherit a shell-sourced ``.env``,
    so the Praxis credentials live in ``<repo>/.env`` and are loaded explicitly. Real env wins.
    """
    candidates = [
        Path(__file__).resolve().parents[3] / ".env",  # <repo>/.env (src/agent_factory/fulfill/..)
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        try:
            if not env_path.is_file():
                continue
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
        except Exception:  # noqa: BLE001 — a malformed .env must not crash the runtime
            continue


_load_dotenv()


def _api_base() -> str:
    return os.environ.get("PRAXIS_API_BASE_URL", DEFAULT_API_BASE).rstrip("/")


def _auth_disabled() -> bool:
    return os.environ.get("PRAXIS_AUTH_DISABLED") == "1"


def _mint_cognito_token() -> str:
    """Mint a fresh Cognito ID token from the cached refresh token, stdlib-only (mirrors _praxis)."""
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        refresh_token = data["refresh_token"]
    except Exception as exc:  # noqa: BLE001
        raise PraxisUnreachable(
            f"no Praxis auth: PRAXIS_API_KEY unset and ~/.praxis/mcp.json unreadable ({exc})"
        ) from exc

    client_id = os.environ.get("COGNITO_CLIENT_ID", "")
    region = os.environ.get("COGNITO_REGION", "us-east-1")
    if not client_id:
        raise PraxisUnreachable(
            "no Praxis auth: PRAXIS_API_KEY unset and COGNITO_CLIENT_ID missing — cannot mint a token"
        )

    url = f"https://cognito-idp.{region}.amazonaws.com/"
    body = json.dumps({
        "AuthFlow": "REFRESH_TOKEN_AUTH",
        "ClientId": client_id,
        "AuthParameters": {"REFRESH_TOKEN": refresh_token},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PraxisUnreachable(f"Cognito token mint failed: {exc}") from exc

    token = (payload.get("AuthenticationResult") or {}).get("IdToken")
    if not token:
        raise PraxisUnreachable("Cognito token mint returned no IdToken")
    return token


_TOKEN_CACHE: dict[str, Any] = {"token": None, "exp": 0.0}


class FulfillPraxis:
    """Per-session Praxis client. Set :attr:`space` to scope every call to a session's space."""

    def __init__(self, space: str | None = None, *, org: str | None = None) -> None:
        self.space = space
        self.org = org

    # --- auth + transport ---------------------------------------------------
    def _auth_headers(self) -> dict[str, str]:
        """Auth + tenancy headers, failing closed if no credential is available (mirrors _praxis)."""
        headers: dict[str, str] = {}
        org = (self.org or os.environ.get("PRAXIS_ORG", DEFAULT_ORG)).strip() or DEFAULT_ORG
        headers["x-praxis-org"] = org
        # The session space rides per-instance; env PRAXIS_SPACE is the fallback default.
        space = (self.space or os.environ.get("PRAXIS_SPACE", "")).strip()
        if space:
            headers["x-praxis-space"] = space

        if _auth_disabled():
            return headers

        api_key = os.environ.get("PRAXIS_API_KEY", "").strip()
        if api_key:
            headers["x-praxis-key"] = api_key
            return headers

        now = time.time()
        if not _TOKEN_CACHE["token"] or now >= _TOKEN_CACHE["exp"]:
            _TOKEN_CACHE["token"] = _mint_cognito_token()
            _TOKEN_CACHE["exp"] = now + 600
        headers["Authorization"] = f"Bearer {_TOKEN_CACHE['token']}"
        return headers

    def _request(self, method: str, path: str, *, params: dict | None = None,
                 body: dict | None = None, not_found_ok: bool = False) -> Any:
        """Issue one HTTP request and return parsed JSON, or raise PraxisUnreachable (fail-closed)."""
        url = _api_base() + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)

        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._auth_headers()
        if data is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and not_found_ok:
                return {}
            detail = ""
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                pass
            raise PraxisUnreachable(f"Praxis {method} {path} -> HTTP {exc.code}: {detail}") from exc
        except Exception as exc:  # noqa: BLE001 (URLError, timeout, JSON, ...)
            raise PraxisUnreachable(f"Praxis {method} {path} failed: {exc}") from exc

    # --- writes -------------------------------------------------------------
    def create_space(self, space_id: str, name: str | None = None) -> dict:
        """Create a fresh private space (a clean session graph). 409 if it already exists."""
        return self._request("POST", "/spaces", body={"spaceId": space_id, "name": name})

    def delete_space(self, space_id: str) -> dict:
        """Permanently delete a session space and its graph (the :meth:`Session.close` hook).

        Per-session space LIFECYCLE/TTL is owned by Praxis (Q7); this is the explicit teardown a
        caller may invoke, not an automatic cleanup policy."""
        return self._request("DELETE", f"/spaces/{urllib.parse.quote(space_id, safe='')}")

    def ingest_requirement(self, *, text: str, source: str, scope: str | None,
                           meta: dict) -> str:
        """Seed ONE requirement fact (the shaped-fact fast lane, ``raw=True`` — no LLM distillation).

        Writes ``category="requirement"``, ``source="prd-<project>"`` plus the pack ``meta``
        (requirement_id/field/verify/cover/renders/depends_on/guard). Returns the new fact id (needed
        to bind the ``renders`` edge). Raises if Praxis returns no id (fail-closed)."""
        out = self._request("POST", "/insights", body={
            "insight": text,
            "source": source,
            "scope": scope,
            "category": "requirement",
            "meta": meta,
            "raw": True,
        })
        fact_id = out.get("id")
        if not fact_id:
            raise PraxisUnreachable(f"seed of requirement {meta.get('requirement_id')!r} returned no id")
        return str(fact_id)

    def bind_surface(self, requirement_fact_id: str, screen_id: str, project: str,
                     *, title: str | None = None) -> dict:
        """Bind a requirement fact to a surface via the typed ``renders`` edge (idempotent)."""
        return self._request("POST", "/surfaces/bind", body={
            "requirementFactId": requirement_fact_id,
            "screenId": screen_id,
            "project": project,
            "title": title,
        })

    def ensure_surface(self, project: str, screen_id: str, *, title: str | None = None) -> dict:
        """Idempotently ensure the deliverable surface fact exists for ``(project, screen_id)``."""
        return self._request("POST", "/surfaces", body={
            "project": project, "screenId": screen_id, "title": title,
        })

    def record_outcome(self, cid: str, success: bool) -> dict:
        """Record a verified cover outcome on the requirement fact (flips derived completeness)."""
        return self._request("POST", f"/facts/{cid}/outcome", body={"success": bool(success)})

    # --- reads --------------------------------------------------------------
    @staticmethod
    def _bare_project(project: str) -> str:
        """Strip EVERY leading ``prd-`` so a doubly-prefixed name can't search ``prd-prd-...`` ->
        EMPTY -> a false "all complete" (fail-open). The endpoints prepend ``prd-`` themselves."""
        while project.startswith("prd-"):
            project = project[len("prd-"):]
        return project

    def incomplete_requirements(self, project: str, *, exclude_leased: bool = False) -> list[dict]:
        """Active requirements in ``prd-<project>`` not yet covered. Pass the BARE project name."""
        out = self._request("GET", "/requirements/incomplete",
                            params={"project": self._bare_project(project),
                                    "exclude_leased": str(exclude_leased).lower()})
        return out.get("incomplete") or out.get("requirements") or out.get("items") or []

    def completeness_summary(self, project: str) -> dict:
        """Done-of-definition counts for ``prd-<project>`` (BARE project name)."""
        return self._request("GET", "/requirements/completeness",
                            params={"project": self._bare_project(project)})

    def surface_coverage(self, project: str, *, scope: str | None = None) -> dict:
        """Bidirectional coverage gate: uncovered surfaces + uncovered requirements for ``project``."""
        return self._request("GET", "/surfaces/coverage", params={"project": project, "scope": scope})

    def get_fact(self, cid: str) -> dict:
        """Full fact (candidate view) including ``meta``. Raises on any error."""
        return self._request("GET", f"/candidates/{cid}")

    def ping(self) -> bool:
        """Best-effort liveness check (used by guarded live smoke tests)."""
        self._request("GET", "/health")
        return True
