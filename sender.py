#!/usr/bin/env python3
"""
sender.py — render and send outreach emails.

Design goals:
  * DRY-RUN BY DEFAULT. Nothing leaves your machine unless you pass a real
    provider. You should always preview rendered emails before sending.
  * Every send is checked against the suppression list and the sent-log
    (no double-contacting) and counted against a daily cap.
  * Every email carries a CAN-SPAM footer: a physical address and a working
    way to opt out. This is legally required for commercial email in the US.

Providers implement one method:  send(to, subject, body) -> (message_id, error).
`DryRunProvider` (default) renders without sending. `SMTPProvider` sends through
any SMTP server (Gmail app-password, your domain, etc.). A Resend/SES provider
can be added the same way.

This module never talks to the DB directly except through the functions passed
in; `send_campaign` takes a db connection so it can enforce suppression/dedupe.
"""

from __future__ import annotations

import smtplib
import time
import uuid
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Dict, List, Optional, Tuple

import db


# --------------------------------------------------------------------------- #
# Sender identity (used for the From header and the required footer)
# --------------------------------------------------------------------------- #

@dataclass
class SenderIdentity:
    from_name: str = "Your Name"
    from_email: str = "you@yourdomain.com"
    # CAN-SPAM requires a valid physical postal address in every commercial email.
    physical_address: str = "123 Main St, Your City, ST 00000"
    # How recipients opt out. A reply instruction always works; a mailto or URL
    # is better. One of these must be present.
    unsubscribe_mailto: str = ""          # e.g. "unsubscribe@yourdomain.com"
    unsubscribe_url: str = ""             # e.g. "https://yourdomain.com/unsub"

    def footer(self) -> str:
        lines = ["", "--", self.from_name, self.physical_address]
        if self.unsubscribe_url:
            lines.append(f"Unsubscribe: {self.unsubscribe_url}")
        elif self.unsubscribe_mailto:
            lines.append(
                f"To stop receiving these emails, email {self.unsubscribe_mailto} "
                f"with 'unsubscribe' in the subject.")
        else:
            lines.append(
                "To stop receiving these emails, reply with 'unsubscribe'.")
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Template rendering
# --------------------------------------------------------------------------- #

class _SafeDict(dict):
    """Leaves unknown {placeholders} visible so typos surface in preview."""
    def __missing__(self, key):  # noqa: D401
        return "{" + key + "}"


def _humanize_signal(signals: List[str]) -> str:
    """Turn the family_signals list into a natural fragment for a template."""
    if not signals:
        return "your restaurant"
    first = signals[0].replace("-", " ").strip()
    return f"a {first} spot" if first else "your restaurant"


def lead_fields(lead: dict) -> Dict[str, str]:
    """Placeholder values available to subject/body templates for one lead."""
    signals = lead.get("family_signals") or []
    if isinstance(signals, str):
        signals = [signals]
    return {
        "name": lead.get("name", "") or "there",
        "city": lead.get("city", ""),
        "phone": lead.get("phone", ""),
        "website": lead.get("website", ""),
        "address": lead.get("address", ""),
        "family_signal": _humanize_signal(signals),
        "family_signals": ", ".join(signals),
    }


def render(template: str, lead: dict) -> str:
    """Fill a template string with a lead's fields (missing keys stay visible)."""
    return template.format_map(_SafeDict(lead_fields(lead)))


def render_email(
    subject_tmpl: str,
    body_tmpl: str,
    lead: dict,
    identity: SenderIdentity,
) -> Tuple[str, str]:
    """Return (subject, full_body_with_footer) for one lead."""
    subject = render(subject_tmpl, lead)
    body = render(body_tmpl, lead).rstrip() + "\n" + identity.footer()
    return subject, body


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #

class DryRunProvider:
    """Renders and 'sends' nothing. Returns a fake message id."""
    name = "dry_run"

    def send(self, to: str, subject: str, body: str) -> Tuple[str, str]:
        return f"dryrun-{uuid.uuid4().hex[:12]}", ""


class SMTPProvider:
    """Send via any SMTP server (Gmail app password, your own domain, etc.).

    NOTE: personal inboxes (Gmail/Outlook) are fine for testing but a poor
    choice for real cold outreach — low daily limits and reputation risk. Use a
    dedicated sending domain + provider for anything beyond a handful.
    """
    name = "smtp"

    def __init__(self, host: str, port: int, username: str, password: str,
                 from_email: str, from_name: str = "", use_tls: bool = True):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.from_email = from_email
        self.from_name = from_name
        self.use_tls = use_tls

    def send(self, to: str, subject: str, body: str) -> Tuple[str, str]:
        msg = EmailMessage()
        msg["From"] = (f"{self.from_name} <{self.from_email}>"
                       if self.from_name else self.from_email)
        msg["To"] = to
        msg["Subject"] = subject
        msg_id = f"<{uuid.uuid4().hex}@{self.from_email.split('@')[-1]}>"
        msg["Message-ID"] = msg_id
        msg.set_content(body)
        try:
            if self.use_tls:
                with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                    s.starttls()
                    s.login(self.username, self.password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP_SSL(self.host, self.port, timeout=30) as s:
                    s.login(self.username, self.password)
                    s.send_message(msg)
            return msg_id, ""
        except Exception as e:  # noqa: BLE001 — surface any send error to the log
            return "", f"{type(e).__name__}: {e}"


# --------------------------------------------------------------------------- #
# Campaign runner
# --------------------------------------------------------------------------- #

@dataclass
class SendResult:
    lead_id: int
    name: str
    to_email: str
    status: str          # dry_run | sent | failed | skipped
    reason: str = ""     # why skipped/failed
    subject: str = ""
    body: str = ""


def send_campaign(
    conn,
    leads: List[dict],
    subject_tmpl: str,
    body_tmpl: str,
    identity: SenderIdentity,
    provider=None,
    *,
    dry_run: bool = True,
    daily_cap: int = 50,
    throttle_seconds: float = 0.0,
    progress: Optional[Callable[[int, int, "SendResult"], None]] = None,
) -> List[SendResult]:
    """Render and (optionally) send to each lead, enforcing all safety checks.

    Order of checks per lead: valid email -> not suppressed -> not already
    contacted (real sends only) -> under daily cap. Everything is logged to the
    `sends` table, including dry runs, so you have a full audit trail.
    """
    if provider is None:
        provider = DryRunProvider()

    results: List[SendResult] = []
    remaining_cap = max(0, daily_cap - db.sends_today(conn)) if not dry_run else None
    total = len(leads)

    for i, lead in enumerate(leads, 1):
        lead_id = lead.get("id")
        name = lead.get("name", "")
        email = (lead.get("email") or "").strip()
        res = SendResult(lead_id=lead_id, name=name, to_email=email,
                         status="skipped")

        if not email or "@" not in email:
            res.reason = "no valid email"
        elif db.is_suppressed(conn, email):
            res.reason = "suppressed (unsubscribed/bounced)"
        elif not dry_run and db.already_sent(conn, lead_id):
            res.reason = "already contacted"
        elif not dry_run and remaining_cap is not None and remaining_cap <= 0:
            res.reason = f"daily cap reached ({daily_cap})"
        else:
            subject, body = render_email(subject_tmpl, body_tmpl, lead, identity)
            res.subject, res.body = subject, body
            if dry_run:
                mid, _ = DryRunProvider().send(email, subject, body)
                db.record_send(conn, lead_id, email, subject, body,
                               db.SEND_DRY_RUN, "dry_run", mid)
                res.status = "dry_run"
            else:
                mid, err = provider.send(email, subject, body)
                if err:
                    db.record_send(conn, lead_id, email, subject, body,
                                   db.SEND_FAILED, provider.name, "", err)
                    res.status, res.reason = "failed", err
                else:
                    db.record_send(conn, lead_id, email, subject, body,
                                   db.SEND_SENT, provider.name, mid)
                    res.status = "sent"
                    if remaining_cap is not None:
                        remaining_cap -= 1
                    if throttle_seconds:
                        time.sleep(throttle_seconds)

        results.append(res)
        if progress:
            progress(i, total, res)

    return results


# Default templates — plain, honest, personalized. Edit freely in the UI.
DEFAULT_SUBJECT = "Quick question for {name}"
DEFAULT_BODY = (
    "Hi {name} team,\n\n"
    "I came across {name} in {city} and loved that you're {family_signal}. "
    "I work with independent restaurants on [what you offer] and thought it "
    "might be a fit.\n\n"
    "Would you be open to a quick chat this week?\n\n"
    "Thanks so much,\n"
    "[Your name]"
)


if __name__ == "__main__":
    # Smoke test: render + dry-run against an in-memory DB.
    conn = db.connect(":memory:")
    db.upsert_leads(conn, [{
        "name": "Battle Street Bistro", "city": "Manassas",
        "email": "hi@example-battle.com", "family_score": 9,
        "family_signals": ["family owned", "since 1998"],
    }])
    leads = db.list_leads(conn)
    ident = SenderIdentity(from_name="Pat Lead", from_email="pat@frenzy.co",
                           physical_address="500 Market St, Reston, VA 20190")
    out = send_campaign(conn, leads, DEFAULT_SUBJECT, DEFAULT_BODY, ident,
                        dry_run=True)
    r = out[0]
    print(f"[{r.status}] -> {r.to_email}")
    print("SUBJECT:", r.subject)
    print(r.body)
    print("\nsends logged:", len(db.list_sends(conn)))
