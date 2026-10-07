"""Web API (docs/PLAN.md 12.2) served on the loopback web listener behind tailscale serve.

Every route declares who may call it with `require(...)`; `check_routes()` refuses to start
the listener otherwise. Phase 2 exposes cluster state and node administration; commands,
jobs and approvals arrive in later phases.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from ..auth.authz import require
from ..auth.principal import Principal
from ..auth.resolvers import client_ip
from ..services.nodes import NodeError, NodeExists, NodeNotFound
from ..state import AppState

router = APIRouter()


def state_of(conn: Request | WebSocket) -> AppState:
    return conn.app.state.cluster


def _node_or_404(st: AppState, node_id: str) -> None:
    if st.nodes.get(node_id) is None:
        raise HTTPException(status_code=404, detail="unknown node")


# -- read ------------------------------------------------------------------------------------


@router.get("/api/cluster/summary")
async def cluster_summary(request: Request, _: Principal = require("viewer")) -> dict[str, Any]:
    return await state_of(request).summary()


@router.get("/api/nodes")
async def list_nodes(request: Request, _: Principal = require("viewer")) -> list[dict[str, Any]]:
    st = state_of(request)
    out = []
    for view in st.nodes.views():
        latest = st.metrics.latest_sample(view["id"])
        view["latest"] = latest.as_dict() if latest else None
        out.append(view)
    return out


@router.get("/api/nodes/{node_id}")
async def get_node(
    node_id: str, request: Request, _: Principal = require("viewer")
) -> dict[str, Any]:
    st = state_of(request)
    _node_or_404(st, node_id)
    view = st.nodes.view(node_id)
    view["latest"] = st.metrics.latest(node_id)
    return view


@router.get("/api/nodes/{node_id}/metrics")
async def node_metrics(
    node_id: str,
    request: Request,
    since: float | None = Query(default=None, ge=0, le=1 << 40),
    limit: int = Query(default=720, ge=1, le=10000),
    history: bool = False,
    _: Principal = require("viewer"),
) -> dict[str, Any]:
    """Recent samples from the ring buffer, or 1-minute rollups with `history=true`."""
    st = state_of(request)
    _node_or_404(st, node_id)
    if history:
        since_ms = int((since if since is not None else time.time() - 24 * 3600) * 1000)
        rows = await st.metrics.history(node_id, since_ms, limit=limit)
        return {"node_id": node_id, "resolution": "1m", "rows": rows}
    samples = st.metrics.recent(node_id, since=since, limit=limit)
    return {"node_id": node_id, "resolution": "raw", "rows": [s.as_dict() for s in samples]}


@router.get("/api/alerts")
async def list_alerts(
    request: Request,
    open: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    before: int | None = Query(default=None, ge=1, le=1 << 62),
    _: Principal = require("viewer"),
) -> list[dict[str, Any]]:
    return await state_of(request).alerts.list(open_only=open, limit=limit, before_id=before)


@router.get("/api/system/status")
async def system_status(request: Request, _: Principal = require("viewer")) -> dict[str, Any]:
    st = state_of(request)
    summary = await st.summary()
    summary["agents"] = st.hub.connections()
    summary["ui_clients"] = st.ui.count()
    return summary


@router.get("/api/audit")
async def audit_tail(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    before: int | None = Query(default=None, ge=1, le=1 << 62),
    _: Principal = require("admin"),
) -> list[dict[str, Any]]:
    return await state_of(request).audit.tail(limit=limit, before_id=before)


@router.get("/api/audit/verify")
async def audit_verify(request: Request, _: Principal = require("admin")) -> dict[str, Any]:
    result = await state_of(request).audit.verify()
    return result.__dict__


# -- node administration ---------------------------------------------------------------------


class NodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # no bool->int or "2"->2 coercion
    id: str = Field(min_length=1, max_length=63)
    board: str
    labels: dict[str, Any] | None = None
    capacity: dict[str, int] | None = None


class NodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    labels: dict[str, Any] | None = None
    capacity: dict[str, int] | None = None
    sched_state: str | None = None
    sched_reason: str | None = Field(default=None, max_length=200)


@router.post("/api/nodes", status_code=201)
async def create_node(
    body: NodeCreate, request: Request, principal: Principal = require("admin")
) -> dict[str, Any]:
    st = state_of(request)
    try:
        record, token = await st.nodes.register(
            body.id,
            body.board,
            labels=body.labels,
            capacity=body.capacity,
            actor=principal.audit_actor(),
            ip=client_ip(request),
        )
    except NodeExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except NodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    view = st.nodes.view(record.id)
    view["token"] = token  # shown once; only the hash is stored
    return view


@router.patch("/api/nodes/{node_id}")
async def patch_node(
    node_id: str, body: NodePatch, request: Request, principal: Principal = require("admin")
) -> dict[str, Any]:
    st = state_of(request)
    _node_or_404(st, node_id)
    if body.sched_reason is not None and body.sched_state is None:
        raise HTTPException(status_code=422, detail="sched_reason requires sched_state")
    if body.labels is None and body.capacity is None and body.sched_state is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    try:
        await st.nodes.update(
            node_id,
            labels=body.labels,
            capacity=body.capacity,
            sched_state=body.sched_state,
            sched_reason=body.sched_reason,
            actor=principal.audit_actor(),
            ip=client_ip(request),
        )
    except NodeNotFound:
        raise HTTPException(status_code=404, detail="unknown node") from None
    except NodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return st.nodes.view(node_id)


@router.post("/api/nodes/{node_id}/token")
async def rotate_token(
    node_id: str, request: Request, principal: Principal = require("admin")
) -> dict[str, Any]:
    st = state_of(request)
    _node_or_404(st, node_id)
    token = await st.nodes.rotate_token(
        node_id, actor=principal.audit_actor(), ip=client_ip(request)
    )
    return {"id": node_id, "token": token}


@router.delete("/api/nodes/{node_id}/token", status_code=204)
async def revoke_token(
    node_id: str, request: Request, principal: Principal = require("admin")
) -> None:
    st = state_of(request)
    _node_or_404(st, node_id)
    await st.nodes.revoke_token(node_id, actor=principal.audit_actor(), ip=client_ip(request))
    return None


@router.delete("/api/nodes/{node_id}", status_code=204)
async def delete_node(
    node_id: str, request: Request, principal: Principal = require("admin")
) -> None:
    st = state_of(request)
    _node_or_404(st, node_id)
    await st.nodes.remove(node_id, actor=principal.audit_actor(), ip=client_ip(request))
    await st.metrics.forget_db(node_id)
    await st.alerts.resolve_node(node_id)
    return None


# -- lockdown --------------------------------------------------------------------------------


class LockdownBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool
    reason: str | None = Field(default=None, max_length=200)


@router.post("/api/system/lockdown")
async def set_lockdown(
    body: LockdownBody, request: Request, principal: Principal = require("admin")
) -> dict[str, Any]:
    st = state_of(request)
    changed = await st.lockdown.set(
        body.active, actor=principal.audit_actor(), reason=body.reason, ip=client_ip(request)
    )
    view = st.lockdown.view()
    view["changed"] = changed
    return view


# -- live events -----------------------------------------------------------------------------


@router.websocket("/ws/ui")
async def ws_ui(ws: WebSocket, principal: Principal = require("viewer")) -> None:
    await state_of(ws).ui.handle(ws, principal)


__all__ = ["router"]
