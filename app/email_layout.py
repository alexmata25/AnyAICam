"""One branded, mobile-friendly frame for AnyAiCam customer emails
(2026-09-29). Alert emails (notification_email.py) use the same look;
account and purchase emails now share it instead of plain unbranded HTML.

Inline styles only (mail clients strip <style>), a single 600 px column
that shrinks on phones, and no remote images (often blocked)."""
from __future__ import annotations

import html

BRAND = "#0e7c7b"


def button(label: str, url: str) -> str:
    return (f'<a href="{html.escape(url, quote=True)}" style="background:{BRAND};color:#ffffff;padding:12px 18px;'
            f'border-radius:6px;text-decoration:none;display:inline-block;font-weight:bold">{html.escape(label)}</a>')


def wrap(inner_html: str, *, preheader: str = "", footer: str = "AnyAiCam · Smart video security") -> str:
    """inner_html is trusted, already-escaped markup from the caller."""
    return (
        '<div style="background:#f2f4f7;padding:16px 8px">'
        + (f'<div style="display:none;max-height:0;overflow:hidden;opacity:0">{html.escape(preheader)}</div>' if preheader else "")
        + '<div style="font-family:Arial,Helvetica,sans-serif;max-width:600px;margin:0 auto;background:#ffffff;border-radius:10px;'
          'padding:20px 18px;color:#101828;font-size:15px;line-height:1.5">'
        f'<div style="font-weight:bold;font-size:15px;color:{BRAND};letter-spacing:.3px;margin:0 0 14px">AnyAiCam</div>'
        f"{inner_html}</div>"
        '<p style="font-family:Arial,Helvetica,sans-serif;color:#667085;font-size:12px;text-align:center;max-width:600px;'
        f'margin:12px auto 0">{html.escape(footer)}</p></div>'
    )


def password_reset_email(link: str, *, initiated_by_admin: bool = False) -> tuple[str, str, str]:
    """(subject, text, html) for a password reset link valid for one hour."""
    subject = "Reset your AnyAiCam password"
    intro = ("An AnyAiCam administrator started a password reset for your account."
             if initiated_by_admin else "We received a request to reset the password for your AnyAiCam account.")
    text = (f"{intro}\n\nChoose a new password with this link (it expires in one hour):\n{link}\n\n"
            "If you didn't ask for this, you can ignore this email; your current password keeps working.\n\n"
            "— The AnyAiCam Team")
    body = (
        '<h2 style="margin:0 0 10px;font-size:20px">Reset your password</h2>'
        f"<p>{html.escape(intro)}</p>"
        f'<p style="margin:18px 0">{button("Choose a new password", link)}</p>'
        '<p style="color:#475467;font-size:13px">This link expires in one hour. If the button doesn\'t work, copy this address into your browser:<br>'
        f'<span style="word-break:break-all">{html.escape(link)}</span></p>'
        '<p style="color:#475467;font-size:13px">If you didn\'t ask for this, you can ignore this email; your current password keeps working.</p>'
    )
    return subject, text, wrap(body, preheader="Choose a new password for your AnyAiCam account.")
