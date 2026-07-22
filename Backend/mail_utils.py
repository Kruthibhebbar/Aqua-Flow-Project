import os
import threading
import requests

_RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
_RESEND_FROM = os.environ.get("RESEND_FROM_EMAIL", "AquaFlow <onboarding@resend.dev>").strip()


def _send_via_resend(msg):
    """Sends a Flask-Mail Message object via Resend's HTTPS API. Returns
    True/False. Raises nothing - failures are logged and return False."""
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_API_KEY}"},
            json={
                "from": _RESEND_FROM,
                "to": msg.recipients,
                "subject": msg.subject,
                "text": msg.body
            },
            timeout=10
        )
        if resp.status_code in (200, 201):
            return True
        print(f"[mail] Resend API error {resp.status_code}: {resp.text}")
        return False
    except Exception as e:
        print(f"[mail] Resend request failed: {e}")
        return False


def _send_via_smtp_capped(mail, msg, timeout_seconds):
    """Original Gmail-SMTP-via-Flask-Mail path, capped with a background
    thread so a blocked/slow connection can't hang the request."""
    result = {"sent": False, "error": None}

    def _send():
        try:
            mail.send(msg)
            result["sent"] = True
        except Exception as e:
            result["error"] = str(e)

    thread = threading.Thread(target=_send, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)

    if thread.is_alive():
        print(f"[mail] SMTP send exceeded {timeout_seconds}s - continuing without "
              f"blocking the request (subject: {getattr(msg, 'subject', '?')})")
        return False

    if result["error"]:
        print(f"[mail] SMTP send failed: {result['error']}")
        return False

    return result["sent"]


def send_mail_capped(mail, msg, timeout_seconds=6):
    """
    Returns True if the email was confirmed sent, False otherwise (check
    server logs for the specific reason). Never raises, never blocks
    longer than timeout_seconds regardless of what the mail server does.

    Uses Resend's HTTP API automatically if RESEND_API_KEY is set (fixes
    Render's outbound-SMTP block); otherwise falls back to Gmail SMTP via
    Flask-Mail exactly as before.
    """
    if _RESEND_API_KEY:
        sent = _send_via_resend(msg)
        if sent:
            return True
        # Fall through to SMTP only if Resend itself is misconfigured -
        # keeps the app working while you're still setting it up.
        print("[mail] Resend send failed, falling back to SMTP")

    return _send_via_smtp_capped(mail, msg, timeout_seconds)
