"""Tests for the org-creation CLI, using an in-memory stand-in for the database."""

from __future__ import annotations

from typing import Any

from knowledge.serve import orgs_cli


class _Result:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def fetchall(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class _FakeConn:
    """Just enough SQL for OrgsStore and apikeys.mint_key."""

    def __init__(self) -> None:
        self.orgs: dict[str, str] = {}
        self.members: list[tuple[str, str, str]] = []
        self.keys: list[tuple[Any, ...]] = []

    def execute(self, sql: str, params: Any = ()) -> _Result:
        s = " ".join(sql.split())
        if s.startswith("SELECT 1 FROM orgs WHERE org_id"):
            return _Result([(1,)] if params[0] in self.orgs else [])
        if s.startswith("INSERT INTO orgs"):
            self.orgs[params[0]] = params[1]
            return _Result([])
        if s.startswith("INSERT INTO org_members"):
            self.members.append((params[0], params[1], params[2]))
            return _Result([])
        if s.startswith("INSERT INTO api_keys"):
            self.keys.append(tuple(params))
            return _Result([])
        if s.startswith("SELECT org_id, name FROM orgs"):
            return _Result([(k, v) for k, v in sorted(self.orgs.items())])
        if s.startswith("SELECT m.org_id, o.name, m.role"):
            return _Result(
                [(o, self.orgs[o], r) for o, u, r in self.members if u == params[0]]
            )
        if s.startswith("SELECT user_id, role FROM org_members"):
            return _Result([(u, r) for o, u, r in self.members if o == params[0]])
        raise AssertionError(f"unexpected SQL: {s}")


def _run(argv: list[str], conn: _FakeConn) -> int:
    return orgs_cli._main(argv, connect=lambda: conn)


def test_create_makes_org_with_owner_and_prints_generated_password(capsys):
    conn = _FakeConn()
    assert _run(["create", "--org", "acme", "--owner", "sub-1", "--name", "Acme"], conn) == 0
    out = capsys.readouterr().out
    assert conn.orgs == {"acme": "Acme"}
    assert conn.members == [("acme", "sub-1", "owner")]
    assert "join password" in out
    assert conn.keys == []


def test_create_with_given_password_does_not_print_it(capsys):
    conn = _FakeConn()
    assert _run(["create", "--org", "acme", "--owner", "sub-1", "--password", "hunter2"], conn) == 0
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "join password" not in out


def test_create_existing_org_fails_without_side_effects(capsys):
    conn = _FakeConn()
    assert _run(["create", "--org", "acme", "--owner", "sub-1"], conn) == 0
    assert _run(["create", "--org", "acme", "--owner", "sub-2"], conn) == 1
    assert "already exists" in capsys.readouterr().out
    assert conn.members == [("acme", "sub-1", "owner")]


def test_create_can_mint_a_key_for_the_owner(capsys):
    conn = _FakeConn()
    assert _run(["create", "--org", "acme", "--owner", "sub-1", "--mint-key", "--label", "ci"], conn) == 0
    assert "raw key" in capsys.readouterr().out
    assert len(conn.keys) == 1
    _key_id, _hash, org_id, user_id, label = conn.keys[0]
    assert (org_id, user_id, label) == ("acme", "sub-1", "ci")


def test_list_and_members(capsys):
    conn = _FakeConn()
    _run(["create", "--org", "acme", "--owner", "sub-1", "--name", "Acme"], conn)
    capsys.readouterr()
    assert _run(["list"], conn) == 0
    assert "acme  name=Acme" in capsys.readouterr().out
    assert _run(["list", "--user", "sub-1"], conn) == 0
    assert "role=owner" in capsys.readouterr().out
    assert _run(["members", "--org", "acme"], conn) == 0
    assert "sub-1  role=owner" in capsys.readouterr().out
