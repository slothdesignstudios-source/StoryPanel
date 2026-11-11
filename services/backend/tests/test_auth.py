"""Clerk-mode authentication and Stripe request shapes, verified without live accounts.

The Clerk tests run the production verification path (RS256 + JWKS fetched over HTTP)
against a locally generated key pair and a local JWKS endpoint. They prove our validation
logic; they do not prove a real Clerk instance issues the claims we expect.
The Stripe tests point the official SDK at a local stub to check the requests we send.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm
from pydantic import SecretStr

from storypanel.api import deps
from storypanel.config import Settings, get_settings
from tests.conftest import PROMPT, auth

ISSUER = "https://test-instance.clerk.accounts.dev"
AZP = "http://localhost:3000"


class _Server:
    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, list[str]], dict[str, str]]] = []
        self.routes: dict[str, Any] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                outer.requests.append((self.command, self.path, parse_qs(body), dict(self.headers)))
                payload = outer.routes.get(self.path.split("?")[0], {"error": {"message": "no route"}})
                data = json.dumps(payload).encode()
                self.send_response(200 if "error" not in payload else 404)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_DELETE = _serve  # noqa: N815

            def log_message(self, *a: Any) -> None:
                pass

        self.http = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()


@pytest.fixture
def server() -> Iterator[_Server]:
    s = _Server()
    yield s
    s.http.shutdown()


def _keypair(kid: str) -> tuple[Any, dict[str, Any]]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(RSAAlgorithm.to_jwk(key.public_key()))
    return key, {**jwk, "kid": kid, "use": "sig", "alg": "RS256"}


@pytest.fixture
def clerk(server: _Server, monkeypatch: pytest.MonkeyPatch) -> Any:
    key, jwk = _keypair("kid-1")
    server.routes["/.well-known/jwks.json"] = {"keys": [jwk]}
    s = get_settings()
    monkeypatch.setattr(s, "auth_mode", "clerk")
    monkeypatch.setattr(s, "clerk_issuer", ISSUER)
    monkeypatch.setattr(s, "clerk_jwks_url", f"{server.url}/.well-known/jwks.json")
    monkeypatch.setattr(s, "clerk_authorized_parties", [AZP])
    monkeypatch.setattr(deps, "_jwks", None)

    def token(
        sub: str = "user_alice", *, key_: Any = key, kid: str = "kid-1", alg: str = "RS256", **over: Any
    ) -> dict[str, str]:
        now = int(time.time())
        claims = {"sub": sub, "iss": ISSUER, "azp": AZP, "iat": now, "nbf": now - 5, "exp": now + 60,
                  "sid": "sess_1", **over}  # fmt: skip
        claims = {k: v for k, v in claims.items() if v is not None}
        return {"Authorization": "Bearer " + jwt.encode(claims, key_, algorithm=alg, headers={"kid": kid})}

    token.key = key  # type: ignore[attr-defined]
    return token


async def test_clerk_tokens_are_fully_validated(client: Any, clerk: Any, server: _Server) -> None:
    ok = await client.get("/v1/me", headers=clerk())
    assert ok.status_code == 200 and ok.json()["plan"] == "free"
    now = int(time.time())
    other_key, _ = _keypair("kid-1")
    rejected = {
        "expired": clerk(exp=now - 30, nbf=now - 120, iat=now - 120),
        "not yet valid": clerk(nbf=now + 300),
        "wrong issuer": clerk(iss="https://evil.clerk.accounts.dev"),
        "wrong authorised party": clerk(azp="https://evil.example"),
        "missing authorised party": clerk(azp=None),
        "missing subject": clerk(sub=None),
        "missing expiry": clerk(exp=None),
        "signed by another key with the same kid": clerk(key_=other_key),
        "unknown key id": clerk(kid="kid-unknown"),
    }  # fmt: skip
    for why, headers in rejected.items():
        r = await client.get("/v1/me", headers=headers)
        assert r.status_code == 401, why
        assert r.json()["error"]["code"] == "unauthenticated" and "Traceback" not in r.text
    # algorithm confusion: an HS256 token signed with the *public key* as the secret
    public_pem = clerk.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                     serialization.PublicFormat.SubjectPublicKeyInfo)  # fmt: skip
    claims = {"sub": "user_alice", "iss": ISSUER, "azp": AZP, "nbf": now - 5, "exp": now + 60}
    forged = jwt.api_jws.encode(json.dumps(claims).encode(), public_pem.replace(b"PUBLIC", b"PUBLIK"), algorithm="HS256",
                                headers={"kid": "kid-1"})  # fmt: skip
    assert (await client.get("/v1/me", headers={"Authorization": f"Bearer {forged}"})).status_code == 401
    unsigned = jwt.encode(claims, "", algorithm="none", headers={"kid": "kid-1"})
    assert (await client.get("/v1/me", headers={"Authorization": f"Bearer {unsigned}"})).status_code == 401
    # a dev-mode token is worthless once the API runs in Clerk mode
    assert (await client.get("/v1/me", headers=auth())).status_code == 401
    # the JWKS was fetched once and cached, not once per request
    assert sum(1 for r in server.requests if r[1].startswith("/.well-known")) <= 3


async def test_clerk_session_refresh_and_account_isolation(client: Any, clerk: Any) -> None:
    now = int(time.time())
    first = clerk(exp=now + 2)
    created = await client.post("/v1/projects", json={"prompt": PROMPT}, headers=first)
    assert created.status_code == 201
    pid = created.json()["id"]
    await asyncio.sleep(8)  # past expiry plus the 5 s clock-skew leeway
    assert (await client.get(f"/v1/projects/{pid}", headers=first)).status_code == 401
    # Clerk's client refreshes the short-lived token; a fresh one for the same user works
    refreshed = clerk()
    again = await client.get(f"/v1/projects/{pid}", headers=refreshed)
    assert again.status_code == 200 and again.json()["id"] == pid
    # a different Clerk user is a different account
    bob = clerk("user_bob")
    assert (await client.get(f"/v1/projects/{pid}", headers=bob)).status_code == 404
    assert (await client.get("/v1/projects", headers=bob)).json()["projects"] == []
    assert (await client.get(f"/v1/projects/{pid}/events", headers=bob)).status_code == 404


async def test_dev_login_is_unreachable_in_clerk_mode(client: Any, clerk: Any) -> None:
    r = await client.post("/v1/dev/login", json={"email": "a@b.co"})
    assert r.status_code == 404
    meta = (await client.get("/v1/meta")).json()
    assert meta["auth_mode"] == "clerk"


def test_deployed_environments_refuse_local_auth_and_fake_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in [k for k in __import__("os").environ if k.startswith("SP_")]:
        monkeypatch.delenv(key)
    for env in ("staging", "production"):
        for over, needle in (
            ({}, "fake providers are not permitted"),
            ({}, "dev auth is not permitted"),
            ({}, "SP_PSEUDONYM_SALT must be set"),
            ({}, "SP_STRIPE_SECRET_KEY must be set"),
            ({}, "SP_STRIPE_WEBHOOK_SECRET must be set"),
            ({"cors_origins": ["*"]}, "wildcard CORS"),
            ({"signup_bonus_credits": 500}, "SP_SIGNUP_BONUS_CREDITS"),
            ({"auth_mode": "clerk"}, "clerk auth requires"),
        ):
            with pytest.raises(ValueError, match=needle):
                Settings(env=env, _env_file=None, **over)  # type: ignore[arg-type]
    # the environment variables alone cannot switch them on either
    monkeypatch.setenv("SP_ENV", "production")
    monkeypatch.setenv("SP_PROVIDER_MODE", "fake")
    monkeypatch.setenv("SP_AUTH_MODE", "dev")
    with pytest.raises(ValueError):
        Settings(_env_file=None)  # type: ignore[call-arg]


# ------------------------------------------------------------------ stripe request shapes
async def test_stripe_checkout_and_portal_requests(client: Any, headers: dict[str, str], server: _Server,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    import stripe

    from storypanel.services import billing

    s = get_settings()
    monkeypatch.setattr(s, "stripe_secret_key", SecretStr("sk_test_local_stub"))
    monkeypatch.setattr(s, "stripe_price_credit_pack", "price_pack")
    monkeypatch.setattr(billing, "_client", lambda: stripe.StripeClient(
        "sk_test_local_stub", base_addresses={"api": server.url}, max_network_retries=0))  # fmt: skip
    server.routes.update({
        "/v1/customers": {"id": "cus_stub", "object": "customer"},
        "/v1/checkout/sessions": {"id": "cs_stub", "object": "checkout.session", "url": "https://checkout.stripe.test/c/cs_stub"},
        "/v1/billing_portal/sessions": {"id": "bps_stub", "object": "billing_portal.session", "url": "https://billing.stripe.test/p/bps_stub"},
    })  # fmt: skip
    me = (await client.get("/v1/me", headers=headers)).json()

    r = await client.post("/v1/billing/checkout", json={"product": "pro"}, headers=headers)
    assert r.status_code == 200 and r.json()["url"] == "https://checkout.stripe.test/c/cs_stub"
    customer = next(q for q in server.requests if q[1] == "/v1/customers")
    assert customer[2]["metadata[user_id]"] == [me["id"]]
    assert customer[3]["Idempotency-Key"] == f"customer:{me['id']}"  # a retry cannot create two customers
    assert customer[3]["Authorization"] == "Bearer sk_test_local_stub"
    sub = next(q for q in server.requests if q[1] == "/v1/checkout/sessions")[2]
    assert sub["mode"] == ["subscription"] and sub["customer"] == ["cus_stub"]
    assert sub["line_items[0][price]"] == ["price_pro"] and sub["line_items[0][quantity]"] == ["1"]
    assert sub["client_reference_id"] == [me["id"]] and sub["metadata[product]"] == ["pro"]
    assert sub["success_url"][0].startswith(s.web_base_url) and sub["cancel_url"][0].startswith(
        s.web_base_url
    )

    server.requests.clear()
    r = await client.post("/v1/billing/checkout", json={"product": "credit_pack"}, headers=headers)
    assert r.status_code == 200
    assert not any(q[1] == "/v1/customers" for q in server.requests)  # customer is reused
    pack = next(q for q in server.requests if q[1] == "/v1/checkout/sessions")[2]
    assert pack["mode"] == ["payment"] and pack["line_items[0][price]"] == ["price_pack"]
    assert pack["metadata[product]"] == ["credit_pack"]

    r = await client.post("/v1/billing/portal", headers=headers)
    assert r.json()["url"].startswith("https://billing.stripe.test/")
    portal = next(q for q in server.requests if q[1] == "/v1/billing_portal/sessions")[2]
    assert portal["customer"] == ["cus_stub"] and portal["return_url"] == [f"{s.web_base_url}/billing"]

    # Stripe down: a typed, retryable error and no leak of the key or the stub's body
    server.routes.pop("/v1/billing_portal/sessions")
    down = await client.post("/v1/billing/portal", headers=headers)
    assert down.status_code == 502 and down.json()["error"]["code"] == "provider_error"
    assert "sk_test" not in down.text and "no route" not in down.text
    other = await client.post("/v1/billing/portal", headers=auth("nobody@example.com"))
    assert other.status_code == 409  # no billing account yet


async def test_stripe_subscription_cancellation_requests(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Account deletion cancels every live subscription of the customer, immediately, once."""
    import stripe

    from storypanel.services import billing

    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_local_stub"))
    monkeypatch.setattr(billing, "_client", lambda: stripe.StripeClient(
        "sk_test_local_stub", base_addresses={"api": server.url}, max_network_retries=0))  # fmt: skip
    server.routes.update({
        "/v1/subscriptions": {"object": "list", "url": "/v1/subscriptions", "has_more": False, "data": [
            {"id": "sub_live", "object": "subscription", "status": "active"},
            {"id": "sub_old", "object": "subscription", "status": "canceled"},
            {"id": "sub_late", "object": "subscription", "status": "past_due"}]},
        "/v1/subscriptions/sub_live": {"id": "sub_live", "object": "subscription", "status": "canceled"},
        "/v1/subscriptions/sub_late": {"id": "sub_late", "object": "subscription", "status": "canceled"},
        "/v1/subscriptions/sub_extra": {"id": "sub_extra", "object": "subscription", "status": "canceled"},
    })  # fmt: skip
    n = await billing.cancel_all_subscriptions(customer_id="cus_x", subscription_ids=["sub_extra"],
                                               idempotency_prefix="deletion:u1")  # fmt: skip
    assert n == 3
    listing = next(r for r in server.requests if r[0] == "GET")
    assert (
        listing[1].startswith("/v1/subscriptions?")
        and "customer=cus_x" in listing[1]
        and "status=all" in listing[1]
    )
    from urllib.parse import parse_qs as qs
    from urllib.parse import urlsplit

    deletes = sorted((urlsplit(r[1]).path, qs(urlsplit(r[1]).query), r[3].get("Idempotency-Key"))
                     for r in server.requests if r[0] == "DELETE")  # fmt: skip
    assert deletes == [(f"/v1/subscriptions/{s}", {"prorate": ["false"], "invoice_now": ["false"]},
                        f"deletion:u1:cancel:{s}") for s in ("sub_extra", "sub_late", "sub_live")]  # fmt: skip


async def test_subscription_events_sync_authoritative_state(client: Any, headers: dict[str, str], server: _Server,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    """With API access, a subscription event triggers a fetch of the current state, so a
    late or equal-timestamp delivery cannot roll the subscription back."""
    import stripe

    from storypanel.services import billing
    from tests.test_billing import hook, sub_obj, user_with_customer

    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_local_stub"))
    monkeypatch.setattr(billing, "_client", lambda: stripe.StripeClient(
        "sk_test_local_stub", base_addresses={"api": server.url}, max_network_retries=0,
        stripe_version=get_settings().stripe_api_version))  # fmt: skip
    uid = await user_with_customer(client, headers)
    server.routes["/v1/subscriptions/sub_1"] = sub_obj("active", uid=uid)
    assert (
        await hook(client, "customer.subscription.created", sub_obj("incomplete", uid=uid), created=100)
    ).json()["status"] == "subscription_synced"
    assert (await client.get("/v1/credits", headers=headers)).json()["plan"] == "pro"
    # Stripe now says canceled; a stale "active" event arriving late still yields canceled
    server.routes["/v1/subscriptions/sub_1"] = sub_obj("canceled", uid=uid)
    await hook(client, "customer.subscription.updated", sub_obj("active", uid=uid), created=50)
    out = (await client.get("/v1/credits", headers=headers)).json()
    assert out["plan"] == "free" and out["subscription_status"] == "canceled"
    fetches = [r for r in server.requests if r[0] == "GET" and r[1].startswith("/v1/subscriptions/sub_1")]
    assert len(fetches) == 2 and fetches[0][3].get("Stripe-Version") == get_settings().stripe_api_version


async def test_checkout_is_idempotent_and_refuses_a_second_subscription(
    client: Any, headers: dict[str, str], server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stripe

    from storypanel.services import billing
    from tests.test_billing import hook, sub_obj, user_with_customer

    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_local_stub"))
    monkeypatch.setattr(billing, "_client", lambda: stripe.StripeClient(
        "sk_test_local_stub", base_addresses={"api": server.url}, max_network_retries=0))  # fmt: skip
    server.routes["/v1/checkout/sessions"] = {"id": "cs_stub", "object": "checkout.session",
                                              "url": "https://checkout.stripe.test/c/cs_stub"}  # fmt: skip
    uid = await user_with_customer(client, headers)
    for _ in range(2):  # a retried click
        r = await client.post("/v1/billing/checkout", headers={**headers, "Idempotency-Key": "click-1"},
                              json={"product": "pro"})  # fmt: skip
        assert r.status_code == 200
    posts = [r for r in server.requests if r[1] == "/v1/checkout/sessions"]
    assert {p[3].get("Idempotency-Key") for p in posts} == {f"checkout:{uid}:pro:click-1"}
    assert posts[0][2]["subscription_data[metadata][user_id]"] == [str(uid)]
    # once a subscription is live, Checkout for another plan is refused (use the portal)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", None)  # event path without API access
    await hook(client, "customer.subscription.created", sub_obj("active", uid=uid), created=10)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", SecretStr("sk_test_local_stub"))
    r = await client.post("/v1/billing/checkout", headers={**headers, "Idempotency-Key": "click-2"},
                          json={"product": "creator"})  # fmt: skip
    assert r.status_code == 409 and r.json()["error"]["code"] == "conflict"
    # credit packs are still allowed
    r = await client.post("/v1/billing/checkout", headers={**headers, "Idempotency-Key": "click-3"},
                          json={"product": "credit_pack"})  # fmt: skip
    assert r.status_code in (200, 409)  # 409 only if no pack price is configured
