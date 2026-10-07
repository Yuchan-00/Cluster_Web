"""Internal listeners (docs/PLAN.md 12.4).

`internal_router` is served on /run/cluster-master/internal.sock (0660 root:cluster-svc) for
the service processes with `cst_` tokens: a read-only allowlist in Phase 2, approvals and
commands later. `admin_router` is served on /run/cluster-master/admin.sock (0600 root:root) for
`cluster-master-admin`; the kernel is the authentication, the router only attributes actions.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from ..auth.authz import require
from ..auth.principal import Principal
from ..services.nodes import NodeError, NodeExists, NodeNotFound
from ..state import AppState

internal_router = APIRouter(prefix="/internal/api")
admin_router = APIRouter(prefix="/internal/admin")


def state_of(request: Request) -> AppState:
    return request.app.state.cluster


READ = ("read",)


# -- internal: service tokens ------------------------------------------------------------------


@internal_router.get("/cluster/summary")
async def i_summary(request: Request, _: Principal = require(scopes=READ)) -> dict[str, Any]:
    return await state_of(request).summary()


@internal_router.get("/nodes")
async def i_nodes(request: Request, _: Principal = require(scopes=READ)) -> list[dict[str, Any]]:
    st = state_of(request)
    out = []
    for view in st.nodes.views():
        latest = st.metrics.latest_sample(view["id"])
        view["latest"] = latest.as_dict() if latest else None
        view.pop("static_info", None)  # keep the Telegram/AI view small
        out.append(view)
    return out


@internal_router.get("/nodes/{node_id}")
async def i_node(
    node_id: str, request: Request, _: Principal = require(scopes=READ)
) -> dict[str, Any]:
    st = state_of(request)
    if st.nodes.get(node_id) is None:
        raise HTTPException(status_code=404, detail="unknown node")
    view = st.nodes.view(node_id)
    view["latest"] = st.metrics.latest(node_id)
    return view


@internal_router.get("/alerts")
async def i_alerts(
    request: Request,
    open: bool = True,
    limit: int = Query(default=50, ge=1, le=500),
    _: Principal = require(scopes=READ),
) -> list[dict[str, Any]]:
    return await state_of(request).alerts.list(open_only=open, limit=limit)


@internal_router.get("/whoami")
async def i_whoami(principal: Principal = require(scopes=READ)) -> dict[str, Any]:
    return {"principal": principal.id, "scopes": sorted(principal.scopes)}


# -- admin: the root socket --------------------------------------------------------------------

ROOT = require(root=True)


class AdminNodeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(min_length=1, max_length=63)
    board: str
    labels: dict[str, Any] | None = None
    capacity: dict[str, int] | None = None


class AdminNodePatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    labels: dict[str, Any] | None = None
    capacity: dict[str, int] | None = None
    sched_state: str | None = None
    sched_reason: str | None = Field(default=None, max_length=200)


class AdminLockdown(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    active: bool
    reason: str | None = Field(default=None, max_length=200)


class AdminServiceToken(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    principal: str = Field(max_length=32)
    scopes: list[str] = Field(min_length=1, max_length=8)


@admin_router.get("/status")
async def a_status(request: Request, _: Principal = ROOT) -> dict[str, Any]:
    st = state_of(request)
    summary = await st.summary()
    summary["agents"] = st.hub.connections()
    summary["ui_clients"] = st.ui.count()
    summary["db_path"] = st.config.db_path
    return summary


@admin_router.get("/nodes")
async def a_nodes(request: Request, _: Principal = ROOT) -> list[dict[str, Any]]:
    return state_of(request).nodes.views()


@admin_router.post("/nodes", status_code=201)
async def a_create_node(
    body: AdminNodeCreate, request: Request, principal: Principal = ROOT
) -> dict[str, Any]:
    st = state_of(request)
    try:
        record, token = await st.nodes.register(
            body.id,
            body.board,
            labels=body.labels,
            capacity=body.capacity,
            actor=principal.audit_actor(),
        )
    except NodeExists as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except NodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    view = st.nodes.view(record.id)
    view["token"] = token
    return view


@admin_router.patch("/nodes/{node_id}")
async def a_patch_node(
    node_id: str, body: AdminNodePatch, request: Request, principal: Principal = ROOT
) -> dict[str, Any]:
    st = state_of(request)
    if body.sched_reason is not None and body.sched_state is None:
        raise HTTPException(status_code=422, detail="sched_reason requires sched_state")
    try:
        await st.nodes.update(
            node_id,
            labels=body.labels,
            capacity=body.capacity,
            sched_state=body.sched_state,
            sched_reason=body.sched_reason,
            actor=principal.audit_actor(),
        )
    except NodeNotFound:
        raise HTTPException(status_code=404, detail="unknown node") from None
    except NodeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return st.nodes.view(node_id)


@admin_router.post("/nodes/{node_id}/token")
async def a_rotate_token(
    node_id: str, request: Request, principal: Principal = ROOT
) -> dict[str, Any]:
    st = state_of(request)
    try:
        token = await st.nodes.rotate_token(node_id, actor=principal.audit_actor())
    except NodeNotFound:
        raise HTTPException(status_code=404, detail="unknown node") from None
    return {"id": node_id, "token": token}


@admin_router.delete("/nodes/{node_id}/token", status_code=204)
async def a_revoke_token(node_id: str, request: Request, principal: Principal = ROOT) -> None:
    st = state_of(request)
    try:
        await st.nodes.revoke_token(node_id, actor=principal.audit_actor())
    except NodeNotFound:
        raise HTTPException(status_code=404, detail="unknown node") from None
    return None


@admin_router.delete("/nodes/{node_id}", status_code=204)
async def a_delete_node(node_id: str, request: Request, principal: Principal = ROOT) -> None:
    st = state_of(request)
    try:
        await st.nodes.remove(node_id, actor=principal.audit_actor())
    except NodeNotFound:
        raise HTTPException(status_code=404, detail="unknown node") from None
    await st.metrics.forget_db(node_id)
    await st.alerts.resolve_node(node_id)
    return None


@admin_router.get("/service-tokens")
async def a_service_tokens(request: Request, _: Principal = ROOT) -> list[dict[str, Any]]:
    return await state_of(request).service_tokens.list()


@admin_router.post("/service-tokens", status_code=201)
async def a_create_service_token(
    body: AdminServiceToken, request: Request, principal: Principal = ROOT
) -> dict[str, Any]:
    st = state_of(request)
    try:
        row, token = await st.service_tokens.create(
            body.principal, body.scopes, actor=principal.audit_actor()
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    row["token"] = token
    return row


@admin_router.delete("/service-tokens/{token_id}", status_code=204)
async def a_revoke_service_token(
    token_id: int, request: Request, principal: Principal = ROOT
) -> None:
    ok = await state_of(request).service_tokens.revoke(token_id, actor=principal.audit_actor())
    if not ok:
        raise HTTPException(status_code=404, detail="unknown or already revoked token")
    return None


@admin_router.post("/lockdown")
async def a_lockdown(
    body: AdminLockdown, request: Request, principal: Principal = ROOT
) -> dict[str, Any]:
    st = state_of(request)
    changed = await st.lockdown.set(body.active, actor=principal.audit_actor(), reason=body.reason)
    view = st.lockdown.view()
    view["changed"] = changed
    return view


@admin_router.get("/audit/verify")
async def a_audit_verify(request: Request, _: Principal = ROOT) -> dict[str, Any]:
    return (await state_of(request).audit.verify()).__dict__


@admin_router.get("/audit/tail")
async def a_audit_tail(
    request: Request,
    limit: int = Query(default=50, ge=1, le=1000),
    before: int | None = Query(default=None, ge=1, le=1 << 62),
    _: Principal = ROOT,
) -> list[dict[str, Any]]:
    return await state_of(request).audit.tail(limit=limit, before_id=before)


__all__ = ["admin_router", "internal_router"]
