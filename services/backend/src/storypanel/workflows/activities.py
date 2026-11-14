"""Temporal activities: thin, observable wrappers around pipeline stages (all I/O lives here)."""

from __future__ import annotations

import asyncio
import functools
import random
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from storypanel.db.session import transaction
from storypanel.errors import AppError, ErrorCode
from storypanel.obs import metrics
from storypanel.obs.logging import bind_context, clear_context, get_logger
from storypanel.obs.tracing import report_exception
from storypanel.pipeline import stages_art as art
from storypanel.pipeline import stages_plan as plan
from storypanel.services import credits

log = get_logger("activity")
Body = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]


def _backoff(attempt: int, throttled: bool) -> timedelta:
    """Exponential backoff with jitter, so retries from many workflows do not align."""
    base = min(90.0, (6.0 if throttled else 2.0) * 2 ** (attempt - 1))
    return timedelta(seconds=base * random.uniform(0.5, 1.0))


def stage_activity(name: str) -> Callable[[Body], Body]:
    def decorate(fn: Body) -> Body:
        @activity.defn(name=name)
        @functools.wraps(fn)
        async def run(arg: dict[str, Any]) -> dict[str, Any] | None:
            info = activity.info()
            started = time.monotonic()
            ids = {k: arg[k] for k in ("project_id", "panel_id", "page_id", "export_id", "version_id")
                   if arg.get(k)}  # fmt: skip
            bind_context(workflow_id=info.workflow_id, workflow_run_id=info.workflow_run_id,
                         stage=name, attempt=info.attempt, **ids)  # fmt: skip
            queued = (info.started_time - info.current_attempt_scheduled_time).total_seconds()
            metrics.schedule_to_start.labels(name).observe(max(0.0, queued))
            if info.attempt > 1:
                metrics.activity_retries.labels(name).inc()

            async def beat() -> None:
                while True:
                    activity.heartbeat()
                    await asyncio.sleep(15)

            heartbeat = asyncio.create_task(beat())
            outcome = "ok"
            try:
                return await fn(arg)
            except AppError as exc:
                outcome = exc.code.value
                metrics.errors.labels(exc.code.value, "worker").inc()
                log.warning("activity_failed", error_code=exc.code.value, retryable=exc.retryable,
                            retry_outcome="will_retry" if exc.retryable else "permanent",
                            **exc.meta)  # fmt: skip
                raise ApplicationError(
                    exc.message, type=exc.code.value, non_retryable=not exc.retryable,
                    next_retry_delay=_backoff(
                        info.attempt, exc.code is ErrorCode.PROVIDER_THROTTLED),
                ) from exc  # fmt: skip
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            except Exception as exc:
                if "account deleted" in str(
                    getattr(exc, "orig", "")
                ):  # owner deleted mid-run: stop, don't retry
                    outcome = "account_deleted"
                    raise ApplicationError("This account has been deleted.", type="account_deleted",
                                           non_retryable=True) from exc  # fmt: skip
                outcome = "internal_error"
                metrics.errors.labels("internal_error", "worker").inc()
                log.error("activity_crashed", error_code="internal_error", exc_info=True)
                report_exception(exc, stage=name, workflow_id=info.workflow_id, attempt=info.attempt)
                # Unknown failures are retried (bounded by the retry policy); the user only
                # ever sees the generic message.
                raise ApplicationError(
                    "Something went wrong on our side. You can try again.",
                    type="internal_error", next_retry_delay=_backoff(info.attempt, False),
                ) from exc  # fmt: skip
            finally:
                heartbeat.cancel()
                metrics.activity_runs.labels(name, outcome).inc()
                metrics.activity_latency.labels(name).observe(time.monotonic() - started)
                log.info("activity_finished", outcome=outcome,
                         duration_ms=int((time.monotonic() - started) * 1000))  # fmt: skip
                clear_context()

        return run

    return decorate


def _id(arg: dict[str, Any], key: str) -> uuid.UUID:
    return uuid.UUID(arg[key])


@stage_activity("intake")
async def intake(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_intake(_id(arg, "project_id"), bool(arg.get("apply_answers")))


@stage_activity("bible")
async def bible(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_bible(_id(arg, "project_id"))


@stage_activity("character_sheet")
async def character_sheet(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_character_sheet(_id(arg, "project_id"), arg["slug"], arg["revision"])


@stage_activity("location_ref")
async def location_ref(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_location_ref(_id(arg, "project_id"), arg["slug"])


@stage_activity("cast_ready")
async def cast_ready(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_cast_ready(_id(arg, "project_id"))


@stage_activity("check_approval")
async def check_approval(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_check_approval(_id(arg, "project_id"), arg["kind"], arg["version"])


@stage_activity("begin_scripting")
async def begin_scripting(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_begin_scripting(_id(arg, "project_id"), arg.get("version"))


@stage_activity("script")
async def script(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_script(_id(arg, "project_id"), defer_gate=bool(arg.get("defer_gate")))


@stage_activity("precheck_script")
async def precheck_script(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_precheck_script(_id(arg, "project_id"), arg["version"])


@stage_activity("check_script")
async def check_script(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_check_script(_id(arg, "project_id"), int(arg["version"]))


@stage_activity("open_script_gate")
async def open_script_gate(arg: dict[str, Any]) -> None:
    await plan.stage_open_script_gate(_id(arg, "project_id"))


@stage_activity("script_unchecked")
async def script_unchecked(arg: dict[str, Any]) -> None:
    await plan.stage_script_unchecked(_id(arg, "project_id"), int(arg["version"]))


@stage_activity("check_sheet")
async def check_sheet(arg: dict[str, Any]) -> dict[str, Any]:
    return await plan.stage_check_sheet(_id(arg, "project_id"), arg["slug"])


@stage_activity("sheet_unchecked")
async def sheet_unchecked(arg: dict[str, Any]) -> None:
    await plan.stage_sheet_unchecked(_id(arg, "project_id"), arg["slug"])


@stage_activity("plan_pages")
async def plan_pages(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_plan_pages(_id(arg, "project_id"), arg["script_version"])


@stage_activity("generate_panel")
async def generate_panel(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_generate_panel(
        _id(arg, "panel_id"), request_key=arg["request_key"], attempt=arg["attempt"],
        kind=arg["kind"], fix_notes=arg.get("fix_notes"), instruction=arg.get("instruction"),
        workflow_id=arg.get("workflow_id", ""))  # fmt: skip


@stage_activity("qa_panel")
async def qa_panel(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_qa_panel(_id(arg, "version_id"), arg["attempt"])


@stage_activity("finalize_panel")
async def finalize_panel(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_finalize_panel(
        _id(arg, "panel_id"), version_ids=arg["version_ids"], kind=arg["kind"],
        request_key=arg["request_key"], intent_seq=arg["intent_seq"],
        reservation_id=arg.get("reservation_id"), workflow_id=arg.get("workflow_id", ""))  # fmt: skip


@stage_activity("panel_failed")
async def panel_failed(arg: dict[str, Any]) -> None:
    await art.stage_panel_failed(_id(arg, "panel_id"), kind=arg["kind"], code=arg["code"],
                                 message=arg["message"], workflow_id=arg.get("workflow_id", ""))  # fmt: skip


@stage_activity("letter_page")
async def letter_page(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_letter_page(_id(arg, "page_id"))


@stage_activity("finish")
async def finish(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_finish(_id(arg, "project_id"))


@stage_activity("end_project")
async def end_project(arg: dict[str, Any]) -> None:
    await art.stage_end(_id(arg, "project_id"), status=arg["status"], code=arg["code"],
                        message=arg["message"], resume_from=arg.get("resume_from"))  # fmt: skip


@stage_activity("close_reservation")
async def close_reservation(arg: dict[str, Any]) -> None:
    async with transaction() as db:
        await credits.close(db, _id(arg, "reservation_id"))


@stage_activity("export_begin")
async def export_begin(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_export_begin(_id(arg, "export_id"))


@stage_activity("export_page")
async def export_page(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_export_page(_id(arg, "export_id"), arg["page_index"])


@stage_activity("export_assemble")
async def export_assemble(arg: dict[str, Any]) -> dict[str, Any]:
    return await art.stage_export_assemble(_id(arg, "export_id"))


@stage_activity("export_failed")
async def export_failed(arg: dict[str, Any]) -> None:
    await art.stage_export_failed(_id(arg, "export_id"), arg["code"], arg["message"])


ACTIVITIES = [
    intake, bible, character_sheet, location_ref, cast_ready, check_approval, begin_scripting,
    script, precheck_script, plan_pages, generate_panel, qa_panel, finalize_panel, panel_failed,
    letter_page, finish, end_project, close_reservation, export_begin, export_page,
    export_assemble, export_failed, check_script, script_unchecked, check_sheet, sheet_unchecked,
    open_script_gate,
]  # fmt: skip
