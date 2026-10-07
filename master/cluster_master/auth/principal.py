"""Who is calling (docs/design/security.md 7.1-7.3).

Three kinds of principal reach the master: a user (web session; in Phase 2 only the loopback
development admin), a service (telegram-bot, ai-operator; bearer `cst_` token on the internal
socket) and root (the admin socket, access enforced by the kernel through the socket mode).
"""

from __future__ import annotations

from dataclasses import dataclass, field

ROLES = ("viewer", "operator", "admin")
_RANK = {role: i for i, role in enumerate(ROLES)}
SERVICE_SCOPES = ("read", "approve", "command", "ai")
CHANNELS = ("web", "telegram", "ai", "cli", "system")


@dataclass(frozen=True)
class Principal:
    kind: str  # user | service | root
    id: str
    channel: str
    role: str | None = None  # users: viewer | operator | admin; root is always admin
    scopes: frozenset[str] = field(default_factory=frozenset)  # services
    ip: str | None = None
    on_behalf_of: str | None = None  # service acting for a user (Telegram, AI)

    def has_role(self, role: str) -> bool:
        if self.kind == "root":
            return True
        if self.kind != "user" or self.role is None:
            return False
        return _RANK[self.role] >= _RANK[role]

    def has_scope(self, scope: str) -> bool:
        return self.kind == "service" and scope in self.scopes

    def audit_actor(self) -> tuple[str, str, str]:
        """(actor_type, actor_id, channel) as the audit log wants them."""
        actor_type = {"user": "user", "service": "service", "root": "cli"}[self.kind]
        return actor_type, self.id, self.channel

    def describe(self) -> str:
        if self.kind == "user":
            return f"user:{self.id}({self.role})"
        if self.kind == "service":
            return f"service:{self.id}[{','.join(sorted(self.scopes))}]"
        return f"root:{self.id}"


DEV_ADMIN = Principal(kind="user", id="dev", channel="web", role="admin", ip="127.0.0.1")


def root_principal(cli_user: str | None, ip: str | None = None) -> Principal:
    # The admin socket is 0600 root:root; a header naming the sudo user is attribution only.
    user = cli_user if cli_user and len(cli_user) <= 64 and cli_user.isprintable() else "root"
    return Principal(kind="root", id=user, channel="cli", role="admin", ip=ip)


__all__ = ["CHANNELS", "DEV_ADMIN", "ROLES", "SERVICE_SCOPES", "Principal", "root_principal"]
