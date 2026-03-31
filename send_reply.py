#!/usr/bin/env python3
"""
HighTrade Email Reply Sender
Sends emails (or threaded replies) via SMTP with App Password authentication.

Credentials are resolved in this order:
  1. EMAIL_USERNAME / EMAIL_APP_PASSWORD environment variables
  2. trading_data/alert_config.json → channels.email.{username,password}

Usage (module):
    from send_reply import send_email, send_reply

    # New email
    send_email("stantonhigh@gmail.com", "Subject", "Plain text body")

    # Threaded reply (preserves Gmail conversation thread)
    send_reply(
        to="someone@example.com",
        subject="Re: Original subject",
        body="Reply text here",
        reply_to_msg_id="<original-message-id@gmail.com>",
        references="<original-message-id@gmail.com>",
    )

Usage (CLI):
    python3 send_reply.py --to you@example.com --subject "Hello" --body "Test"
    python3 send_reply.py --to you@example.com --subject "Re: Hello" --body "Reply" \
        --reply-to-msg-id "<abc123@gmail.com>"
"""

import argparse
import json
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).parent.resolve()
CONFIG_PATH = SCRIPT_DIR / "trading_data" / "alert_config.json"


# ── Credential resolution ──────────────────────────────────────────────────────

def _load_smtp_config() -> dict:
    """Return SMTP settings merged from config file + env overrides."""
    cfg = {
        "smtp_server": "smtp.gmail.com",
        "smtp_port": 587,
        "username": "",
        "password": "",
        "address": "",
    }

    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            email_cfg = data.get("channels", {}).get("email", {})
            cfg.update({k: v for k, v in email_cfg.items() if v})
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read alert_config.json: %s", exc)

    # Env vars take priority
    if os.environ.get("EMAIL_USERNAME"):
        cfg["username"] = os.environ["EMAIL_USERNAME"]
    if os.environ.get("EMAIL_APP_PASSWORD"):
        cfg["password"] = os.environ["EMAIL_APP_PASSWORD"]

    return cfg


# ── Core send function ─────────────────────────────────────────────────────────

def send_email(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    html_body: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    bcc: Optional[str | list[str]] = None,
    reply_to_msg_id: Optional[str] = None,
    references: Optional[str] = None,
    smtp_config: Optional[dict] = None,
) -> bool:
    """
    Send an email via SMTP with App Password (STARTTLS).

    Args:
        to:               Recipient address or list of addresses.
        subject:          Email subject line.
        body:             Plain-text body (always included).
        html_body:        Optional HTML alternative body.
        cc:               CC address(es).
        bcc:              BCC address(es) — delivered but hidden from headers.
        reply_to_msg_id:  Message-ID of the email being replied to
                          (sets In-Reply-To header for threading).
        references:       Space-separated Message-IDs for the thread chain
                          (sets References header; defaults to reply_to_msg_id).
        smtp_config:      Override SMTP settings dict (useful for testing).

    Returns:
        True on success, False on failure.
    """
    cfg = smtp_config or _load_smtp_config()

    if not cfg.get("username") or not cfg.get("password"):
        logger.error(
            "Email credentials missing. Set EMAIL_USERNAME / EMAIL_APP_PASSWORD "
            "env vars or fill in trading_data/alert_config.json."
        )
        return False

    sender = cfg["username"]

    # Normalise recipient lists
    to_list  = [to]  if isinstance(to,  str) else list(to)
    cc_list  = ([cc]  if isinstance(cc,  str) else list(cc))  if cc  else []
    bcc_list = ([bcc] if isinstance(bcc, str) else list(bcc)) if bcc else []

    # Build MIME message
    msg = MIMEMultipart("alternative") if html_body else MIMEText(body, "plain", "utf-8")

    msg["From"]    = sender
    msg["To"]      = ", ".join(to_list)
    msg["Date"]    = formatdate(localtime=True)
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=sender.split("@")[-1])

    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    # Threading headers
    if reply_to_msg_id:
        msg["In-Reply-To"] = reply_to_msg_id
        msg["References"]  = references or reply_to_msg_id
    elif references:
        msg["References"] = references

    if html_body:
        msg.attach(MIMEText(body,      "plain", "utf-8"))
        msg.attach(MIMEText(html_body, "html",  "utf-8"))

    all_recipients = to_list + cc_list + bcc_list

    try:
        with smtplib.SMTP(cfg["smtp_server"], cfg["smtp_port"], timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(cfg["username"], cfg["password"])
            server.sendmail(sender, all_recipients, msg.as_string())

        logger.info(
            "Email sent → %s | subject: %r | msg-id: %s",
            ", ".join(to_list),
            subject,
            msg["Message-ID"],
        )
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error(
            "SMTP authentication failed for %s. "
            "Ensure you are using a Gmail App Password, not your account password. "
            "Generate one at: Google Account → Security → App Passwords",
            cfg["username"],
        )
        return False
    except smtplib.SMTPException as exc:
        logger.error("SMTP error sending email: %s", exc)
        return False
    except OSError as exc:
        logger.error("Network error sending email: %s", exc)
        return False


def send_reply(
    to: str | list[str],
    subject: str,
    body: str,
    *,
    reply_to_msg_id: str,
    references: Optional[str] = None,
    html_body: Optional[str] = None,
    cc: Optional[str | list[str]] = None,
    bcc: Optional[str | list[str]] = None,
    smtp_config: Optional[dict] = None,
) -> bool:
    """
    Send a threaded reply that appears in the same Gmail conversation.

    reply_to_msg_id is required (the Message-ID header of the original email).
    """
    return send_email(
        to=to,
        subject=subject,
        body=body,
        html_body=html_body,
        cc=cc,
        bcc=bcc,
        reply_to_msg_id=reply_to_msg_id,
        references=references or reply_to_msg_id,
        smtp_config=smtp_config,
    )


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Send an email (or threaded reply) via Gmail App Password SMTP.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--to",       required=True,  help="Recipient email address")
    p.add_argument("--subject",  required=True,  help="Email subject")
    p.add_argument("--body",     required=True,  help="Plain-text email body")
    p.add_argument("--html",                     help="Optional HTML body")
    p.add_argument("--cc",                       help="CC address")
    p.add_argument("--bcc",                      help="BCC address")
    p.add_argument(
        "--reply-to-msg-id",
        dest="reply_to_msg_id",
        help="Message-ID of the email to reply to (enables threading)",
    )
    p.add_argument(
        "--references",
        help="Full References header value (space-separated Message-IDs)",
    )
    p.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )

    ok = send_email(
        to=args.to,
        subject=args.subject,
        body=args.body,
        html_body=args.html,
        cc=args.cc,
        bcc=args.bcc,
        reply_to_msg_id=args.reply_to_msg_id,
        references=args.references,
    )

    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
