"""Transactional email via Resend.

The only thing we send right now is the install/recovery link. If
``RESEND_API_KEY`` is unset, all functions log and no-op so dev environments
work without external credentials.
"""
from __future__ import annotations

import logging
from typing import Optional

import aiohttp

from saas import app_config

log = logging.getLogger("coach.email")

_RESEND_URL = "https://api.resend.com/emails"


async def _send(to: str, subject: str, html: str, text: str) -> bool:
    if not app_config.RESEND_API_KEY:
        log.info("RESEND_API_KEY unset — would send to %s: %s", to, subject)
        return False
    payload = {
        "from": app_config.EMAIL_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
        "text": text,
        "reply_to": app_config.EMAIL_REPLY_TO,
    }
    headers = {
        "Authorization": f"Bearer {app_config.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(_RESEND_URL, json=payload, headers=headers, timeout=15) as r:
                body = await r.text()
                if r.status >= 300:
                    log.warning("Resend send failed (%s): %s", r.status, body)
                    return False
                log.info("Resend sent to %s: %s", to, subject)
                return True
    except aiohttp.ClientError:
        log.exception("Resend HTTP error sending to %s", to)
        return False


def _connect_url(session_id: str) -> str:
    return f"{app_config.BASE_URL}/connect?session_id={session_id}"


async def send_install_link(to: Optional[str], session_id: str) -> bool:
    """Email the permanent /connect link tied to a Stripe Checkout Session."""
    if not to:
        return False
    url = _connect_url(session_id)
    subject = f"Install {app_config.APP_NAME} on Discord"
    text = (
        f"Thanks for subscribing to {app_config.APP_NAME}!\n\n"
        f"Click this link any time to install (or re-install) the bot on a "
        f"Discord server:\n\n{url}\n\n"
        f"Keep this email — it's your permanent install/recovery link.\n\n"
        f"Need help? Reply to this email or write to {app_config.SUPPORT_EMAIL}.\n"
    )
    html = f"""\
<!doctype html>
<html><body style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5;color:#111">
  <h2>Thanks for subscribing to {app_config.APP_NAME}!</h2>
  <p>Click the button below any time to install (or re-install) the bot on a Discord server:</p>
  <p>
    <a href="{url}"
       style="display:inline-block;background:#2563eb;color:#fff;padding:12px 20px;
              border-radius:8px;text-decoration:none;font-weight:600">
      Install on Discord
    </a>
  </p>
  <p style="font-size:13px;color:#555">Or copy this URL:<br>
    <a href="{url}">{url}</a>
  </p>
  <p style="font-size:13px;color:#555">
    Keep this email — it's your permanent install/recovery link.
  </p>
  <hr style="border:none;border-top:1px solid #eee;margin:24px 0">
  <p style="font-size:12px;color:#777">
    Need help? Reply to this email or write to
    <a href="mailto:{app_config.SUPPORT_EMAIL}">{app_config.SUPPORT_EMAIL}</a>.
  </p>
</body></html>"""
    return await _send(to, subject, html, text)
