from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from storypanel.api import schemas as api
from storypanel.api.deps import (
    AuthUser,
    claim_idempotency,
    current_user,
    generation_limit,
    idempotency_key,
    store_idempotent_response,
)  # fmt: skip
from storypanel.db.session import transaction
from storypanel.obs import metrics
from storypanel.services import editor, events, projects, repo

router = APIRouter(prefix="/v1", tags=["projects"])
ERR: dict[int | str, dict[str, Any]] = {400: {"model": api.ErrorResponse}, 401: {"model": api.ErrorResponse},
       402: {"model": api.ErrorResponse}, 404: {"model": api.ErrorResponse},
       409: {"model": api.ErrorResponse}, 422: {"model": api.ErrorResponse},
       429: {"model": api.ErrorResponse}}  # fmt: skip


async def _after_commit(request: Request, db: object) -> None:
    await events.notify_after_commit(db)  # type: ignore[arg-type]
    request.app.state.outbox_wake.set()


@router.post("/projects", response_model=api.ProjectOut, status_code=201, responses=ERR)
async def create_project(body: api.CreateProject, request: Request,
                         user: AuthUser = Depends(generation_limit),
                         key: str = Depends(idempotency_key)) -> api.ProjectOut:  # fmt: skip
    """Create a comic from a prompt. Reserves planning credits and starts the workflow."""
    async with transaction() as db:
        seen = await claim_idempotency(db, user.id, "create_project", key, body.model_dump_json())
        if seen:
            project = await repo.get_project(db, uuid.UUID(seen["project_id"]), owner_id=user.id)
            return await projects.serialize(db, project)
        project = await projects.create_project(db, user_id=user.id, plan=user.plan, body=body, key=key)
        await store_idempotent_response(db, user.id, "create_project", key,
                                        {"project_id": str(project.id)})  # fmt: skip
        await repo.audit(db, user_ref=user.ref, action="project.create", target_type="project",
                         target_id=project.id, request_id=request.state.request_id)  # fmt: skip
        out = await projects.serialize(db, project)
    await _after_commit(request, db)
    return out


@router.get("/projects", response_model=api.ProjectList, responses=ERR)
async def list_projects(user: AuthUser = Depends(current_user)) -> api.ProjectList:
    async with transaction() as db:
        return api.ProjectList(projects=await projects.list_projects(db, user.id))


@router.get("/projects/{project_id}", response_model=api.ProjectOut, responses=ERR)
async def get_project(project_id: uuid.UUID, user: AuthUser = Depends(current_user)) -> api.ProjectOut:
    """Full project state: bible, cast, script, pages, exports and measured progress."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id)
        return await projects.serialize(db, project)


@router.delete("/projects/{project_id}", status_code=204, responses=ERR)
async def delete_project(project_id: uuid.UUID, request: Request,
                         user: AuthUser = Depends(current_user)) -> None:  # fmt: skip
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.soft_delete(db, project)
        await repo.audit(db, user_ref=user.ref, action="project.delete", target_type="project",
                         target_id=project_id, request_id=request.state.request_id)  # fmt: skip
    await _after_commit(request, db)


@router.get("/estimate", response_model=api.EstimateOut, responses=ERR)
async def estimate_new(pages: int | None = Query(default=None, ge=1, le=8),
                       format: str | None = Query(default=None, pattern="^(western_page|strip)$"),
                       user: AuthUser = Depends(current_user)) -> api.EstimateOut:  # fmt: skip
    async with transaction() as db:
        return await projects.estimate(db, user.id, None, pages, format)


@router.get("/projects/{project_id}/estimate", response_model=api.EstimateOut, responses=ERR)
async def estimate_project(project_id: uuid.UUID, user: AuthUser = Depends(current_user)) -> api.EstimateOut:
    """Credits the next paid step will hold. Shown before the user approves spending."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id)
        return await projects.estimate(db, user.id, project)


@router.post("/projects/{project_id}/brief/answers", response_model=api.Accepted, status_code=202,
             responses=ERR)  # fmt: skip
async def answer_questions(project_id: uuid.UUID, body: api.Answers, request: Request,
                           user: AuthUser = Depends(current_user),
                           key: str = Depends(idempotency_key)) -> api.Accepted:  # fmt: skip
    """Answer (or skip, with an empty object) the clarifying questions."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.submit_answers(db, project, {str(k): v for k, v in body.answers.items()}, key)
    await _after_commit(request, db)
    return api.Accepted(request_id=key)


@router.patch("/projects/{project_id}/bible", response_model=api.ProjectOut, responses=ERR)
async def patch_bible(project_id: uuid.UUID, body: api.PatchBible, request: Request,
                      user: AuthUser = Depends(current_user)) -> api.ProjectOut:  # fmt: skip
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.patch_bible(db, project, body)
        await db.flush()
        out = await projects.serialize(db, project)
    await _after_commit(request, db)
    return out


@router.put("/projects/{project_id}/continuity-rules", response_model=api.ProjectOut, responses=ERR)
async def put_continuity_rules(project_id: uuid.UUID, body: api.PutContinuityRules, request: Request,
                               user: AuthUser = Depends(current_user)) -> api.ProjectOut:  # fmt: skip
    """Replace the comic's continuity rules (classify world rules, add, edit or remove rules)."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.put_continuity_rules(db, project, body)
        await db.flush()
        out = await projects.serialize(db, project)
    await _after_commit(request, db)
    return out


@router.post("/projects/{project_id}/characters/{slug}/revise", response_model=api.Accepted,
             status_code=202, responses=ERR)  # fmt: skip
async def revise_character(project_id: uuid.UUID, slug: str, body: api.ReviseCharacter,
                           request: Request, user: AuthUser = Depends(generation_limit),
                           key: str = Depends(idempotency_key)) -> api.Accepted:  # fmt: skip
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        request_id = await projects.revise_character(db, project, slug, body.instruction, key)
    await _after_commit(request, db)
    return api.Accepted(request_id=request_id)


@router.post("/projects/{project_id}/characters/approve", response_model=api.Accepted, responses=ERR)
async def approve_cast(project_id: uuid.UUID, body: api.ApproveCast, request: Request,
                       user: AuthUser = Depends(current_user)) -> api.Accepted:  # fmt: skip
    """Approve one exact cast version. A stale version returns 409 version_conflict."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.approve_cast(db, project, body.cast_version, user.id, body.acknowledged)
        await repo.audit(db, user_ref=user.ref, action="cast.approve", target_type="project",
                         target_id=project_id, request_id=request.state.request_id,
                         version=body.cast_version)  # fmt: skip
    await _after_commit(request, db)
    return api.Accepted(request_id=f"cast:{body.cast_version}")


@router.patch("/projects/{project_id}/script", response_model=api.ScriptOut, responses=ERR)
async def patch_script(project_id: uuid.UUID, body: api.PatchScript, request: Request,
                       user: AuthUser = Depends(current_user)) -> api.ScriptOut:  # fmt: skip
    """Save an edited script as a new version. Invalidates any earlier approval."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        row = await projects.patch_script(db, project, body)
        out = projects.script_out_of(row)
    await _after_commit(request, db)
    return out


@router.post("/projects/{project_id}/script/approve", response_model=api.Accepted, responses=ERR)
async def approve_script(project_id: uuid.UUID, body: api.ApproveScript, request: Request,
                         user: AuthUser = Depends(generation_limit)) -> api.Accepted:  # fmt: skip
    """Approve one exact script version, hold illustration credits and start drawing."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.approve_script(db, project, body.script_version, user.id, body.acknowledged)
        await repo.audit(db, user_ref=user.ref, action="script.approve", target_type="project",
                         target_id=project_id, request_id=request.state.request_id,
                         version=body.script_version)  # fmt: skip
    await _after_commit(request, db)
    return api.Accepted(request_id=f"script:{body.script_version}")


@router.post("/projects/{project_id}/cancel", response_model=api.Accepted, status_code=202, responses=ERR)
async def cancel_project(project_id: uuid.UUID, request: Request,
                         user: AuthUser = Depends(current_user)) -> api.Accepted:  # fmt: skip
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.cancel(db, project)
    await _after_commit(request, db)
    return api.Accepted(request_id="cancel")


@router.post("/projects/{project_id}/resume", response_model=api.Accepted, status_code=202, responses=ERR)
async def resume_project(project_id: uuid.UUID, request: Request,
                         user: AuthUser = Depends(generation_limit),
                         key: str = Depends(idempotency_key)) -> api.Accepted:  # fmt: skip
    """Recover a failed, expired or partially completed comic from its last durable stage."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        await projects.resume(db, project, key)
    await _after_commit(request, db)
    return api.Accepted(request_id=key)


@router.post("/projects/{project_id}/exports", response_model=api.ExportOut, status_code=202,
             responses=ERR)  # fmt: skip
async def create_export(project_id: uuid.UUID, body: api.CreateExport, request: Request,
                        user: AuthUser = Depends(current_user),
                        key: str = Depends(idempotency_key)) -> api.ExportOut:  # fmt: skip
    """Export an immutable snapshot of the comic as PDF, PNG or CBZ."""
    async with transaction() as db:
        project = await repo.get_project(db, project_id, owner_id=user.id, lock=True)
        export = await editor.create_export(db, project, body.format, key)
        out = api.ExportOut(id=str(export.id), format=export.format, status=export.status,
                            created_at=export.created_at, download_url=None, error=None,
                            edit_version=export.revision.get("edit_version", 0))  # fmt: skip
    await _after_commit(request, db)
    return out


@router.post("/exports/{export_id}/retry", response_model=api.Accepted, status_code=202, responses=ERR)
async def retry_export(export_id: uuid.UUID, request: Request,
                       user: AuthUser = Depends(current_user)) -> api.Accepted:  # fmt: skip
    async with transaction() as db:
        await editor.retry_export(db, export_id, user.id)
    await _after_commit(request, db)
    return api.Accepted(request_id=str(export_id))


def _sse(event: str, data: dict[str, object], seq: int | None = None) -> str:
    head = f"id: {seq}\n" if seq is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@router.get("/projects/{project_id}/events", responses={200: {"content": {"text/event-stream": {}}}, **ERR})
async def project_events(project_id: uuid.UUID, request: Request, after: int | None = Query(default=None, ge=0),
                         user: AuthUser = Depends(current_user)) -> StreamingResponse:  # fmt: skip
    """Server-sent progress events. Resumes from `Last-Event-ID` (or `?after=`); events are
    persisted, so anything missed while disconnected is replayed in order. If the requested
    position is older than retained history a `reset` event tells the client to refetch."""
    async with transaction() as db:
        await repo.get_project(db, project_id, owner_id=user.id)  # ownership check
    header = request.headers.get("last-event-id", "")
    cursor = after if after is not None else int(header) if header.isdigit() else 0

    async def stream() -> AsyncIterator[str]:
        nonlocal cursor
        metrics.sse_connections.inc()
        pubsub = events.redis_client().pubsub()
        try:
            await pubsub.subscribe(events.channel(project_id))
        except Exception:
            pubsub = None  # type: ignore[assignment]
        try:
            yield "retry: 2000\n\n"
            async with transaction() as db:
                oldest = await events.oldest_retained_seq(db, project_id)
            if cursor and oldest is not None and cursor < oldest - 1:
                yield _sse("reset", {"reason": "history_expired"})
            last_beat = time.monotonic()
            while not await request.is_disconnected():
                async with transaction() as db:
                    batch = await events.replay(db, project_id, cursor)
                for e in batch:
                    cursor = e.seq
                    yield _sse(e.type, {**e.payload, "seq": e.seq, "at": e.created_at.isoformat()}, e.seq)
                if len(batch) >= 500:
                    continue
                if time.monotonic() - last_beat > 15:
                    last_beat = time.monotonic()
                    yield ": keep-alive\n\n"
                if pubsub is not None:
                    try:  # wake on publish; the timeout makes polling the safety net
                        await pubsub.get_message(ignore_subscribe_messages=True, timeout=2.0)
                    except Exception:
                        await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(2.0)
        finally:
            metrics.sse_connections.dec()
            if pubsub is not None:
                try:
                    await pubsub.aclose()  # type: ignore[attr-defined,no-untyped-call]
                except Exception:
                    pass

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})  # fmt: skip
