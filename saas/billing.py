"""Stripe wrappers — Checkout sessions, billing portal, webhook signature verify."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
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
    """Convert a Stripe SDK object to plain Python (dict / list / scalars).

    Stripe's ``StripeObject`` does not implement ``.get()`` and triggers
    ``AttributeError`` on most attribute access. Its ``__str__`` returns JSON,
    so the safest, version-stable conversion is to round-trip via JSON.
    """
    if isinstance(obj, stripe.stripe_object.StripeObject) if hasattr(stripe, "stripe_object") else False:
        return json.loads(str(obj))
    # Fallback duck-type: Stripe objects in newer SDKs live at stripe._stripe_object.
    cls_name = type(obj).__name__
    mod_name = type(obj).__module__
    if cls_name in ("StripeObject",) or mod_name.startswith("stripe."):
        try:
            return json.loads(str(obj))
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


async def find_latest_session_id_for_email(email: str) -> str | None:
    """Look up the most recent Checkout Session for a customer by email.

    Used by the /recover flow to re-send the install link. Returns the
    session_id (cs_...) or None if nothing matches.
    """
    return await asyncio.to_thread(_find_latest_session_id_for_email_sync, email)


def _find_latest_session_id_for_email_sync(email: str) -> str | None:
    customers = stripe.Customer.list(email=email, limit=10).data or []
    latest_id: str | None = None
    latest_ts = -1
    for c in customers:
        cid = c.get("id") if isinstance(c, dict) else c["id"]
        sessions = stripe.checkout.Session.list(customer=cid, limit=5).data or []
        for s in sessions:
            s = _to_plain(s)
            if s.get("payment_status") not in ("paid", "no_payment_required"):
                continue
            created = int(s.get("created") or 0)
            if created > latest_ts:
                latest_ts = created
                latest_id = s.get("id")
    return latest_id


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
        session_id = obj.get("id")
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
        # Email the permanent recovery link (best-effort; never fails the webhook).
        if email and session_id:
            from saas import email as mailer
            try:
                await mailer.send_install_link(email, session_id)
            except Exception:
                log.exception("send_install_link failed for %s", email)

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
