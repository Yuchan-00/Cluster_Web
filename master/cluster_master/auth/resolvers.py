"""Per-listener principal resolvers and the service-token store.

web      -> Phase 2: the loopback development admin (dev.unauthenticated_admin); Phase 3 adds
            sessions, TOTP and roles.
internal -> `Authorization: Bearer cst_...` service tokens (telegram-bot, ai-operator).
admin    -> the 0600 root-only UNIX socket; `X-Cli-User` is attribution only.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
from typing import Any

from starlette.requests import HTTPConnection

from ..audit import AuditLog
from ..db import Database, SyncDB
from ..secrets import SERVICE_PREFIX, hash_token, is_token, new_token, token_matches
from .authz import AuthError
from .principal import DEV_ADMIN, SERVICE_SCOPES, Principal, root_principal

log = logging.getLogger(__name__)
SERVICE_PRINCIPALS = ("telegram-bot", "ai-operator", "test-client")
ON_BEHALF_OF = re.compile(r"^[A-Za-z0-9_.@:-]{1,64}$")


def client_ip(conn: HTTPConnection) -> str | None:
    client = conn.scope.get("client")
    return client[0] if client else None


def _is_loopback(ip: str | None) -> bool:
    if ip is None:
        return True  # UNIX sockets have no peer address
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        return False


def bearer_token(conn: HTTPConnection) -> str | None:
    header = conn.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


# -- web --------------------------------------------------------------------------------------


class DevAdminResolver:
    """Every loopback request is the admin user `dev`. Only for Phase 2 and the mock cluster."""

    def __init__(self) -> None:
        log.warning(
            "dev.unauthenticated_admin is ON: every loopback web request acts as admin. "
            "Never expose this listener."
        )

    async def __call__(self, conn: HTTPConnection) -> Principal:
        ip = client_ip(conn)
        if not _is_loopback(ip):
            raise AuthError("development admin is loopback-only")
        return Principal(kind="user", id=DEV_ADMIN.id, channel="web", role=DEV_ADMIN.role, ip=ip)


class DenyAllResolver:
    """Until Phase 3 brings sessions, a web listener without the dev shortcut accepts nobody."""

    async def __call__(self, conn: HTTPConnection) -> Principal:
        raise AuthError("login is not available yet (Phase 3)")


# -- internal (service tokens) ---------------------------------------------------------------


class ServiceTokens:
    def __init__(self, db: Database, audit: AuditLog) -> None:
        self.db = db
        self.audit = audit

    async def create(
        self,
        principal: str,
        scopes: list[str],
        *,
        actor: tuple[str, str, str],
        ip: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        if principal not in SERVICE_PRINCIPALS:
            raise ValueError(f"principal must be one of {', '.join(SERVICE_PRINCIPALS)}")
        bad = sorted(set(scopes) - set(SERVICE_SCOPES))
        if bad or not scopes:
            raise ValueError(f"scopes must be a non-empty subset of {', '.join(SERVICE_SCOPES)}")
        token = new_token(SERVICE_PREFIX)
        scopes_json = json.dumps(sorted(set(scopes)))
        now_ms = int(time.time() * 1000)

        def _insert(db: SyncDB) -> dict[str, Any]:
            with db.transaction(immediate=True):
                cur = db.execute(
                    "INSERT INTO service_tokens (principal, token_hash, scopes, created_ms)"
                    " VALUES (?, ?, ?, ?)",
                    (principal, hash_token(token), scopes_json, now_ms),
                )
                row = dict(db.fetchone("SELECT * FROM service_tokens WHERE id=?", (cur.lastrowid,)))
            row.pop("token_hash")
            return row

        row = await self.db.run(_insert)
        actor_type, actor_id, channel = actor
        await self.audit.record(
            actor_type=actor_type,
            actor_id=actor_id,
            channel=channel,
            action="service_token.create",
            target=principal,
            detail={"id": row["id"], "scopes": sorted(set(scopes))},
            ip=ip,
        )
        return row, token

    async def revoke(
        self, token_id: int, *, actor: tuple[str, str, str], ip: str | None = None
    ) -> bool:
        now_ms = int(time.time() * 1000)

        def _revoke(db: SyncDB) -> bool:
            with db.transaction(immediate=True):
                cur = db.execute(
                    "UPDATE service_tokens SET revoked_ms=? WHERE id=? AND revoked_ms IS NULL",
                    (now_ms, token_id),
                )
                return cur.rowcount == 1

        ok = await self.db.run(_revoke)
        if ok:
            actor_type, actor_id, channel = actor
            await self.audit.record(
                actor_type=actor_type,
                actor_id=actor_id,
                channel=channel,
                action="service_token.revoke",
                target=str(token_id),
                ip=ip,
            )
        return ok

    async def list(self) -> list[dict[str, Any]]:
        def _list(db: SyncDB) -> list[dict[str, Any]]:
            rows = db.fetchall(
                "SELECT id, principal, scopes, created_ms, revoked_ms FROM service_tokens"
                " ORDER BY id"
            )
            out = []
            for r in rows:
                d = dict(r)
                d["scopes"] = json.loads(d["scopes"])
                out.append(d)
            return out

        return await self.db.run(_list)

    async def authenticate(self, token: str) -> Principal | None:
        if not is_token(token, SERVICE_PREFIX):
            return None
        digest = hash_token(token)
        row = await self.db.run(
            lambda db: db.fetchone(
                "SELECT principal, scopes, token_hash FROM service_tokens"
                " WHERE token_hash=? AND revoked_ms IS NULL",
                (digest,),
            )
        )
        # The lookup is by hash (a unique index); compare_digest keeps the final check uniform.
        if row is None or not token_matches(token, row["token_hash"]):
            return None
        return Principal(
            kind="service",
            id=row["principal"],
            channel=_channel_for(row["principal"]),
            scopes=frozenset(json.loads(row["scopes"])),
        )


def _channel_for(principal: str) -> str:
    return {"telegram-bot": "telegram", "ai-operator": "ai"}.get(principal, "system")


class ServiceTokenResolver:
    def __init__(self, tokens: ServiceTokens) -> None:
        self.tokens = tokens

    async def __call__(self, conn: HTTPConnection) -> Principal:
        token = bearer_token(conn)
        if token is None:
            raise AuthError("service token required")
        principal = await self.tokens.authenticate(token)
        if principal is None:
            log.warning("internal: rejected service token from %s", client_ip(conn) or "uds")
            raise AuthError("invalid service token")
        acting_for = conn.headers.get("x-on-behalf-of")
        if acting_for:
            # Attribution only (the audit row's on_behalf_of); the service vouches for it.
            # Phase 5 binds Telegram chat ids to users and checks this against that table.
            if not ON_BEHALF_OF.fullmatch(acting_for):
                raise AuthError("invalid X-On-Behalf-Of")
            principal = Principal(
                kind=principal.kind,
                id=principal.id,
                channel=principal.channel,
                scopes=principal.scopes,
                on_behalf_of=acting_for,
            )
        return principal


# -- admin socket -----------------------------------------------------------------------------


class AdminSocketResolver:
    async def __call__(self, conn: HTTPConnection) -> Principal:
        if conn.scope.get("client") is not None:
            # Only reachable over the UNIX socket; a TCP client means a misconfiguration.
            raise AuthError("admin API is only served on the admin socket")
        return root_principal(conn.headers.get("x-cli-user"))


__all__ = [
    "SERVICE_PRINCIPALS",
    "AdminSocketResolver",
    "DenyAllResolver",
    "DevAdminResolver",
    "ServiceTokenResolver",
    "ServiceTokens",
    "bearer_token",
    "client_ip",
]
