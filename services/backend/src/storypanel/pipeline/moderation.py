"""Moderation gate for text: deterministic rule lists first, then an LLM classifier.

This implements the product's content policy. It is a product control, not a legal
determination; IP and likeness handling still needs legal review before launch.
"""

from __future__ import annotations

import re

from storypanel.catalog import PROMPT_VERSION, render_prompt
from storypanel.domain.schemas import AgeRating, ModerationVerdict
from storypanel.obs import metrics
from storypanel.providers import metering
from storypanel.providers.base import CallContext

# Illustrative seed lists. They are data: extend them without touching pipeline code.
IP_NAMES = (
    "spider-man", "spiderman", "batman", "superman", "wonder woman", "iron man", "pikachu",
    "pokemon", "pokémon", "mickey mouse", "donald duck", "naruto", "goku", "dragon ball",
    "harry potter", "darth vader", "star wars", "spongebob", "hello kitty", "super mario",
    "sonic the hedgehog", "elsa", "shrek", "minions", "peppa pig", "bluey", "one piece", "luffy",
    "sailor moon", "totoro", "the simpsons", "homer simpson", "bart simpson", "marvel", "disney",
)  # fmt: skip
SEXUAL_TERMS = (
    "nsfw", "porn", "pornographic", "hentai", "erotic", "erotica", "explicit sex", "sex scene",
    "nude", "naked", "topless", "lingerie", "fetish", "sexy", "seductive", "lewd",
)  # fmt: skip
MINOR_TERMS = ("child", "kid", "minor", "underage", "loli", "shota", "schoolgirl", "schoolboy",
               "teen", "toddler", "baby", "boy", "girl")  # fmt: skip
LIKENESS = re.compile(
    r"\b(photo(graph)? of (me|my|him|her|them)|looks? (exactly )?like (my|the real)|"
    r"real[- ]life (person|celebrity|politician)|likeness of|deepfake)\b", re.I)  # fmt: skip
STYLE_OF = re.compile(
    r"\b(in|with|using) the (art |drawing |visual )?style of ([A-Z][\w.'-]+(?: [A-Z][\w.'-]+){0,3})"
    r"|\b([A-Z][\w.'-]+(?: [A-Z][\w.'-]+){0,2})[- ]style\b"
)


def _word_hit(text: str, terms: tuple[str, ...]) -> str | None:
    low = text.lower()
    for term in terms:
        if re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", low):
            return term
    return None


def neutralise_style_references(text: str) -> tuple[str, bool]:
    """Requests to imitate a named artist are mapped to presets, so the name is dropped."""
    new = STYLE_OF.sub(lambda m: "" if m.group(3) else m.group(0), text)
    return re.sub(r"\s{2,}", " ", new).strip(), new != text


def rule_check(text: str) -> ModerationVerdict | None:
    """Fast, deterministic blocks. Returns None when the rules have nothing to say."""
    sexual = _word_hit(text, SEXUAL_TERMS)
    if sexual and _word_hit(text, MINOR_TERMS):
        return ModerationVerdict(
            allowed=False, categories=["minors", "sexual"],
            user_message="This request can't be used. Sexual content involving minors is never "
                         "allowed.", suggested_rewrite=None)  # fmt: skip
    if sexual:
        return ModerationVerdict(
            allowed=False, categories=["sexual"],
            user_message="StoryPanel doesn't create sexual content. Try a different idea.",
            suggested_rewrite=None)  # fmt: skip
    ip = _word_hit(text, IP_NAMES)
    if ip:
        return ModerationVerdict(
            allowed=False, categories=["ip"],
            user_message="That looks like an existing copyrighted character or brand. "
                         "Describe an original character instead.",
            suggested_rewrite="Try an original hero with their own name, look and powers.")  # fmt: skip
    if LIKENESS.search(text):
        return ModerationVerdict(
            allowed=False, categories=["real_person"],
            user_message="Comics can't depict real people. Describe an invented character instead.",
            suggested_rewrite="Describe a fictional character by their look and personality.")  # fmt: skip
    return None


async def moderate_text(
    text: str, *, audience: AgeRating, surface: str, ctx: CallContext, use_llm: bool = True
) -> ModerationVerdict:
    verdict = rule_check(text)
    if verdict is None and use_llm and text.strip():
        verdict = await metering.structured(
            role="light",
            system=render_prompt("moderation", audience=audience.value),
            user=f"<content>\n{text}\n</content>",
            schema=ModerationVerdict,
            ctx=CallContext(
                stage=f"moderation.{surface}",
                user_id=ctx.user_id,
                project_id=ctx.project_id,
                target_id=ctx.target_id,
                prompt_version=PROMPT_VERSION,
            ),  # fmt: skip
        )
    verdict = verdict or ModerationVerdict(allowed=True, categories=[], user_message="")
    if "minors" in verdict.categories:  # absolute: no classifier output can allow it
        verdict = verdict.model_copy(update={"allowed": False})
    metrics.moderation_results.labels(surface, "allowed" if verdict.allowed else "blocked").inc()
    return verdict
