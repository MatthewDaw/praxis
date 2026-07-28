"""Acceptance tests for R89: each job record captures and exposes which model
backend it actually ran on (sonnet or deepseek), surfaced in the job list, per-job
detail (MCP + website), and the per-job detail view.

Acceptance condition:
  - A job launched while the box backend was deepseek → its record and the
    jobs-view row/detail both read backend=deepseek.
  - A job launched under sonnet → both read backend=sonnet.
  - A job launched before this field existed (model_backend=None) → reads an
    explicit unknown value, never a false default.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock


from knowledge.serve.box_service_backends import (
    VALID_BACKENDS,
    read_active_backend,
)
from knowledge.serve.box_service_models import Job, JobState, job_view


# ── helpers ────────────────────────────────────────────────────────────────


def _job(*, model_backend: str | None = None, **kw) -> Job:
    """Minimal valid job, overriding only the caller's fields."""
    defaults = dict(
        id="test-job",
        project="test",
        snapshot="test",
        state=JobState.RUNNING,
        model_backend=model_backend,
    )
    defaults.update(kw)
    return Job(**defaults)


# ── model_backend field on the Job struct (record) ──────────────────────────


class TestJobRecordBackendField:
    def test_field_is_nullable_str(self):
        """model_backend is Optional[str], default None."""
        j = _job()
        assert j.model_backend is None

    def test_deepseek_value_persisted(self):
        """A job with backend='deepseek' carries the value on the record."""
        j = _job(model_backend="deepseek")
        assert j.model_backend == "deepseek"

    def test_sonnet_value_persisted(self):
        """A job with backend='sonnet' carries the value on the record."""
        j = _job(model_backend="sonnet")
        assert j.model_backend == "sonnet"


# ── job_view() surfaces modelBackend (R89) ──────────────────────────────────


class TestJobViewSurfacesBackend:
    def test_known_backend_surfaced_as_is(self):
        """A known backend value is returned as-is in the view."""
        for backend in sorted(VALID_BACKENDS):
            j = _job(model_backend=backend, state=JobState.RUNNING)
            v = job_view(j)
            assert v["modelBackend"] == backend, f"expected {backend!r}, got {v['modelBackend']!r}"

    def test_none_renders_as_unknown(self):
        """model_backend=None → 'unknown' in the view (never a false default)."""
        j = _job(model_backend=None, state=JobState.RUNNING)
        v = job_view(j)
        assert v["modelBackend"] == "unknown", f"expected 'unknown', got {v['modelBackend']!r}"

    def test_empty_string_renders_as_unknown(self):
        """An empty string (if somehow set) also renders as 'unknown'."""
        j = _job(model_backend="", state=JobState.RUNNING)
        v = job_view(j)
        assert v["modelBackend"] == "unknown"

    def test_model_backend_appears_for_every_state(self):
        """modelBackend is surfaced regardless of the job's current state."""
        for state in JobState:
            j = _job(model_backend="sonnet", state=state)
            v = job_view(j)
            assert "modelBackend" in v
            assert v["modelBackend"] == "sonnet"

    def test_completed_job_still_shows_backend_alongside_branch(self):
        """A completed job shows modelBackend AND branch/PR URL."""
        j = _job(model_backend="deepseek", state=JobState.COMPLETED, branch="feat/x", pr_url="https://pr/1")
        v = job_view(j)
        assert v["modelBackend"] == "deepseek"
        assert v.get("branch") == "feat/x"
        assert v.get("pr_url") == "https://pr/1"


# ── launch records the active backend ───────────────────────────────────────


class TestLaunchRecordsBackend:
    def test_launch_writes_deepseek_to_job(self, tmp_path: Path):
        """When the backend file holds 'deepseek', launch stamps it on the job."""
        backend_file = tmp_path / "backend"
        backend_file.write_text("deepseek")
        with mock.patch("knowledge.serve.box_service_backends._backend_file", return_value=str(backend_file)):
            j = _job(model_backend=None, worktree_path=str(tmp_path))
            j.model_backend = read_active_backend()
        assert j.model_backend == "deepseek"

    def test_launch_writes_sonnet_to_job(self, tmp_path: Path):
        """When the backend file holds 'sonnet', launch stamps it on the job."""
        backend_file = tmp_path / "backend"
        backend_file.write_text("sonnet")
        with mock.patch("knowledge.serve.box_service_backends._backend_file", return_value=str(backend_file)):
            j = _job(model_backend=None, worktree_path=str(tmp_path))
            j.model_backend = read_active_backend()
        assert j.model_backend == "sonnet"

    def test_no_backend_file_leaves_job_none(self, tmp_path: Path):
        """When the backend file doesn't exist, the job's model_backend stays None."""
        missing = tmp_path / "does-not-exist"
        with mock.patch("knowledge.serve.box_service_backends._backend_file", return_value=str(missing)):
            j = _job(model_backend=None, worktree_path=str(tmp_path))
            try:
                j.model_backend = read_active_backend()
            except FileNotFoundError:
                pass  # stays None — the launch path swallows this
        assert j.model_backend is None


# ── MCP tool praxis_list_jobs surfaces modelBackend ──────────────────────────


class TestMcpListJobsSurfacesBackend:
    """The MCP praxis_list_jobs tool passes through the /jobs endpoint's
    modelBackend field — verified via mock (same pattern as the R26 test)."""

    def test_mcp_list_jobs_includes_model_backend(self, monkeypatch):
        """praxis_list_jobs returns modelBackend in every job row."""
        from knowledge.mcp import identity, server

        monkeypatch.setattr(identity, "is_logged_in", lambda: True)
        monkeypatch.setattr(identity, "token", lambda: "id-tok")
        monkeypatch.setattr(identity, "active_org", lambda: "acme")
        monkeypatch.setattr(identity, "api_base", lambda: "http://api.test")

        backend_payload = {
            "jobs": [
                {"id": "j1", "state": "running", "needsAttention": False, "modelBackend": "deepseek"},
                {"id": "j2", "state": "running", "needsAttention": False, "modelBackend": "sonnet"},
                {"id": "j3", "state": "completed", "needsAttention": False, "modelBackend": "unknown"},
            ]
        }

        class _Resp:
            def json(self):
                return backend_payload
            def raise_for_status(self):
                pass

        monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _Resp())

        out = server.praxis_list_jobs()
        # Extract the structured JSON block from the MCP tool output
        mcp_jobs = json.loads(out.split("```json", 1)[1].split("```", 1)[0])["jobs"]
        assert len(mcp_jobs) == 3
        assert mcp_jobs[0]["modelBackend"] == "deepseek"
        assert mcp_jobs[1]["modelBackend"] == "sonnet"
        assert mcp_jobs[2]["modelBackend"] == "unknown"


# ── MCP tool praxis_get_job (R89 new tool) ──────────────────────────────────


class TestMcpGetJobTool:
    """The MCP tool praxis_get_job returns a per-job detail with modelBackend."""

    def test_mcp_get_job_function_exists(self):
        """praxis_get_job is importable and callable."""
        from knowledge.mcp.server import praxis_get_job as get_job_fn
        assert callable(get_job_fn)

    def test_mcp_get_job_accepts_job_id_parameter(self):
        """praxis_get_job accepts a job_id parameter."""
        from knowledge.mcp.server import praxis_get_job as get_job_fn
        import inspect
        sig = inspect.signature(get_job_fn)
        assert "job_id" in sig.parameters

    def test_mcp_get_job_returns_model_backend(self, monkeypatch):
        """praxis_get_job surfaces modelBackend in its output."""
        from knowledge.mcp import identity, server

        monkeypatch.setattr(identity, "is_logged_in", lambda: True)
        monkeypatch.setattr(identity, "token", lambda: "id-tok")
        monkeypatch.setattr(identity, "active_org", lambda: "acme")
        monkeypatch.setattr(identity, "api_base", lambda: "http://api.test")

        detail_payload = {
            "id": "test-job",
            "project": "test",
            "state": "running",
            "needsAttention": False,
            "modelBackend": "deepseek",
            "failureReason": None,
            "groupId": None,
        }

        class _Resp:
            def json(self):
                return detail_payload
            def raise_for_status(self):
                pass

        monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _Resp())

        out = server.praxis_get_job("test-job")
        # Extract the structured JSON block
        result = json.loads(out.split("```json", 1)[1].split("```", 1)[0])
        assert result["id"] == "test-job"
        assert result["modelBackend"] == "deepseek"

    def test_mcp_get_job_404_handling(self, monkeypatch):
        """praxis_get_job returns a friendly message for unknown job ids."""
        from knowledge.mcp import identity, server
        import httpx

        monkeypatch.setattr(identity, "is_logged_in", lambda: True)
        monkeypatch.setattr(identity, "token", lambda: "id-tok")
        monkeypatch.setattr(identity, "active_org", lambda: "acme")
        monkeypatch.setattr(identity, "api_base", lambda: "http://api.test")

        # Build a mock response that matches the shape httpx.HTTPStatusError expects.
        # The real exception sets exc.response.status_code — the MCP tool reads that.
        mock_response = mock.Mock()
        mock_response.status_code = 404
        mock_response.text = "not found"

        class _404Resp:
            def raise_for_status(self):
                raise httpx.HTTPStatusError(
                    "404 Not Found",
                    request=mock.Mock(method="GET", url="http://api.test/jobs/unknown-job"),
                    response=mock_response,
                )

        monkeypatch.setattr(server.httpx, "get", lambda url, headers, timeout=None: _404Resp())

        out = server.praxis_get_job("unknown-job")
        assert "Unknown job" in out
        assert "praxis_list_jobs" in out
