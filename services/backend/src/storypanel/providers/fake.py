"""Deterministic offline providers for tests, local development and the simulated eval.

These never run in staging/production: Settings validation rejects provider_mode="fake"
there, and the registry has no fallback path from live to fake.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from PIL import Image, ImageDraw

from storypanel.domain import schemas as sc
from storypanel.errors import AppError, ErrorCode
from storypanel.providers.base import (
    ASPECTS,
    CallContext,
    ImageInput,
    ImageRequest,
    ImageResult,
    JobFailed,
    RequestPending,
    RequestUnknown,
    T,
    TextRole,
    Usage,
)  # fmt: skip


@dataclass
class FakeControl:
    """Failure injection. Tests mutate `control`; processes can seed it via SP_FAKE_SCENARIO."""

    script_violations: int = 0  # first N scripts break the 25-word rule
    qa_fail_attempts: int = 0  # each panel fails QA on its first N attempts
    qa_fail_targets: set[str] = field(default_factory=set)  # these targets always fail QA
    unsafe_targets: set[str] = field(default_factory=set)  # image safety blocks these
    unsafe_attempts: int = 0  # every image is unsafe on its first N attempts
    stray_text_targets: set[str] = field(default_factory=set)
    unlocalised_attempts: int = 0  # the judge omits every box on the first N attempts
    # after a cancel, the provider reports the job as failed/cancelled (an acknowledgement)
    cancel_reports_job_failed: bool = False
    errors: dict[str, list[Exception]] = field(default_factory=dict)  # op -> queue of raises
    pending_fetches: int = 0  # next N image fetches report "still running"
    pending_upscale_fetches: int = 0  # next N upscale fetches report "still running"
    submits: list[str] = field(default_factory=list)
    models_used: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    fetches: list[tuple[str, str]] = field(default_factory=list)  # (model, request id) per fetch
    cancels: list[tuple[str, str]] = field(default_factory=list)  # (model, request id) per cancel
    hang_fetches: int = 0
    crashes: dict[str, int] = field(default_factory=dict)  # op -> times to raise an unexpected error
    calls: list[tuple[str, str]] = field(default_factory=list)
    malformed_text: int = 0

    def reset(self) -> None:
        self.__init__()  # type: ignore[misc]

    def crash_if_configured(self, op: str) -> None:
        if self.crashes.get(op, 0) > 0:
            self.crashes[op] -= 1
            raise RuntimeError(f"simulated unexpected failure in {op}")

    def raise_if_queued(self, op: str) -> None:
        self.crash_if_configured(op)
        queue = self.errors.get(op)
        if queue:
            raise queue.pop(0)


def _from_env() -> FakeControl:
    c = FakeControl()
    raw = os.environ.get("SP_FAKE_SCENARIO")
    if raw:
        for key, value in json.loads(raw).items():
            setattr(c, key, set(value) if isinstance(value, list) else value)
    return c


control = _from_env()


def _h(*parts: object) -> int:
    return int(hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12], 16)


# ------------------------------------------------------------------ text
class FakeTextProvider:
    name = "fake"
    max_tokens = 16000

    def model_for(self, role: TextRole) -> str:
        return f"fake-{role}"

    async def structured(
        self, *, role: TextRole, system: str, user: str, schema: type[T], ctx: CallContext,
        images: tuple[ImageInput, ...] = (),
    ) -> tuple[T, Usage]:  # fmt: skip
        control.calls.append((ctx.stage, schema.__name__))
        control.raise_if_queued(f"text:{ctx.stage}")
        control.raise_if_queued("text")
        if control.malformed_text > 0:
            control.malformed_text -= 1
            raise AppError(ErrorCode.PROVIDER_BAD_OUTPUT, "Fake malformed output.")
        builder = {
            "IntakeResult": self._intake, "BibleAndCast": self._bible, "ScriptDraft": self._script,
            "ModerationVerdict": self._moderation, "VisionQAReport": self._vision,
            "SafetyVerdict": self._safety, "CharacterSpec": self._revise_spec,
        }[schema.__name__]  # fmt: skip
        out = schema.model_validate(builder(ctx, user))
        usage = Usage(provider=self.name, model=f"fake-{role}", operation=f"text_{role}",
                      input_tokens=len(system + user) // 4, output_tokens=200, latency_ms=1)  # fmt: skip
        return out, usage

    def _intake(self, ctx: CallContext, user: str) -> dict[str, Any]:
        i = ctx.inputs
        prompt: str = i["prompt"]
        answers: dict[str, str] = i.get("answers") or {}
        low = prompt.lower()
        m = re.search(r"(\d+)\s*-?\s*pages?", low)
        strip = "strip" in low
        pages = 1 if strip else int(m.group(1)) if m else i["default_pages"]
        words = {"clean_manga_warm": ("manga",), "bold_western_ink": ("western comic", "bold western"),
                 "soft_storybook": ("storybook",), "noir_duotone": ("noir",)}  # fmt: skip
        style = next((s for s in i["style_ids"] if any(w in low for w in words.get(s, ()))),
                     i["default_style"])  # fmt: skip
        audience = "teen" if "teen" in low else "all_ages"
        questions: list[dict[str, Any]] = []
        if not answers and not m and not strip and len(prompt.split()) < 9:
            questions.append({"id": "length", "question": "How long should the comic be?",
                              "options": ["A 4-panel strip", "2 pages", "4 pages"],
                              "default": "4 pages"})  # fmt: skip
        if "length" in answers:
            a = answers["length"].lower()
            strip = "strip" in a
            mm = re.search(r"(\d+)\s*pages?", a)
            pages = 1 if strip else int(mm.group(1)) if mm else pages
        if "style" in answers and answers["style"] in i["style_ids"]:
            style = answers["style"]
        if "audience" in answers and answers["audience"] in ("all_ages", "teen", "mature"):
            audience = answers["audience"]
        pages = max(1, min(int(pages), i["max_pages"]))
        # like the live model: a strip with a stated panel count ("3 panel strip") keeps it
        pm = re.search(r"\b(\d+)[\s-]*panels?\b", low)
        panels = int(pm.group(1)) if strip and pm else None
        names = re.findall(r"\bnamed ([A-Z][a-z]+)", prompt)[: i["max_characters"]]
        return {
            "brief": {
                "summary": prompt[:200], "genre": ["slice of life", "comedy"], "tone": "warm, funny",
                "audience": audience, "pages": pages,
                "format": "strip" if strip else "western_page", "style_preset": style,
                "named_characters": names, "setting": "a rain-soaked neon city",
                "must_haves": [], "panels": panels,
            },
            "questions": questions,
        }  # fmt: skip

    def _bible(self, ctx: CallContext, user: str) -> dict[str, Any]:
        b = ctx.inputs["brief"]
        names = b["named_characters"] or ["Kiro", "Mei"]
        palettes = [("#E9E4D8", "#D94F30", "#7FD4FF"), ("#FFD23F", "#2B2B2B", "#F4A7B9"),
                    ("#8BC34A", "#3E2723", "#FFFFFF"), ("#9575CD", "#FFB300", "#263238")]  # fmt: skip
        chars = []
        for n, name in enumerate(names[: ctx.inputs["max_characters"]]):
            body, accent, eye = palettes[n % 4]
            chars.append({
                "id": sc.slugify(name), "name": name,
                "role": "protagonist" if n == 0 else "friend",
                "personality": "shy, precise, secretly funny" if n == 0 else "curious and bold",
                "voice": "short literal sentences" if n == 0 else "quick, teasing questions",
                "visual": {
                    "type": "small service robot, about 1.2 m tall" if n == 0 else "girl, about 10",
                    "body": "round body, short stubby legs" if n == 0 else "slim, energetic",
                    "head": "dome head with a single wide visor eye" if n == 0 else "round face",
                    "hair": "none" if n == 0 else "short black bob",
                    "outfit": "red apron with a noodle-bowl patch" if n == 0 else "yellow raincoat",
                    "palette": {"body": body, "accent": accent, "eye": eye},
                    "signature_features": (["single visor eye", "red apron", "antenna with a bell"]
                                           if n == 0 else ["yellow raincoat", "striped umbrella"]),
                },
            })  # fmt: skip
        fmt = b["format"]
        return {
            "bible": {
                "title": "Noodles After Midnight", "logline": b["summary"][:160],
                "audience": b["audience"], "genre": b["genre"],
                "format": {"type": fmt, "pages": b["pages"], "panels": b.get("panels"),
                           "panels_per_page": "3-4" if fmt == "strip" else "4-6"},
                "style_preset": b["style_preset"], "setting": b["setting"], "tone": b["tone"],
                "themes": ["loneliness", "small kindnesses"],
                "world_rules": ["robots are common but rarely befriended"],
                "palette": ["#F4E9D8", "#D94F30", "#2B2B2B"],
                "locations": [
                    {"id": "noodle_stall_night", "name": "Noodle stall",
                     "description": "A street noodle stall at night in light rain, paper lanterns"},
                    {"id": "rooftop_dawn", "name": "Rooftop",
                     "description": "A flat rooftop at dawn above the city, water tanks, pink sky"},
                ],
                "cast": [c["id"] for c in chars],
            },
            "characters": chars,
        }  # fmt: skip

    def _script(self, ctx: CallContext, user: str) -> dict[str, Any]:
        i = ctx.inputs
        cast: list[str] = i["character_ids"]
        locs: list[str] = i["location_ids"]
        strip = i["format"] == "strip"
        pages = 1 if strip else i["pages"]
        violate = control.script_violations > 0 and not i.get("repair")
        if control.script_violations > 0:
            control.script_violations -= 1
            violate = True
        climax_page = max(0, pages - 2) if pages > 1 else 0
        out_pages = []
        for p in range(pages):
            n_panels = (i.get("panels") or 4) if strip else 4 + (p % 2)
            panels = []
            for k in range(n_panels):
                who = cast[: 1 + ((p + k) % min(len(cast), 3))]
                positions = ["left", "right", "center"]
                speaker = who[k % len(who)]
                text = "I calculated you were hungry." if k % 2 == 0 else "You made this for me?"
                if violate and p == 0 and k == 0:
                    text = " ".join(["word"] * 30)
                dialogue = [{"type": "speech", "speaker": speaker, "text": text}]
                if k == 0:
                    dialogue.insert(0, {"type": "caption", "speaker": None,  # type: ignore[dict-item]
                                        "text": f"Page {p + 1}. The rain kept falling."})  # fmt: skip
                if k == n_panels - 1 and len(who) > 1:
                    dialogue.append({"type": "speech", "speaker": who[(k + 1) % len(who)],
                                     "text": "Same time tomorrow?"})  # fmt: skip
                if k == 1 and p == climax_page:
                    dialogue.append({"type": "sfx", "speaker": None, "text": "CLANG!"})  # type: ignore[dict-item]
                panels.append({
                    "shot": ["wide", "medium", "close_up"][k % 3], "angle": "eye-level",
                    "location_id": locs[(p + k // 3) % len(locs)], "time_of_day": "night, light rain",
                    "characters": [{"id": c, "position": positions[n], "pose": "standing",
                                    "expression": "nervous smile"} for n, c in enumerate(who)],
                    "action": f"{who[0]} offers a steaming bowl", "mood": "warm, cosy",
                    "emphasis": "large" if (p == climax_page and k == n_panels - 1
                                            and not strip) else "normal",
                    "dialogue": dialogue,
                })  # fmt: skip
            out_pages.append({"panels": panels})
        beats = (["setup", "development", "twist", "payoff"] if strip
                 else ["hook", "setup", "rising conflict", "climax", "resolution"])  # fmt: skip
        return {
            "beat_sheet": {
                "structure": "kishotenketsu" if strip else "three_act",
                "beats": [{"name": b, "summary": f"The {b}.",
                           "pages": [min(pages, 1 + n * pages // len(beats))]}
                          for n, b in enumerate(beats)],
            },
            "script": {"pages": out_pages},
        }  # fmt: skip

    def _revise_spec(self, ctx: CallContext, user: str) -> dict[str, Any]:
        spec = dict(ctx.inputs["spec"])
        visual = dict(spec["visual"])
        visual["outfit"] = visual["outfit"].split(" (revised")[0] + " (revised)"
        return {**spec, "visual": visual}

    def _moderation(self, ctx: CallContext, user: str) -> dict[str, Any]:
        blocked = "[[unsafe]]" in user
        return {"allowed": not blocked, "categories": ["graphic_violence"] if blocked else [],
                "user_message": "This content isn't allowed." if blocked else "",
                "suggested_rewrite": None}  # fmt: skip

    def _vision(self, ctx: CallContext, user: str) -> dict[str, Any]:
        chars = ctx.inputs.get("characters", [])
        target = ctx.target_id or ""
        fail = ctx.attempt < control.qa_fail_attempts or target in control.qa_fail_targets
        text = target in control.stray_text_targets
        xs = {"left": 0.08, "center": 0.38, "right": 0.68}
        boxes = ctx.attempt >= control.unlocalised_attempts
        observations = [
            {"id": c["id"], "present": True,
             "head_box": {"x": xs[c["position"]] + 0.04, "y": 0.34, "w": 0.16, "h": 0.2} if boxes else None,
             "body_box": {"x": xs[c["position"]], "y": 0.34, "w": 0.24, "h": 0.6} if boxes else None,
             "missing_features": [c["signature_features"][0]] if fail and n == 0 else [],
             "identity_rating": 2 if fail and n == 0 else 5}
            for n, c in enumerate(chars)
        ]  # fmt: skip
        return {
            "character_count": len(chars), "characters": observations, "action_matches": True,
            "visible_text": text, "anatomy_defects": [],
            "action_box": {"x": 0.3, "y": 0.5, "w": 0.4, "h": 0.3},
            "fix_notes": (["Draw the missing signature feature clearly."] if fail else [])
            + (["Remove all lettering from the art."] if text else []),
        }  # fmt: skip

    def _safety(self, ctx: CallContext, user: str) -> dict[str, Any]:
        bad = (ctx.target_id or "") in control.unsafe_targets or ctx.attempt < control.unsafe_attempts
        return {"allowed": not bad, "categories": ["graphic_violence"] if bad else [],
                "severity": "high" if bad else "none"}  # fmt: skip


# ------------------------------------------------------------------ images
def _hex(color: str) -> tuple[int, int, int]:
    c = color.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16)) if len(c) == 6 else (128, 128, 128)


def render_fake_image(req: ImageRequest) -> bytes:
    """A flat, text-free placeholder scene: enough structure for layout, QA and export tests."""
    ratio = ASPECTS.get(req.aspect, 1.0)
    w = 768 if ratio >= 1 else int(768 * ratio)
    h = int(w / ratio)
    seed = _h(req.prompt, req.seed)
    base = ((seed >> 4) % 90 + 110, (seed >> 12) % 90 + 110, (seed >> 20) % 90 + 130)
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img)
    kind = req.inputs.get("kind", "panel")
    if kind not in ("turnaround", "expressions"):  # model sheets are drawn on a plain background
        d.rectangle([0, int(h * 0.72), w, h], fill=tuple(max(0, c - 55) for c in base))
    chars: list[dict[str, Any]] = req.inputs.get("characters", [])
    if kind in ("turnaround", "expressions") and chars:
        chars = [{**chars[0], "slot": n} for n in range(4)]
    xs = {"left": 0.2, "center": 0.5, "right": 0.8}
    for n, c in enumerate(chars):
        pal = list(c.get("palette", {}).values()) or ["#CCCCCC", "#555555"]
        body, accent = _hex(pal[0]), _hex(pal[1 % len(pal)])
        bottom = 0.92
        if "slot" in c and kind == "expressions":  # 2 x 2 grid of busts
            cx = int(w * (0.25 + 0.5 * (c["slot"] % 2)))
            r, top = int(min(w, h) * 0.07), int(h * (0.18 + 0.5 * (c["slot"] // 2)))
            bottom = 0.42 + 0.5 * (c["slot"] // 2)
        elif "slot" in c:
            cx, r, top = int(w * (0.125 + 0.25 * c["slot"])), int(min(w, h) * 0.09), int(h * 0.36)
        else:
            cx, r, top = (
                int(w * xs.get(c.get("position", "center"), 0.5)),
                int(min(w, h) * 0.09),
                int(h * 0.36),
            )
        d.rounded_rectangle([cx - r, top + r, cx + r, int(h * bottom)], radius=r // 2, fill=body,
                            outline=(20, 20, 20), width=3)  # fmt: skip
        d.ellipse([cx - r, top - r, cx + r, top + r], fill=body, outline=(20, 20, 20), width=3)
        d.rectangle([cx - r, top + r + 6, cx + r, top + r + 6 + r // 2], fill=accent)
        d.ellipse([cx - r // 3 + n * 2, top - r // 4, cx + r // 3 + n * 2, top + r // 4], fill=accent)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def render_fake_repair(req: ImageRequest) -> bytes:
    """Repaint the masked region with a flat colour: lets tests prove that pixels outside
    the mask are untouched after compositing."""
    assert req.image is not None and req.mask is not None
    base = Image.open(io.BytesIO(req.image)).convert("RGB")
    mask = Image.open(io.BytesIO(req.mask)).convert("L")
    seed = _h(req.prompt, req.seed)
    patch = Image.new(
        "RGB", base.size, ((seed >> 3) % 200 + 30, (seed >> 9) % 200 + 30, (seed >> 15) % 200 + 30)
    )
    base.paste(patch, (0, 0), mask)
    out = io.BytesIO()
    base.save(out, format="PNG")
    return out.getvalue()


class _DurableQueue:
    """Optional Redis-backed job store (SP_FAKE_DURABLE_QUEUE=1). It outlives worker
    processes the way a real provider queue does, so crash drills can prove that a resumed
    request is fetched rather than resubmitted. It also counts submissions across
    processes (`sp:fake:submissions`), which is the provider-side figure drills assert on."""

    @staticmethod
    def enabled() -> bool:
        return os.environ.get("SP_FAKE_DURABLE_QUEUE") == "1"

    @staticmethod
    async def put(request_id: str, model: str, req: ImageRequest) -> None:
        import pickle

        from storypanel.services.events import redis_client

        r = redis_client()
        # the shared client decodes responses as text, so store the pickle base64-encoded
        await r.set(
            f"sp:fake:job:{request_id}", base64.b64encode(pickle.dumps((model, req))).decode(), ex=86400
        )
        await r.incr("sp:fake:submissions")

    @staticmethod
    async def get(request_id: str) -> tuple[str, ImageRequest] | None:
        import pickle

        from storypanel.services.events import redis_client

        raw = await redis_client().get(f"sp:fake:job:{request_id}")
        return pickle.loads(base64.b64decode(raw)) if raw else None  # noqa: S301 - local test fixture data only


class FakeImageProvider:
    name = "fake"

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[str, ImageRequest]] = {}  # request id -> (endpoint, request)
        self.cancelled: list[str] = []

    def model_for(self, req: ImageRequest) -> str:
        return (
            req.model or {"generate": "fake-image", "edit": "fake-edit", "inpaint": "fake-inpaint"}[req.role]
        )

    def fallback_for(self, req: ImageRequest) -> str | None:
        return "fake-fallback" if req.role in ("generate", "edit") else None

    async def cancel(self, model: str, request_id: str) -> None:
        control.cancels.append((model, request_id))
        control.raise_if_queued("image:cancel")
        self.cancelled.append(request_id)
        control.cancelled.append(request_id)

    async def submit(self, req: ImageRequest, ctx: CallContext, *, model: str) -> str:
        control.models_used.append(model)
        if model != "fake-fallback":
            control.raise_if_queued("image:submit")
        request_id = (
            f"fake-{_h(req.prompt, req.seed, len(control.submits), os.getpid(), os.urandom(4).hex()):x}"
        )
        control.submits.append(ctx.target_id or "")
        self._jobs[request_id] = (model, req)
        if _DurableQueue.enabled():
            await _DurableQueue.put(request_id, model, req)
        return request_id

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult:
        control.fetches.append((model, request_id))
        control.raise_if_queued("image:fetch")
        if control.pending_fetches > 0:
            control.pending_fetches -= 1
            raise RequestPending(request_id)
        if control.hang_fetches > 0:
            control.hang_fetches -= 1
            await asyncio.sleep(3600)  # until cancelled
        if control.cancel_reports_job_failed and request_id in control.cancelled:
            raise JobFailed(request_id, "cancelled")
        job = self._jobs.get(request_id)
        if job is None and _DurableQueue.enabled():
            job = await _DurableQueue.get(request_id)
        # Like a real queue, a request id is only known to the endpoint that accepted it.
        if job is None or job[0] != model:
            raise RequestUnknown(request_id)
        req = job[1]
        if req.mode == "inpaint" and req.image is not None:
            return ImageResult(data=render_fake_repair(req), provider=self.name, model=model,
                               request_id=request_id, seed=req.seed)  # fmt: skip
        return ImageResult(data=render_fake_image(req), provider=self.name, model=model,
                           request_id=request_id, seed=req.seed)  # fmt: skip


class FakeEmbeddingProvider:
    name = "fake"
    model = "fake-embed"

    def __init__(self, dims: int = 1024) -> None:
        self._dims = dims

    async def embed_images(self, images: list[bytes], ctx: CallContext) -> list[list[float]]:
        control.raise_if_queued("embed")
        out = []
        for data in images:
            small = Image.open(io.BytesIO(data)).convert("RGB").resize((16, 16))
            flat = [b / 255.0 for b in small.tobytes()]  # 768 colour features
            out.append((flat + [0.5] * self._dims)[: self._dims])
        return out


class FakeUpscaleProvider:
    name = "fake"
    model = "fake-upscale"

    async def cancel(self, model: str, request_id: str) -> None:
        control.cancels.append((model, request_id))
        control.cancelled.append(request_id)

    def __init__(self) -> None:
        self._jobs: dict[str, tuple[str, bytes, int]] = {}  # request id -> (endpoint, image, factor)

    async def submit(self, image: bytes, factor: int, ctx: CallContext, *, model: str) -> str:
        control.models_used.append(model)
        control.raise_if_queued("upscale")
        request_id = f"fake-up-{_h(hashlib.sha256(image).hexdigest(), factor, len(control.models_used)):x}"
        self._jobs[request_id] = (model, image, factor)
        return request_id

    async def fetch(self, model: str, request_id: str, *, wait_seconds: float) -> ImageResult:
        control.fetches.append((model, request_id))
        control.raise_if_queued("upscale:fetch")
        if control.pending_upscale_fetches > 0:
            control.pending_upscale_fetches -= 1
            raise RequestPending(request_id)
        job = self._jobs.get(request_id)
        if job is None or job[0] != model:
            raise RequestUnknown(request_id)
        _, image, factor = job
        src = Image.open(io.BytesIO(image))
        big = src.resize((src.width * factor, src.height * factor), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        big.save(out, format="PNG")
        return ImageResult(out.getvalue(), self.name, model, request_id)
