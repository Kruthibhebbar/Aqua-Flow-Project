"""
mail_utils.py
=====================

Single, shared email-sending function used by every OTP feature
(Signup, Forgot Password, Driver Login, Delivery OTP) - Gmail SMTP
via Flask-Mail only. No third-party email API, no fallback service.

Why emails were failing before:
--------------------------------
1. Resend's API was being used (with Gmail SMTP as a "fallback"), but
   Resend's test mode only allows sending to the account owner's own
   verified email address - every other recipient got a 403 on Render.
   This has been removed completely, exactly as requested.

2. The real bug affecting BOTH localhost and Render: the SMTP send
   ran inside a background thread (to stop a slow mail server from
   hanging the request), but Flask-Mail's mail.send() depends on
   Flask's `current_app` proxy internally - and that proxy only
   resolves inside an active Flask application context. A plain
   background thread has no app context of its own, so mail.send()
   was silently raising "RuntimeError: Working outside of application
   context" every single time the SMTP path was used - which is why
   OTP pages would load fine (nothing to do with SMTP) while the
   actual email never arrived, with no obvious error visible unless
   you were watching the server console closely.

   Fixed by explicitly pushing `app.app_context()` inside the
   background thread before calling mail.send().
"""

import threading


def send_mail_capped(app, mail, msg, timeout_seconds=6):
    """
    Sends a Flask-Mail Message via Gmail SMTP in a background thread,
    capped at timeout_seconds so a slow/blocked connection can never
    hang the request that triggered it.

    Returns True if the send was confirmed complete, False otherwise
    (check the server console for "[mail] SMTP send failed: ..." -
    the exact reason, e.g. bad app password, network block, etc., is
    always logged there). Never raises.

    Used identically by every OTP feature:
        - Signup OTP
        - Forgot Password OTP
        - Driver Login OTP
        - Delivery OTP
    """

    result = {"sent": False, "error": None}

    def _send():
        try:
            # Required: mail.send() reads config via Flask's
            # current_app proxy, which only works inside an active
            # app context. Without this line, every send from this
            # background thread fails silently - see module docstring.
            with app.app_context():
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
