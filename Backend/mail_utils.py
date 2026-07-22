"""
Sends email with a hard wall-clock cap.

Flask-Mail's mail.send() goes through Python's smtplib with NO timeout
configured by default - if the SMTP connection is silently dropped or
throttled (common on free hosting tiers restricting outbound SMTP), the
call doesn't fail, it just hangs. On a request like driver login that
sends an OTP email as one of its steps, that means the whole login
request hangs too - "the page just spins forever."

This runs the send in a background thread and only waits up to
`timeout_seconds` for it. If it doesn't finish in time, the request
continues anyway (login still succeeds) and the mail thread either
finishes silently in the background or is abandoned - either way, the
person using the app is never stuck waiting on Gmail's SMTP server.
"""

import threading


def send_mail_capped(mail, msg, timeout_seconds=6):
    """
    Returns True if the email was confirmed sent within the time limit,
    False otherwise (timed out OR a real send error - check server logs
    for which one). Never raises, and never blocks longer than
    timeout_seconds regardless of what the SMTP server does.
    """
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
        print(f"[mail] send exceeded {timeout_seconds}s - continuing without "
              f"blocking the request (subject: {getattr(msg, 'subject', '?')})")
        return False

    if result["error"]:
        print(f"[mail] send failed: {result['error']}")
        return False

    return result["sent"]
