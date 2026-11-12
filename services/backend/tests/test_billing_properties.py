"""Entitlements are a function of the set of paid invoices, not of delivery order or repetition.

For each scenario the expected plan credits are written down from the policy in
docs/billing-policy.md (one allowance per paid subscription period, topped up on upgrade, no
clawback on downgrade). The same invoices are then delivered in many shuffled orders, with
duplicates under a new event id and re-deliveries of the same event id, interleaved with
subscription state events. Every ordering must end at exactly the expected total: neither a
duplicate nor a missing grant."""

from __future__ import annotations

import random
import uuid
from typing import Any

import pytest

from tests.conftest import auth
from tests.test_billing import NEXT, PERIOD, hook, invoice, line, plan_credits, sub_obj, user_with_customer

CREATOR, PRO = 80, 240  # monthly allowances (tests/test_billing.py)
DAY = 86400


def _scenarios(n: str) -> dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]], int]]:
    """name -> (paid invoices, subscription events, expected plan credits). Ids are unique per run."""
    a, b, c = f"sub_a{n}", f"sub_b{n}", f"cus_{n}"
    change = PERIOD[0] + 10 * DAY
    reset = (change, change + 2_592_000)
    after_reset = (reset[1], reset[1] + 2_592_000)
    third = (NEXT[1], NEXT[1] + 2_600_000)

    def inv(name: str, reason: str, lines: list[dict[str, Any]], sub: str = a) -> dict[str, Any]:
        return invoice(f"in_{name}_{n}", reason, lines, sub=sub, customer=c)

    def ln(
        price: str, period: tuple[int, int], amount: int, sub: str = a, proration: bool = False
    ) -> dict[str, Any]:
        return line(price, period, amount, sub=sub, proration=proration)

    return {
        # create creator -> immediate-proration upgrade to pro -> renewal at pro -> downgrade
        # (no clawback) -> renewal at creator
        "upgrade_renew_downgrade": (
            [
                inv("create", "subscription_create", [ln("price_creator", PERIOD, 900)]),
                inv(
                    "up",
                    "subscription_update",
                    [
                        ln("price_creator", (change, PERIOD[1]), -600, proration=True),
                        ln("price_pro", (change, PERIOD[1]), 1300, proration=True),
                    ],
                ),
                inv("renew", "subscription_cycle", [ln("price_pro", NEXT, 2900)]),
                inv(
                    "down",
                    "subscription_update",
                    [
                        ln("price_pro", (NEXT[0] + 5, NEXT[1]), -2000, proration=True),
                        ln("price_creator", (NEXT[0] + 5, NEXT[1]), 600, proration=True),
                    ],
                ),
                inv("renew2", "subscription_cycle", [ln("price_creator", third, 900)]),
            ],
            [
                sub_obj("active", "price_creator", a),
                sub_obj("active", "price_pro", a, period=NEXT),
                sub_obj("active", "price_creator", a, period=third),
            ],
            CREATOR + (PRO - CREATOR) + PRO + CREATOR,
        ),
        # billing-date reset on upgrade: a new full pro period starts mid-period; then it renews
        "anchor_reset": (
            [
                inv("create", "subscription_create", [ln("price_creator", PERIOD, 900)]),
                inv(
                    "reset",
                    "subscription_update",
                    [
                        ln("price_creator", (change, PERIOD[1]), -600, proration=True),
                        ln("price_pro", reset, 2900),
                    ],
                ),
                inv("renew", "subscription_cycle", [ln("price_pro", after_reset, 2900)]),
            ],
            [sub_obj("active", "price_creator", a), sub_obj("active", "price_pro", a, period=reset)],
            CREATOR + PRO + PRO,
        ),
        # deferred proration: the upgrade's prorations ride on the next cycle invoice
        "deferred_proration": (
            [
                inv("create", "subscription_create", [ln("price_creator", PERIOD, 900)]),
                inv(
                    "cycle",
                    "subscription_cycle",
                    [
                        ln("price_creator", (change, PERIOD[1]), -600, proration=True),
                        ln("price_pro", (change, PERIOD[1]), 1300, proration=True),
                        ln("price_pro", NEXT, 2900),
                    ],
                ),
            ],
            [sub_obj("active", "price_pro", a, period=NEXT)],
            CREATOR + (PRO - CREATOR) + PRO,
        ),
        # two live subscriptions on one customer: each paid period earns its own allowance
        "two_subscriptions": (
            [
                inv("a1", "subscription_create", [ln("price_creator", PERIOD, 900)]),
                inv("b1", "subscription_create", [ln("price_pro", PERIOD, 2900, sub=b)], sub=b),
                inv("a2", "subscription_cycle", [ln("price_creator", NEXT, 900)]),
                inv("b2", "subscription_cycle", [ln("price_pro", NEXT, 2900, sub=b)], sub=b),
            ],
            [sub_obj("active", "price_creator", a), sub_obj("active", "price_pro", b)],
            2 * CREATOR + 2 * PRO,
        ),
        # a second qualifying invoice for an already-paid period (e.g. a manual invoice) grants nothing
        "second_invoice_same_period": (
            [
                inv("create", "subscription_create", [ln("price_pro", PERIOD, 2900)]),
                inv("extra", "subscription_update", [ln("price_pro", PERIOD, 2900)]),
            ],
            [],
            PRO,
        ),
    }


@pytest.mark.parametrize("scenario", ["upgrade_renew_downgrade", "anchor_reset", "deferred_proration",
                                      "two_subscriptions", "second_invoice_same_period"])  # fmt: skip
async def test_credits_depend_only_on_the_set_of_paid_invoices(client: Any, scenario: str) -> None:
    rng = random.Random(f"billing-{scenario}")  # deterministic shuffles, reproducible failures
    for trial in range(8):
        n = uuid.uuid4().hex[:8]
        headers = auth(f"prop-{scenario}-{trial}-{n}@example.com")
        invoices, subs, expected = _scenarios(n)[scenario]
        uid = await user_with_customer(client, headers, customer=f"cus_{n}")
        for s in subs:
            s["customer"], s["metadata"] = f"cus_{n}", {"user_id": str(uid)}
        events: list[tuple[str, dict[str, Any], str]] = [("invoice.paid", i, f"evt_{uuid.uuid4().hex[:12]}")
                                                         for i in invoices]  # fmt: skip
        events += [("customer.subscription.updated", s, f"evt_{uuid.uuid4().hex[:12]}") for s in subs]
        # duplicates: the same invoice under a new event id, and the same event delivered twice
        for kind, obj, _ in rng.sample(events, k=min(3, len(events))):
            events.append((kind, obj, f"evt_{uuid.uuid4().hex[:12]}"))
        events += rng.sample(events, k=min(2, len(events)))
        rng.shuffle(events)
        for t, (kind, obj, eid) in enumerate(events):
            r = await hook(client, kind, obj, eid=eid, created=1_790_000_000 + rng.randrange(10_000))
            assert r.status_code == 200, (scenario, trial, kind, r.text)
        got = await plan_credits(uid)
        order = [(k, o["id"]) for k, o, _ in events]
        assert got == expected, f"{scenario} trial {trial}: {got} != {expected} for delivery order {order}"
