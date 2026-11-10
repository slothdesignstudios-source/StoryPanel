from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request

from storypanel.api import schemas as api
from storypanel.api.deps import AuthUser, current_user, generation_limit, idempotency_key
from storypanel.api.routes.projects import ERR, _after_commit
from storypanel.db.session import transaction
from storypanel.services import editor

router = APIRouter(prefix="/v1", tags=["editor"])


@router.post("/panels/{panel_id}/regenerate", response_model=api.Accepted, status_code=202, responses=ERR)
async def regenerate_panel(panel_id: uuid.UUID, request: Request,
                           user: AuthUser = Depends(generation_limit),
                           key: str = Depends(idempotency_key)) -> api.Accepted:  # fmt: skip
    """New version from the same spec with a new seed. Costs credits only if it delivers."""
    async with transaction() as db:
        rid = await editor.request_panel_work(db, panel_id=panel_id, owner_id=user.id,
                                              kind="regenerate", instruction=None, key=key)  # fmt: skip
    await _after_commit(request, db)
    return api.Accepted(request_id=rid)


@router.post("/panels/{panel_id}/edit", response_model=api.Accepted, status_code=202, responses=ERR)
async def edit_panel(panel_id: uuid.UUID, body: api.EditPanel, request: Request,
                     user: AuthUser = Depends(generation_limit),
                     key: str = Depends(idempotency_key)) -> api.Accepted:  # fmt: skip
    """Instruction-based edit ("make her smile") of the current image."""
    async with transaction() as db:
        rid = await editor.request_panel_work(db, panel_id=panel_id, owner_id=user.id, kind="edit",
                                              instruction=body.instruction, key=key)  # fmt: skip
    await _after_commit(request, db)
    return api.Accepted(request_id=rid)


@router.get("/panels/{panel_id}/versions", response_model=api.VersionList, responses=ERR)
async def panel_versions(panel_id: uuid.UUID, user: AuthUser = Depends(current_user)) -> api.VersionList:
    async with transaction() as db:
        return api.VersionList(versions=await editor.list_versions(db, panel_id, user.id))


@router.post("/panels/{panel_id}/versions/{version_id}/restore", response_model=api.Accepted, responses=ERR)
async def restore_version(panel_id: uuid.UUID, version_id: uuid.UUID, request: Request,
                          user: AuthUser = Depends(current_user)) -> api.Accepted:  # fmt: skip
    async with transaction() as db:
        await editor.restore_version(db, panel_id, version_id, user.id)
    await _after_commit(request, db)
    return api.Accepted(request_id=str(version_id), status="restored")


@router.post("/panels/{panel_id}/feedback", status_code=204, responses=ERR)
async def panel_feedback(panel_id: uuid.UUID, body: api.Feedback,
                         user: AuthUser = Depends(current_user)) -> None:  # fmt: skip
    async with transaction() as db:
        await editor.feedback(db, panel_id, user.id, body.rating)


@router.post("/panels/{panel_id}/balloons", response_model=api.BalloonOut, status_code=201, responses=ERR)
async def create_balloon(panel_id: uuid.UUID, body: api.CreateBalloon, request: Request,
                         user: AuthUser = Depends(current_user)) -> api.BalloonOut:  # fmt: skip
    async with transaction() as db:
        out = await editor.create_balloon(db, panel_id, user.id, body)
    await _after_commit(request, db)
    return out


@router.patch("/balloons/{balloon_id}", response_model=api.BalloonOut, responses=ERR)
async def patch_balloon(balloon_id: uuid.UUID, body: api.PatchBalloon, request: Request,
                        user: AuthUser = Depends(current_user)) -> api.BalloonOut:  # fmt: skip
    """Move, resize or retype a balloon. `version` must match (409 version_conflict if not)."""
    async with transaction() as db:
        out = await editor.patch_balloon(db, balloon_id, user.id, body)
    await _after_commit(request, db)
    return out


@router.delete("/balloons/{balloon_id}", status_code=204, responses=ERR)
async def delete_balloon(balloon_id: uuid.UUID, request: Request,
                         user: AuthUser = Depends(current_user)) -> None:  # fmt: skip
    async with transaction() as db:
        await editor.delete_balloon(db, balloon_id, user.id)
    await _after_commit(request, db)


@router.patch("/pages/{page_id}/layout", response_model=api.Accepted, responses=ERR)
async def change_layout(page_id: uuid.UUID, body: api.PatchLayout, request: Request,
                        user: AuthUser = Depends(current_user)) -> api.Accepted:  # fmt: skip
    async with transaction() as db:
        await editor.change_layout(db, page_id, user.id, body.layout_template_id)
    await _after_commit(request, db)
    return api.Accepted(request_id=str(page_id), status="updated")


@router.post("/pages/{page_id}/reorder", response_model=api.Accepted, responses=ERR)
async def reorder_panels(page_id: uuid.UUID, body: api.ReorderPanels, request: Request,
                         user: AuthUser = Depends(current_user)) -> api.Accepted:  # fmt: skip
    async with transaction() as db:
        await editor.reorder_panels(db, page_id, user.id, body.panel_ids)
    await _after_commit(request, db)
    return api.Accepted(request_id=str(page_id), status="updated")
