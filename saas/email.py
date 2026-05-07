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
    app = app_config.APP_NAME
    support = app_config.SUPPORT_EMAIL
    subject = f"Install {app} on Discord"
    text = (
        f"Thanks for subscribing to {app}!\n\n"
        f"Click this link any time to install (or re-install) the bot on a "
        f"Discord server:\n\n{url}\n\n"
        f"Keep this email — it's your permanent install/recovery link.\n\n"
        f"Need help? Reply to this email or write to {support}.\n"
    )
    # Brand palette (matches the website):
    #   --accent  #A50256  deep magenta
    #   --accent-h #FF5FA7 hot pink
    #   --accent-d #6e0039 darker magenta
    #   --bg-2    #fff0f6  faint pink wash
    #   --border  #f0c4dc  soft pink
    html = f"""\
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#fff0f6;
             font-family:Georgia,'Times New Roman',serif;color:#0a0a0a;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"
         style="background:#fff0f6;padding:32px 16px;">
    <tr><td align="center">
      <table role="presentation" width="560" cellpadding="0" cellspacing="0" border="0"
             style="max-width:560px;width:100%;background:#ffffff;
                    border:1px solid #f0c4dc;border-top:4px solid #A50256;
                    border-radius:12px;overflow:hidden;">
        <tr><td style="padding:32px 36px 8px 36px;">
          <p style="margin:0 0 4px 0;font-family:Helvetica,Arial,sans-serif;
                    font-size:12px;letter-spacing:0.12em;text-transform:uppercase;
                    color:#A50256;font-weight:700;">
            {app}
          </p>
          <h1 style="margin:0;font-size:26px;line-height:1.25;color:#0a0a0a;
                     font-weight:700;">
            Welcome aboard.
          </h1>
        </td></tr>

        <tr><td style="padding:18px 36px 8px 36px;
                       font-family:Helvetica,Arial,sans-serif;font-size:15px;
                       line-height:1.55;color:#0a0a0a;">
          <p style="margin:0 0 14px 0;">
            Thanks for subscribing. One step left — install the Discord bot
            on the server you want it to coach in.
          </p>
        </td></tr>

        <tr><td align="center" style="padding:14px 36px 8px 36px;">
          <a href="{url}"
             style="display:inline-block;background:#A50256;color:#ffffff;
                    padding:14px 28px;border-radius:8px;text-decoration:none;
                    font-family:Helvetica,Arial,sans-serif;font-weight:700;
                    font-size:15px;letter-spacing:0.02em;
                    border:1px solid #6e0039;">
            Install on Discord
          </a>
        </td></tr>

        <tr><td style="padding:8px 36px 4px 36px;
                       font-family:Helvetica,Arial,sans-serif;font-size:12px;
                       line-height:1.5;color:#5b5b5b;">
          <p style="margin:14px 0 4px 0;">Or copy this URL:</p>
          <p style="margin:0 0 0 0;word-break:break-all;">
            <a href="{url}" style="color:#A50256;text-decoration:underline;">{url}</a>
          </p>
        </td></tr>

        <tr><td style="padding:18px 36px 8px 36px;">
          <div style="background:#fff0f6;border:1px solid #f0c4dc;
                      border-radius:8px;padding:14px 16px;
                      font-family:Helvetica,Arial,sans-serif;font-size:13px;
                      line-height:1.5;color:#0a0a0a;">
            <strong style="color:#6e0039;">Keep this email.</strong>
            It's your permanent install &amp; recovery link — bookmark it or
            star it now. Lose it later? Use
            <a href="{app_config.BASE_URL}/recover"
               style="color:#A50256;text-decoration:underline;">{app_config.BASE_URL}/recover</a>.
          </div>
        </td></tr>

        <tr><td style="padding:18px 36px 28px 36px;
                       border-top:1px solid #f0c4dc;
                       font-family:Helvetica,Arial,sans-serif;font-size:12px;
                       line-height:1.55;color:#5b5b5b;">
          Need help? Reply to this email or write to
          <a href="mailto:{support}" style="color:#A50256;text-decoration:underline;">{support}</a>.
        </td></tr>
      </table>

      <p style="margin:18px 0 0 0;font-family:Helvetica,Arial,sans-serif;
                font-size:11px;color:#8a5a72;">
        {app} · sent because you started a subscription.
      </p>
    </td></tr>
  </table>
</body>
</html>"""
    return await _send(to, subject, html, text)
