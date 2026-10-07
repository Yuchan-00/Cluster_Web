"""`cluster-master-admin`: node registration, tokens, lockdown, audit (docs/PLAN.md 12.4).

With the master running, commands go over the root-only admin socket so the live state is
updated at once. Without it (first install, recovery) the same operations run directly on the
database; the master picks them up at the next start.
"""

from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import socket
import sys
from collections.abc import Sequence
from typing import Any

from cluster_common.redact import default_redactor

from . import __version__
from .audit import AuditLog, verify_sync
from .auth.resolvers import ServiceTokens
from .config import DEFAULT_PATH, ConfigError, MasterConfig, load_config
from .db import Database, SyncDB
from .events import EventBus
from .services.lockdown import LockdownService
from .services.nodes import NodeError, NodeRegistry

ACTOR = ("cli", os.environ.get("SUDO_USER") or os.environ.get("USER") or "root", "cli")


class CliError(Exception):
    pass


# -- transport: HTTP over the admin socket ---------------------------------------------------


class _UdsConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("localhost", timeout=30)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


class AdminSocket:
    def __init__(self, path: str) -> None:
        self.path = path

    def available(self) -> bool:
        if not os.path.exists(self.path):
            return False
        try:
            conn = _UdsConnection(self.path)
            conn.connect()
            conn.close()
            return True
        except OSError:
            return False

    def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        conn = _UdsConnection(self.path)
        headers = {"X-Cli-User": ACTOR[1], "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            payload = resp.read()
        except OSError as exc:
            raise CliError(f"admin socket: {exc}") from exc
        finally:
            conn.close()
        if resp.status == 204:
            return None
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = payload.decode("utf-8", "replace")
        if resp.status >= 400:
            detail = parsed.get("detail") if isinstance(parsed, dict) else parsed
            raise CliError(f"{method} {path}: HTTP {resp.status}: {detail}")
        return parsed


# -- offline mode: straight to the database --------------------------------------------------


class Offline:
    def __init__(self, cfg: MasterConfig) -> None:
        if not os.path.exists(cfg.db_path):
            print(f"note: creating {cfg.db_path}", file=sys.stderr)
        self.db = Database(cfg.db_path)
        self.audit = AuditLog(self.db, default_redactor)
        self.bus = EventBus()
        self.nodes = NodeRegistry(self.db, self.audit, self.bus)
        self.tokens = ServiceTokens(self.db, self.audit)
        self.lockdown = LockdownService(self.db, self.audit, self.bus)

    async def start(self) -> None:
        await self.db.migrate()
        await self.nodes.load()
        await self.lockdown.load()

    async def close(self) -> None:
        await self.db.close()


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- commands ----------------------------------------------------------------------------------


def _labels(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep:
            raise CliError(f"--label expects key=value, got {item!r}")
        if value in ("true", "false"):
            out[key] = value == "true"
        elif value.lstrip("-").isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def _capacity(args: argparse.Namespace) -> dict[str, int] | None:
    cap = {
        k: getattr(args, k)
        for k in ("slots", "bpu_slots", "job_mem_mb")
        if getattr(args, k, None) is not None
    }
    return cap or None


def _emit_token(token: str, token_file: str | None, as_json: bool, extra: dict[str, Any]) -> None:
    if token_file:
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token + "\n")
        if as_json:
            print(json.dumps(dict(extra, token_file=token_file)))
        else:
            print(f"token written to {token_file} (mode 0600). It is not stored anywhere else.")
    elif as_json:
        print(json.dumps(dict(extra, token=token)))
    else:
        print("TOKEN (shown once, store it on the node as /etc/cluster-agent/token):")
        print(token)


def cmd_status(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        _print(ctx.sock.call("GET", "/internal/admin/status"), args)
        return 0
    off = ctx.offline()

    async def _go() -> dict[str, Any]:
        await off.start()
        try:
            db: SyncDB = off.db.sync
            return {
                "master": "not running (offline mode)",
                "db_path": ctx.cfg.db_path,
                "nodes_total": len(off.nodes.ids()),
                "lockdown": off.lockdown.view(),
                "audit_rows": db.fetchone("SELECT COUNT(*) AS n FROM audit_log")["n"],
            }
        finally:
            await off.close()

    _print(_run(_go()), args)
    return 0


def cmd_node_list(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        views = ctx.sock.call("GET", "/internal/admin/nodes")
    else:
        off = ctx.offline()

        async def _go() -> list[dict[str, Any]]:
            await off.start()
            try:
                return off.nodes.views()
            finally:
                await off.close()

        views = _run(_go())
    if args.json:
        print(json.dumps(views, indent=2))
        return 0
    if not views:
        print("no nodes registered")
        return 0
    print(f"{'NODE':<16}{'BOARD':<8}{'STATE':<9}{'TOKEN':<8}{'SCHED':<10}LABELS")
    for v in views:
        state = "online" if v.get("online") else "offline"
        print(
            f"{v['id']:<16}{v['board']:<8}{state:<9}{'yes' if v['has_token'] else 'no':<8}"
            f"{v['sched_state']:<10}{json.dumps(v['labels'], sort_keys=True)}"
        )
    return 0


def cmd_node_register(ctx: Ctx, args: argparse.Namespace) -> int:
    body = {
        "id": args.name,
        "board": args.board,
        "labels": _labels(args.label),
        "capacity": _capacity(args),
    }
    if ctx.sock is not None:
        view = ctx.sock.call("POST", "/internal/admin/nodes", body)
        token = view.pop("token")
    else:
        off = ctx.offline()

        async def _go() -> tuple[dict[str, Any], str]:
            await off.start()
            try:
                record, token = await off.nodes.register(
                    args.name,
                    args.board,
                    labels=body["labels"],
                    capacity=body["capacity"],
                    actor=ACTOR,
                )
                return off.nodes.view(record.id), token
            finally:
                await off.close()

        view, token = _run(_go())
    if not args.json:
        print(f"registered {view['id']} ({view['board']}) labels={json.dumps(view['labels'])}")
    _emit_token(token, args.token_file, args.json, {"id": view["id"]})
    return 0


def cmd_node_rotate(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        token = ctx.sock.call("POST", f"/internal/admin/nodes/{args.name}/token")["token"]
    else:
        off = ctx.offline()

        async def _go() -> str:
            await off.start()
            try:
                return await off.nodes.rotate_token(args.name, actor=ACTOR)
            finally:
                await off.close()

        token = _run(_go())
    _emit_token(token, args.token_file, args.json, {"id": args.name})
    return 0


def cmd_node_revoke(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        ctx.sock.call("DELETE", f"/internal/admin/nodes/{args.name}/token")
    else:
        off = ctx.offline()

        async def _go() -> None:
            await off.start()
            try:
                await off.nodes.revoke_token(args.name, actor=ACTOR)
            finally:
                await off.close()

        _run(_go())
    print(f"token of {args.name} revoked; the agent is refused from now on")
    return 0


def cmd_node_remove(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        ctx.sock.call("DELETE", f"/internal/admin/nodes/{args.name}")
    else:
        off = ctx.offline()

        async def _go() -> None:
            await off.start()
            try:
                await off.nodes.remove(args.name, actor=ACTOR)
            finally:
                await off.close()

        _run(_go())
    print(f"removed {args.name}")
    return 0


def cmd_node_set(ctx: Ctx, args: argparse.Namespace) -> int:
    body: dict[str, Any] = {}
    if args.label:
        body["labels"] = _labels(args.label)
    cap = _capacity(args)
    if cap is not None:
        body["capacity"] = cap
    if args.sched_state:
        body["sched_state"] = args.sched_state
        body["sched_reason"] = args.reason
    if not body:
        raise CliError("nothing to change")
    if ctx.sock is not None:
        view = ctx.sock.call("PATCH", f"/internal/admin/nodes/{args.name}", body)
    else:
        off = ctx.offline()

        async def _go() -> dict[str, Any]:
            await off.start()
            try:
                await off.nodes.update(
                    args.name,
                    labels=body.get("labels"),
                    capacity=body.get("capacity"),
                    sched_state=body.get("sched_state"),
                    sched_reason=body.get("sched_reason"),
                    actor=ACTOR,
                )
                return off.nodes.view(args.name)
            finally:
                await off.close()

        view = _run(_go())
    _print(view, args)
    return 0


def cmd_token_create(ctx: Ctx, args: argparse.Namespace) -> int:
    body = {"principal": args.principal, "scopes": args.scope}
    if ctx.sock is not None:
        row = ctx.sock.call("POST", "/internal/admin/service-tokens", body)
        token = row.pop("token")
    else:
        off = ctx.offline()

        async def _go() -> tuple[dict[str, Any], str]:
            await off.start()
            try:
                return await off.tokens.create(args.principal, args.scope, actor=ACTOR)
            finally:
                await off.close()

        row, token = _run(_go())
    if not args.json:
        print(f"service token #{row['id']} for {row['principal']} scopes={row['scopes']}")
    _emit_token(token, args.token_file, args.json, {"id": row["id"]})
    return 0


def cmd_token_list(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        rows = ctx.sock.call("GET", "/internal/admin/service-tokens")
    else:
        off = ctx.offline()

        async def _go() -> list[dict[str, Any]]:
            await off.start()
            try:
                return await off.tokens.list()
            finally:
                await off.close()

        rows = _run(_go())
    _print(rows, args)
    return 0


def cmd_token_revoke(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        ctx.sock.call("DELETE", f"/internal/admin/service-tokens/{args.id}")
    else:
        off = ctx.offline()

        async def _go() -> bool:
            await off.start()
            try:
                return await off.tokens.revoke(args.id, actor=ACTOR)
            finally:
                await off.close()

        if not _run(_go()):
            raise CliError("unknown or already revoked token")
    print(f"service token #{args.id} revoked")
    return 0


def cmd_lockdown(ctx: Ctx, args: argparse.Namespace) -> int:
    active = args.state == "on"
    body = {"active": active, "reason": args.reason}
    if ctx.sock is not None:
        view = ctx.sock.call("POST", "/internal/admin/lockdown", body)
    else:
        off = ctx.offline()

        async def _go() -> dict[str, Any]:
            await off.start()
            try:
                changed = await off.lockdown.set(active, actor=ACTOR, reason=args.reason)
                view = off.lockdown.view()
                view["changed"] = changed
                view["note"] = "master not running; applies at its next start"
                return view
            finally:
                await off.close()

        view = _run(_go())
    _print(view, args)
    return 0


def cmd_audit_verify(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        result = ctx.sock.call("GET", "/internal/admin/audit/verify")
    else:
        db = SyncDB(ctx.cfg.db_path, read_only=True)
        try:
            result = verify_sync(db).__dict__
        finally:
            db.close()
    _print(result, args)
    return 0 if result["ok"] else 1


def cmd_audit_tail(ctx: Ctx, args: argparse.Namespace) -> int:
    if ctx.sock is not None:
        rows = ctx.sock.call("GET", f"/internal/admin/audit/tail?limit={args.n}")
    else:
        db = SyncDB(ctx.cfg.db_path, read_only=True)
        try:
            rows = [
                dict(r)
                for r in db.fetchall("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (args.n,))
            ]
        finally:
            db.close()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for r in reversed(rows):
        print(
            f"#{r['id']} {r['ts_ms']} {r['actor_type']}:{r['actor_id']}@{r['channel']} "
            f"{r['action']} {r['target'] or ''} {r['detail']}"
        )
    return 0


def cmd_config_check(ctx: Ctx, args: argparse.Namespace) -> int:
    print(f"config ok: db={ctx.cfg.db_path} admin={ctx.cfg.listeners.admin.path}")
    return 0


def _print(obj: Any, args: argparse.Namespace) -> None:
    print(json.dumps(obj, indent=None if getattr(args, "json", False) else 2, sort_keys=True))


# -- wiring ------------------------------------------------------------------------------------


class Ctx:
    def __init__(self, cfg: MasterConfig, sock: AdminSocket | None) -> None:
        self.cfg = cfg
        self.sock = sock

    def offline(self) -> Offline:
        print("master not running: operating on the database directly", file=sys.stderr)
        return Offline(self.cfg)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cluster-master-admin", description=__doc__.split("\n")[0])
    p.add_argument("--config", default=DEFAULT_PATH)
    p.add_argument("--dev", metavar="DATA_DIR", help="use the development master in DATA_DIR")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status").set_defaults(fn=cmd_status)

    node = sub.add_parser("node").add_subparsers(dest="sub", required=True)
    node.add_parser("list").set_defaults(fn=cmd_node_list)
    reg = node.add_parser("register")
    reg.add_argument("name")
    reg.add_argument("--board", required=True, choices=["rpi3", "rdkx3", "generic"])
    reg.add_argument("--label", action="append", metavar="KEY=VALUE")
    reg.add_argument("--slots", type=int)
    reg.add_argument("--bpu-slots", type=int)
    reg.add_argument("--job-mem-mb", type=int)
    reg.add_argument("--token-file", help="write the token here (0600) instead of printing it")
    reg.set_defaults(fn=cmd_node_register)
    rot = node.add_parser("rotate", help="issue a new token (the old one stops working)")
    rot.add_argument("name")
    rot.add_argument("--token-file")
    rot.set_defaults(fn=cmd_node_rotate)
    rev = node.add_parser("revoke", help="disable the node's token")
    rev.add_argument("name")
    rev.set_defaults(fn=cmd_node_revoke)
    rem = node.add_parser("remove")
    rem.add_argument("name")
    rem.set_defaults(fn=cmd_node_remove)
    st = node.add_parser("set", help="change labels, capacity or scheduling state")
    st.add_argument("name")
    st.add_argument("--label", action="append", metavar="KEY=VALUE")
    st.add_argument("--slots", type=int)
    st.add_argument("--bpu-slots", type=int)
    st.add_argument("--job-mem-mb", type=int)
    st.add_argument("--sched-state", choices=["active", "cordoned", "draining", "drained"])
    st.add_argument("--reason")
    st.set_defaults(fn=cmd_node_set)

    tok = sub.add_parser("service-token").add_subparsers(dest="sub", required=True)
    tc = tok.add_parser("create")
    tc.add_argument("principal", choices=["telegram-bot", "ai-operator", "test-client"])
    tc.add_argument(
        "--scope", action="append", required=True, choices=["read", "approve", "command", "ai"]
    )
    tc.add_argument("--token-file")
    tc.set_defaults(fn=cmd_token_create)
    tok.add_parser("list").set_defaults(fn=cmd_token_list)
    tr = tok.add_parser("revoke")
    tr.add_argument("id", type=int)
    tr.set_defaults(fn=cmd_token_revoke)

    ld = sub.add_parser("lockdown")
    ld.add_argument("state", choices=["on", "off"])
    ld.add_argument("--reason")
    ld.set_defaults(fn=cmd_lockdown)

    au = sub.add_parser("audit").add_subparsers(dest="sub", required=True)
    au.add_parser("verify").set_defaults(fn=cmd_audit_verify)
    tail = au.add_parser("tail")
    tail.add_argument("-n", type=int, default=50)
    tail.set_defaults(fn=cmd_audit_tail)

    cfgp = sub.add_parser("config").add_subparsers(dest="sub", required=True)
    cfgp.add_parser("check").set_defaults(fn=cmd_config_check)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.dev:
            from .main import dev_config

            cfg = dev_config(args.dev, 8000, 8001)
        else:
            cfg = load_config(args.config)
        sock = AdminSocket(cfg.listeners.admin.path)
        ctx = Ctx(cfg, sock if sock.available() else None)
        return args.fn(ctx, args)
    except (CliError, ConfigError, NodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except FileExistsError as exc:
        print(
            f"error: {exc.filename} already exists; not overwriting a token file", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
