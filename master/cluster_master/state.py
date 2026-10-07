"""Everything the four listener apps share (one process, one state)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from cluster_common.redact import Redactor, default_redactor

from . import __version__
from .audit import AuditLog
from .auth.resolvers import ServiceTokens
from .config import MasterConfig
from .db import Database
from .events import EventBus
from .services.alerts import AlertService
from .services.lockdown import LockdownService
from .services.metrics_store import MetricsStore
from .services.nodes import NodeRegistry
from .ws.agent_hub import AgentHub
from .ws.ui_hub import UiHub


@dataclass
class AppState:
    config: MasterConfig
    db: Database
    bus: EventBus
    audit: AuditLog
    nodes: NodeRegistry
    metrics: MetricsStore
    alerts: AlertService
    lockdown: LockdownService
    service_tokens: ServiceTokens
    hub: AgentHub
    ui: UiHub
    redactor: Redactor = default_redactor
    started_at: float = field(default_factory=time.time)
    version: str = __version__

    @classmethod
    def build(cls, config: MasterConfig, redactor: Redactor = default_redactor) -> AppState:
        db = Database(config.db_path)
        bus = EventBus()
        audit = AuditLog(db, redactor)
        nodes = NodeRegistry(db, audit, bus)
        metrics = MetricsStore(db, ring_size=config.agent.ring_size)
        alerts = AlertService(db, bus)
        lockdown = LockdownService(db, audit, bus)
        tokens = ServiceTokens(db, audit)
        hub = AgentHub(config.agent, nodes, metrics, alerts, lockdown, bus, audit)
        ui = UiHub(bus, origins=config.web.origins)
        return cls(
            config=config,
            db=db,
            bus=bus,
            audit=audit,
            nodes=nodes,
            metrics=metrics,
            alerts=alerts,
            lockdown=lockdown,
            service_tokens=tokens,
            hub=hub,
            ui=ui,
            redactor=redactor,
        )

    async def start(self) -> None:
        await self.db.migrate()
        await self.nodes.load()
        await self.lockdown.load()
        await self.hub.start()
        await self.ui.start()

    async def stop(self) -> None:
        await self.ui.stop()
        await self.hub.stop()
        try:
            await self.metrics.rollup()
        finally:
            await self.db.close()

    async def summary(self) -> dict:
        ids = self.nodes.ids()
        online = self.nodes.online_ids()
        return {
            "version": self.version,
            "ts": time.time(),
            "uptime_s": int(time.time() - self.started_at),
            "nodes_total": len(ids),
            "nodes_online": len(online),
            "nodes_offline": len(ids) - len(online),
            "lockdown": self.lockdown.view(),
            "alerts_open": await self.alerts.open_count(),
        }


__all__ = ["AppState"]
