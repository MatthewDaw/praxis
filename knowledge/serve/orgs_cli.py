"""Operator CLI for creating and inspecting orgs, next to ``knowledge.serve.apikeys``.

Orgs are normally created from the dashboard or the ``praxis_create_org`` MCP tool,
both of which need a live Cognito login. This CLI uses the same database connection
as the server (``knowledge.serve.db.connect()``), so an operator on the box can create
an org and its first API key with no login, which is what unattended provisioning
needs::

    uv run python -m knowledge.serve.orgs_cli create --org acme --owner <cognito-sub> \\
        [--name "Acme"] [--password ...] [--mint-key [--label ci]]
    uv run python -m knowledge.serve.orgs_cli list [--user <cognito-sub>]
    uv run python -m knowledge.serve.orgs_cli members --org acme

The owner is a Cognito ``sub``, because emails are not stored app-side. A password is
generated and printed once when ``--password`` is not given. Secrets are never logged.
"""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Callable
from typing import Any

from knowledge.serve import apikeys
from knowledge.serve.orgs_store import OrgsStore


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="knowledge.serve.orgs_cli")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create an org with its first owner")
    p_create.add_argument("--org", required=True, help="org id, e.g. acme")
    p_create.add_argument("--owner", required=True, help="Cognito sub of the owner")
    p_create.add_argument("--name", default=None, help="display name (default: the org id)")
    p_create.add_argument(
        "--password", default=None, help="join password (default: generate and print once)"
    )
    p_create.add_argument(
        "--mint-key", action="store_true", help="also mint an org-scoped API key for the owner"
    )
    p_create.add_argument("--label", default=None, help="label for the minted key")

    p_list = sub.add_parser("list", help="list orgs")
    p_list.add_argument("--user", default=None, help="only the orgs this Cognito sub belongs to")

    p_members = sub.add_parser("members", help="list an org's members")
    p_members.add_argument("--org", required=True)
    return parser


def _create(conn: Any, args: argparse.Namespace) -> int:
    store = OrgsStore(conn)
    generated = args.password is None
    password = secrets.token_urlsafe(18) if generated else args.password
    try:
        store.create_org(args.org, args.name or args.org, password, args.owner)
    except ValueError as exc:
        print(f"not created: {exc}")
        return 1
    print(f"created org {args.org} (owner {args.owner})")
    if generated:
        print(f"join password (store it now, shown only once): {password}")
    if args.mint_key:
        key_id, raw_key = apikeys.mint_key(conn, args.org, args.owner, args.label)
        print(f"minted key id={key_id} org={args.org}")
        print(f"raw key (store it now, shown only once): {raw_key}")
    return 0


def _main(argv: list[str] | None = None, connect: Callable[[], Any] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if connect is None:
        from dotenv import load_dotenv

        load_dotenv()
        from knowledge.serve import db

        connect = db.connect
        conn = connect()
        db.bootstrap()
    else:
        conn = connect()

    if args.cmd == "create":
        return _create(conn, args)
    store = OrgsStore(conn)
    if args.cmd == "list":
        orgs = store.list_orgs(args.user) if args.user else store.all_orgs()
        for o in orgs:
            role = f"  role={o['role']}" if "role" in o else ""
            print(f"{o['org_id']}  name={o['name']}{role}")
        return 0
    if args.cmd == "members":
        for m in store.members(args.org):
            print(f"{m['user_id']}  role={m['role']}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
