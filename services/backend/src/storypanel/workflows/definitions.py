"""Temporal workflows. Deterministic orchestration only: every side effect is an activity.

Activities are referenced by name so this module imports nothing with I/O, which keeps it
valid inside the workflow sandbox. Limits arrive in the workflow input so replays never
depend on live configuration.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

# Mirrors errors.NON_RETRYABLE_CODES (duplicated as literals to keep this module import-free).
NON_RETRYABLE = [
    "validation_error", "unauthenticated", "forbidden", "not_found", "conflict",
    "version_conflict", "invalid_state", "quota_exceeded", "insufficient_credits",
    "spend_limit_reached", "moderation_blocked", "provider_outcome_unknown_final", "pricing_unavailable",
    "payload_too_large",
]  # fmt: skip

TEXT = {
    "start_to_close_timeout": timedelta(minutes=5),
    "schedule_to_close_timeout": timedelta(minutes=25),
    "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=2), backoff_coefficient=2.0,
                                maximum_interval=timedelta(seconds=60), maximum_attempts=4,
                                non_retryable_error_types=NON_RETRYABLE),
}  # fmt: skip
IMAGE = {
    "start_to_close_timeout": timedelta(minutes=6),
    "schedule_to_close_timeout": timedelta(minutes=40),
    "heartbeat_timeout": timedelta(seconds=60),
    "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=3), backoff_coefficient=2.0,
                                maximum_interval=timedelta(seconds=90), maximum_attempts=5,
                                non_retryable_error_types=NON_RETRYABLE),
}  # fmt: skip
DB = {
    "start_to_close_timeout": timedelta(seconds=45),
    "schedule_to_close_timeout": timedelta(minutes=10),
    "retry_policy": RetryPolicy(initial_interval=timedelta(seconds=1), backoff_coefficient=2.0,
                                maximum_interval=timedelta(seconds=20), maximum_attempts=8,
                                non_retryable_error_types=NON_RETRYABLE),
}  # fmt: skip


def classify(exc: BaseException) -> tuple[str, str]:
    """Stable error code and user-safe message from an activity failure."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ApplicationError) and cause.type:
            return cause.type, cause.message
        cause = cause.__cause__
    return "internal_error", "Something went wrong on our side. You can try again."


async def act(name: str, arg: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    result = await workflow.execute_activity(name, arg, **opts)
    return result or {}


async def run_panel(*, panel_id: str, kind: str, request_key: str, intent_seq: int,
                    max_qa_retries: int, instruction: str | None = None,
                    reservation_id: str | None = None) -> dict[str, Any]:  # fmt: skip
    """Generate -> QA -> bounded repair -> finalize, for one panel.

    At most `max_qa_retries` automatic regenerations follow the first attempt. A panel that
    still fails quality checks is finalized as "needs attention"; a panel whose every
    attempt was safety-blocked is finalized as blocked and stays hidden.
    """
    wf_id = workflow.info().workflow_id
    versions: list[str] = []
    notes: list[str] = []
    final = {"panel_id": panel_id, "version_ids": versions, "kind": kind,
             "request_key": request_key, "intent_seq": intent_seq,
             "reservation_id": reservation_id, "workflow_id": wf_id}  # fmt: skip
    try:
        for attempt in range(max_qa_retries + 1):
            made = await act("generate_panel", {
                "panel_id": panel_id, "request_key": request_key, "attempt": attempt,
                "kind": kind, "fix_notes": notes, "instruction": instruction,
                "workflow_id": wf_id}, IMAGE)  # fmt: skip
            versions.append(made["version_id"])
            verdict = await act("qa_panel", {"version_id": made["version_id"],
                                             "attempt": attempt}, IMAGE)  # fmt: skip
            if verdict["status"] in ("pass", "review"):
                break  # review: an uncertain identity judgment is not fixed by repainting
            if verdict.get("stop") and workflow.patched("qa-stop-repeat-2026-11"):
                break  # the same problems survived a repair aimed at them: the user decides
            notes = verdict.get("fix_notes", [])
        return await act("finalize_panel", final, DB)
    except ActivityError as exc:
        code, message = classify(exc)
        if versions:
            # Earlier attempts exist: finalize from them rather than discarding paid work.
            return await act("finalize_panel", final, DB)
        await act("panel_failed", {"panel_id": panel_id, "kind": kind, "code": code,
                                   "message": message, "workflow_id": wf_id}, DB)  # fmt: skip
        return {"status": "failed", "applied": False, "code": code}


@workflow.defn(name="ComicWorkflow")
class ComicWorkflow:
    """Prompt to finished pages, pausing durably at the cast and script approval gates."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._answers = False
        self._approvals: dict[str, list[int]] = {"cast": [], "script": []}
        self._revisions: list[str] = []
        self._checks: list[int] = []  # script versions saved by the user, to check against the Bible
        self._stage = "queued"

    def _fresh(self, signal_id: str) -> bool:
        """Signals are delivered at least once; each id is acted on once."""
        if signal_id in self._seen:
            return False
        self._seen.add(signal_id)
        return True

    @workflow.signal
    def answers_submitted(self, arg: dict[str, Any]) -> None:
        if self._fresh(arg["id"]):
            self._answers = True

    @workflow.signal
    def approve(self, arg: dict[str, Any]) -> None:
        if self._fresh(arg["id"]):
            self._approvals[arg["kind"]].append(int(arg["version"]))

    @workflow.signal
    def revise_character(self, arg: dict[str, Any]) -> None:
        if self._fresh(arg["id"]):
            self._revisions.append(arg["slug"])

    @workflow.signal
    def check_script(self, arg: dict[str, Any]) -> None:
        if self._fresh(arg["id"]):
            self._checks.append(int(arg["version"]))

    async def _check_script(self, pid: str, version: int) -> None:
        """Metered Story Bible check of one script version; bounded retries, then "not checked"."""
        try:
            await act("check_script", {"project_id": pid, "version": version}, TEXT)
        except ActivityError as exc:
            if classify(exc)[0] in ("spend_limit_reached", "insufficient_credits"):
                raise
            await act("script_unchecked", {"project_id": pid, "version": version}, DB)

    async def _sheet(self, pid: str, slug: str, revision: bool) -> None:
        await act("character_sheet", {"project_id": pid, "slug": slug, "revision": revision}, IMAGE)
        if workflow.patched("sheet-check-2026-11"):
            try:
                await act("check_sheet", {"project_id": pid, "slug": slug}, TEXT)
            except ActivityError as exc:
                if classify(exc)[0] in ("spend_limit_reached", "insufficient_credits"):
                    raise
                await act("sheet_unchecked", {"project_id": pid, "slug": slug}, DB)

    @workflow.query
    def stage(self) -> str:
        return self._stage

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        pid = inp["project_id"]
        try:
            return await self._pipeline(pid, inp)
        except asyncio.CancelledError:
            # Cancellation is delivered once; cleanup below runs as normal activities.
            await act("end_project", {"project_id": pid, "status": "cancelled",
                                      "code": "cancelled", "message": "You cancelled this comic.",
                                      "resume_from": None}, DB)  # fmt: skip
            raise
        except ActivityError as exc:
            code, message = classify(exc)
            await act("end_project", {
                "project_id": pid, "status": "blocked" if code == "moderation_blocked" else "failed",
                "code": code, "message": message, "resume_from": self._stage}, DB)  # fmt: skip
            raise ApplicationError(message, type=code, non_retryable=True) from exc

    async def _gate(self, pid: str, kind: str, days: int) -> int | None:
        """Wait for an approval of the *current* content version; None when it expires."""
        while True:
            pending = self._approvals[kind]
            try:
                await workflow.wait_condition(
                    lambda pending=pending: bool(pending) or (kind == "cast" and bool(self._revisions))  # type: ignore[misc]
                    or (kind == "script" and bool(self._checks)),
                    timeout=timedelta(days=days))  # fmt: skip
            except TimeoutError:
                return None
            while kind == "cast" and self._revisions:
                slug = self._revisions.pop(0)
                await self._sheet(pid, slug, revision=True)
            while kind == "script" and self._checks:
                await self._check_script(pid, self._checks.pop(0))
            if pending:
                version = pending.pop(0)
                if kind == "script":
                    check = await act("precheck_script", {"project_id": pid, "version": version}, TEXT)
                    if check["outcome"] == "ok":
                        return version
                else:
                    check = await act("check_approval", {"project_id": pid, "kind": kind,
                                                         "version": version}, DB)  # fmt: skip
                    if check["valid"]:
                        return version
                # stale or rejected approval: keep waiting for a valid one

    async def _expire(self, pid: str, gate: str) -> dict[str, Any]:
        await act("end_project", {"project_id": pid, "status": "expired", "code": "approval_expired",
                                  "message": "This comic was waiting for your approval for too long.",
                                  "resume_from": gate}, DB)  # fmt: skip
        return {"status": "expired"}

    async def _pipeline(self, pid: str, inp: dict[str, Any]) -> dict[str, Any]:
        order = ["intake", "planning", "cast_gate", "scripting", "script_gate", "illustrating"]
        start = order.index(inp.get("resume_from") or "intake")
        script_version = int(inp.get("script_version") or 0)

        if start <= 0:
            self._stage = "intake"
            res = await act("intake", {"project_id": pid}, TEXT)
            if res["outcome"] == "blocked":
                return {"status": "blocked"}
            if res["outcome"] == "questions":
                try:
                    await workflow.wait_condition(lambda: self._answers,
                                                  timeout=timedelta(days=inp["answers_ttl_days"]))  # fmt: skip
                except TimeoutError:
                    pass  # every question is skippable: defaults apply after the timeout
                res = await act("intake", {"project_id": pid, "apply_answers": True}, TEXT)
                if res["outcome"] == "blocked":
                    return {"status": "blocked"}

        if start <= 1:
            self._stage = "planning"
            bible = await act("bible", {"project_id": pid}, TEXT)
            if bible["outcome"] == "blocked":
                return {"status": "blocked"}
            # Characters and locations are independent of each other: draw them in parallel.
            await asyncio.gather(
                *[self._sheet(pid, s, revision=False) for s in bible["characters"]],
                *[act("location_ref", {"project_id": pid, "slug": s}, IMAGE)
                  for s in bible["locations"]],
            )  # fmt: skip
            await act("cast_ready", {"project_id": pid}, DB)

        if start <= 2:
            self._stage = "cast_gate"
            while True:
                cast_version = await self._gate(pid, "cast", inp["approval_ttl_days"])
                if cast_version is None:
                    return await self._expire(pid, "cast_gate")
                began = await act("begin_scripting", {"project_id": pid, "version": cast_version}, DB)
                if not began.get("stale"):
                    break  # a save that raced the approval leaves the comic at the gate

        if start <= 3:
            self._stage = "scripting"
            checked = workflow.patched("script-consistency-2026-11")
            res = await act("script", {"project_id": pid, "defer_gate": checked}, TEXT)
            if res["outcome"] == "blocked":
                return {"status": "blocked"}
            if checked:  # the review screen opens once the Story Bible check has an answer
                await self._check_script(pid, int(res["script_version"]))
                await act("open_script_gate", {"project_id": pid}, DB)

        plan: dict[str, Any] | None = None
        if start <= 4:
            while True:
                self._stage = "script_gate"
                approved = await self._gate(pid, "script", inp["approval_ttl_days"])
                if approved is None:
                    return await self._expire(pid, "script_gate")
                script_version = approved
                self._stage = "illustrating"
                plan = await act("plan_pages", {"project_id": pid, "script_version": script_version}, DB)
                if not plan.get("stale"):
                    break  # stale: a newer save landed after the check; wait for a new approval

        self._stage = "illustrating"
        if plan is None:  # resumed while illustrating: no gate
            plan = await act("plan_pages", {"project_id": pid, "script_version": script_version}, DB)
        retries = int(inp["max_qa_retries"])
        run_key = inp.get("run_key") or "initial"  # a resumed run gets fresh attempts

        async def chain(panel_ids: list[str]) -> None:
            for panel_id in panel_ids:  # sequential: each panel references the previous one
                await run_panel(panel_id=panel_id, kind="generate", request_key=run_key,
                                intent_seq=0, max_qa_retries=retries)  # fmt: skip

        async def page(p: dict[str, Any]) -> None:
            await asyncio.gather(*[chain(c) for c in p["chains"]])
            await act("letter_page", {"page_id": p["page_id"]}, DB | {
                "start_to_close_timeout": timedelta(minutes=3)})  # fmt: skip

        await asyncio.gather(*[page(p) for p in plan["pages"]])
        return await act("finish", {"project_id": pid}, DB)


@workflow.defn(name="PanelWorkWorkflow")
class PanelWorkWorkflow:
    """One user-requested regenerate or instruction edit of a single panel."""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await run_panel(
                panel_id=inp["panel_id"], kind=inp["kind"], request_key=inp["request_key"],
                intent_seq=inp["intent_seq"], max_qa_retries=int(inp["max_qa_retries"]),
                instruction=inp.get("instruction"), reservation_id=inp.get("reservation_id"))  # fmt: skip
            if result.get("applied"):
                await act("letter_page", {"page_id": inp["page_id"]}, DB | {
                    "start_to_close_timeout": timedelta(minutes=3)})  # fmt: skip
            return result
        finally:
            if inp.get("reservation_id"):
                # Whatever happened (success, failure, cancellation), release the unused hold.
                await asyncio.shield(act("close_reservation",
                                         {"reservation_id": inp["reservation_id"]}, DB))  # fmt: skip


@workflow.defn(name="ExportWorkflow")
class ExportWorkflow:
    """Renders an immutable project revision. Pages are independent and resumable."""

    @workflow.run
    async def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        export_id = inp["export_id"]
        try:
            begin = await act("export_begin", {"export_id": export_id}, DB)
            await asyncio.gather(*[
                act("export_page", {"export_id": export_id, "page_index": n}, IMAGE)
                for n in range(begin["pages"])])  # fmt: skip
            return await act("export_assemble", {"export_id": export_id}, DB | {
                "start_to_close_timeout": timedelta(minutes=5)})  # fmt: skip
        except ActivityError as exc:
            code, message = classify(exc)
            await act("export_failed", {"export_id": export_id, "code": code,
                                        "message": message}, DB)  # fmt: skip
            raise ApplicationError(message, type=code, non_retryable=True) from exc
        except asyncio.CancelledError:
            await act("export_failed", {"export_id": export_id, "code": "cancelled",
                                        "message": "Export was cancelled."}, DB)  # fmt: skip
            raise


WORKFLOWS = [ComicWorkflow, PanelWorkWorkflow, ExportWorkflow]
