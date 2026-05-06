"""Stripe wrappers — Checkout sessions, billing portal, webhook signature verify."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Any

import stripe

from saas import app_config

log = logging.getLogger("coach.billing")

stripe.api_key = app_config.STRIPE_SECRET_KEY


def _ts_to_dt(ts) -> dt.datetime | None:
    if ts is None:
        return None
    return dt.datetime.fromtimestamp(int(ts), tz=dt.timezone.utc)


def _to_plain(obj: Any) -> Any:
    """Recursively convert a Stripe SDK object (or container) to plain Python.

    Stripe's ``StripeObject`` is dict-like (iterating yields keys) but does not
    expose ``.get()`` and the recursive ``to_dict_recursive`` helper is private
    in newer SDK versions. We convert by walking keys.
    """
    # StripeObject lives at stripe._stripe_object in modern SDKs; duck-type via keys().
    if hasattr(obj, "keys") and hasattr(obj, "__getitem__") and not isinstance(obj, dict):
        try:
            return {k: _to_plain(obj[k]) for k in obj.keys()}
        except Exception:
            pass
    if isinstance(obj, dict):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_plain(v) for v in obj]
    return obj


# --- public API ----------------------------------------------------------

async def create_checkout_session() -> str:
    """Create a Stripe Checkout session and return its URL."""
    return await asyncio.to_thread(_create_checkout_session_sync)


def _create_checkout_session_sync() -> str:
    s = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": app_config.STRIPE_PRICE_ID, "quantity": 1}],
        subscription_data={"trial_period_days": 7},
        success_url=app_config.stripe_success_url(),
        cancel_url=app_config.stripe_cancel_url(),
        allow_promotion_codes=True,
        billing_address_collection="auto",
    )
    return s.url


async def retrieve_session(session_id: str) -> dict[str, Any]:
    sess = await asyncio.to_thread(stripe.checkout.Session.retrieve, session_id)
    return _to_plain(sess)


async def create_billing_portal_url(customer_id: str) -> str:
    sess = await asyncio.to_thread(
        stripe.billing_portal.Session.create,
        customer=customer_id,
        return_url=app_config.BASE_URL,
    )
    return sess.url


def verify_webhook(payload: bytes, sig_header: str) -> dict:
    """Verify Stripe webhook signature, return the parsed event as a plain dict."""
    event = stripe.Webhook.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=app_config.STRIPE_WEBHOOK_SECRET,
    )
    return _to_plain(event)


def map_subscription_status(stripe_status: str) -> str:
    """Map a Stripe subscription status onto our internal `tenant_status` enum."""
    return {
        "trialing":           "trialing",
        "active":             "active",
        "past_due":           "past_due",
        "unpaid":             "past_due",
        "incomplete":         "pending",
        "incomplete_expired": "canceled",
        "canceled":           "canceled",
        "paused":             "past_due",
    }.get(stripe_status, "pending")


# --- webhook event handlers ---------------------------------------------

async def handle_event(event: dict) -> None:
    from saas import db  # local import to avoid cycle on cold imports

    etype = event.get("type")
    obj = (event.get("data") or {}).get("object") or {}
    log.info("Stripe event: %s", etype)

    if etype == "checkout.session.completed":
        customer_id = obj.get("customer")
        sub_id = obj.get("subscription")
        email = (obj.get("customer_details") or {}).get("email") or obj.get("customer_email")
        if customer_id:
            # We may not yet have full subscription details — mark pending; the
            # subsequent `customer.subscription.created/updated` event flips us
            # to trialing/active.
            await db.upsert_tenant_from_stripe(
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                status="pending",
                trial_end=None,
                install_email=email,
            )

    elif etype in ("customer.subscription.created",
                   "customer.subscription.updated",
                   "customer.subscription.trial_will_end"):
        customer_id = obj.get("customer")
        sub_id      = obj.get("id")
        status      = map_subscription_status(obj.get("status", ""))
        trial_end   = _ts_to_dt(obj.get("trial_end"))
        if customer_id:
            await db.upsert_tenant_from_stripe(
                stripe_customer_id=customer_id,
                stripe_subscription_id=sub_id,
                status=status,
                trial_end=trial_end,
            )

    elif etype == "customer.subscription.deleted":
        customer_id = obj.get("customer")
        if customer_id:
            await db.set_tenant_status(customer_id, "canceled")

    elif etype == "invoice.payment_failed":
        customer_id = obj.get("customer")
        if customer_id:
            await db.set_tenant_status(customer_id, "past_due")
