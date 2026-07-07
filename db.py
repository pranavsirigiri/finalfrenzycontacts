#!/usr/bin/env python3
"""
db.py — SQLite persistence for the restaurant outreach tool.

Keeps state that must survive between runs:
  * leads        — every discovered restaurant, deduped, with a contact status.
  * sends        — a log of every email (dry-run or real) we've generated.
  * suppression  — addresses we must never contact (unsubscribes / bounces).

Stdlib only (sqlite3) — no extra dependency. The default database file is
`outreach.db` in the working directory.

Typical use:
    import db
    conn = db.connect()
    added, updated = db.upsert_leads(conn, leads)     # leads = List[Lead] or dicts
    rows = db.list_leads(conn, status="new", has_email=True)
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import asdict, is_dataclass
from typing import Dict, Iterable, List, Optional

DEFAULT_DB_PATH = "outreach.db"

# Lead lifecycle. A lead moves new -> contacted once a real email is sent.
STATUS_NEW = "new"
STATUS_CONTACTED = "contacted"
STATUS_REPLIED = "replied"
STATUS_BOUNCED = "bounced"
STATUS_UNSUBSCRIBED = "unsubscribed"

# Send outcomes recorded in the `sends` table.
SEND_DRY_RUN = "dry_run"
SEND_SENT = "sent"
SEND_FAILED = "failed"


SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    dedupe_key     TEXT UNIQUE NOT NULL,
    name           TEXT NOT NULL,
    category       TEXT,
    city           TEXT,
    phone          TEXT,
    email          TEXT,
    website        TEXT,
    source         TEXT,
    address        TEXT,
    family_score   INTEGER DEFAULT 0,
    family_signals TEXT,              -- JSON array
    rating         REAL,
    review_count   INTEGER,
    place_id       TEXT,
    status         TEXT NOT NULL DEFAULT 'new',
    first_seen     REAL NOT NULL,
    last_updated   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id     INTEGER NOT NULL,
    to_email    TEXT NOT NULL,
    subject     TEXT,
    body        TEXT,
    status      TEXT NOT NULL,        -- dry_run | sent | failed
    provider    TEXT,
    message_id  TEXT,
    error       TEXT,
    created_at  REAL NOT NULL,
    FOREIGN KEY (lead_id) REFERENCES leads(id)
);

CREATE TABLE IF NOT EXISTS suppression (
    email      TEXT PRIMARY KEY,      -- always stored lowercased
    reason     TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_sends_lead   ON sends(lead_id);
"""


# --------------------------------------------------------------------------- #
# Connection / schema
# --------------------------------------------------------------------------- #

def connect(path: str = DEFAULT_DB_PATH,
            check_same_thread: bool = True) -> sqlite3.Connection:
    """Open (creating if needed) the database and ensure the schema exists.

    Streamlit reruns the script on a pool of threads, so the UI passes
    check_same_thread=False to reuse one cached connection across reruns.
    """
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _as_dict(lead) -> dict:
    """Accept either a Lead dataclass or a plain dict."""
    if is_dataclass(lead):
        return asdict(lead)
    if isinstance(lead, dict):
        return dict(lead)
    raise TypeError(f"Unsupported lead type: {type(lead)!r}")


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def dedupe_key(lead: dict) -> str:
    """Stable identity for a lead: prefer place_id, else normalized name+city."""
    place_id = (lead.get("place_id") or "").strip()
    if place_id:
        return f"pid:{place_id}"
    return f"nc:{_norm_name(lead.get('name', ''))}|{(lead.get('city') or '').strip().lower()}"


# --------------------------------------------------------------------------- #
# Leads
# --------------------------------------------------------------------------- #

def upsert_leads(conn: sqlite3.Connection, leads: Iterable) -> tuple[int, int]:
    """Insert new leads; update contact fields on existing ones.

    Never downgrades status or overwrites a non-empty email/phone with a blank.
    Returns (added, updated).
    """
    added = updated = 0
    now = time.time()
    for raw in leads:
        d = _as_dict(raw)
        key = dedupe_key(d)
        signals = d.get("family_signals") or []
        signals_json = json.dumps(signals if isinstance(signals, list) else [signals])

        existing = conn.execute(
            "SELECT id, email, phone FROM leads WHERE dedupe_key = ?", (key,)
        ).fetchone()

        if existing is None:
            conn.execute(
                """INSERT INTO leads
                   (dedupe_key, name, category, city, phone, email, website,
                    source, address, family_score, family_signals, rating,
                    review_count, place_id, status, first_seen, last_updated)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (key, d.get("name", ""), d.get("category", "restaurant"),
                 d.get("city", ""), d.get("phone", ""), d.get("email", ""),
                 d.get("website", ""), d.get("source", ""), d.get("address", ""),
                 int(d.get("family_score") or 0), signals_json,
                 d.get("rating"), d.get("review_count"), d.get("place_id", ""),
                 STATUS_NEW, now, now),
            )
            added += 1
        else:
            # Only fill blanks; keep whatever contact info we already trust.
            conn.execute(
                """UPDATE leads SET
                       email = CASE WHEN COALESCE(email,'')='' THEN ? ELSE email END,
                       phone = CASE WHEN COALESCE(phone,'')='' THEN ? ELSE phone END,
                       website = CASE WHEN COALESCE(website,'')='' THEN ? ELSE website END,
                       family_score = ?, family_signals = ?, rating = ?,
                       review_count = ?, last_updated = ?
                   WHERE id = ?""",
                (d.get("email", ""), d.get("phone", ""), d.get("website", ""),
                 int(d.get("family_score") or 0), signals_json, d.get("rating"),
                 d.get("review_count"), now, existing["id"]),
            )
            updated += 1
    conn.commit()
    return added, updated


def list_leads(
    conn: sqlite3.Connection,
    status: Optional[str] = None,
    has_email: Optional[bool] = None,
    order_by: str = "family_score DESC, review_count DESC",
) -> List[dict]:
    """Return leads as dicts, optionally filtered by status / email presence."""
    where = []
    params: list = []
    if status is not None:
        where.append("status = ?")
        params.append(status)
    if has_email is True:
        where.append("COALESCE(email,'') != ''")
    elif has_email is False:
        where.append("COALESCE(email,'') = ''")
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM leads {clause} ORDER BY {order_by}", params
    ).fetchall()
    return [_row_to_lead(r) for r in rows]


def get_lead(conn: sqlite3.Connection, lead_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
    return _row_to_lead(row) if row else None


def set_lead_status(conn: sqlite3.Connection, lead_id: int, status: str) -> None:
    conn.execute(
        "UPDATE leads SET status = ?, last_updated = ? WHERE id = ?",
        (status, time.time(), lead_id),
    )
    conn.commit()


def _row_to_lead(row: sqlite3.Row) -> dict:
    d = dict(row)
    try:
        d["family_signals"] = json.loads(d.get("family_signals") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["family_signals"] = []
    return d


# --------------------------------------------------------------------------- #
# Sends
# --------------------------------------------------------------------------- #

def record_send(
    conn: sqlite3.Connection,
    lead_id: int,
    to_email: str,
    subject: str,
    body: str,
    status: str,
    provider: str = "",
    message_id: str = "",
    error: str = "",
) -> int:
    """Log a send attempt. A real 'sent' also flips the lead to 'contacted'."""
    now = time.time()
    cur = conn.execute(
        """INSERT INTO sends
           (lead_id, to_email, subject, body, status, provider, message_id,
            error, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (lead_id, to_email, subject, body, status, provider, message_id,
         error, now),
    )
    if status == SEND_SENT:
        conn.execute(
            "UPDATE leads SET status = ?, last_updated = ? "
            "WHERE id = ? AND status = ?",
            (STATUS_CONTACTED, now, lead_id, STATUS_NEW),
        )
    conn.commit()
    return cur.lastrowid


def already_sent(conn: sqlite3.Connection, lead_id: int) -> bool:
    """True if a *real* email (not a dry run) has already gone to this lead."""
    row = conn.execute(
        "SELECT 1 FROM sends WHERE lead_id = ? AND status = ? LIMIT 1",
        (lead_id, SEND_SENT),
    ).fetchone()
    return row is not None


def sends_today(conn: sqlite3.Connection) -> int:
    """Count of real emails sent since local midnight (for daily-cap checks)."""
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM sends WHERE status = ? AND created_at >= ?",
        (SEND_SENT, midnight),
    ).fetchone()
    return row["n"]


def list_sends(conn: sqlite3.Connection, limit: int = 200) -> List[dict]:
    rows = conn.execute(
        """SELECT s.*, l.name AS lead_name, l.city AS lead_city
           FROM sends s JOIN leads l ON l.id = s.lead_id
           ORDER BY s.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Suppression list (unsubscribes / bounces / do-not-contact)
# --------------------------------------------------------------------------- #

def add_suppression(conn: sqlite3.Connection, email: str, reason: str = "") -> None:
    email = (email or "").strip().lower()
    if not email:
        return
    conn.execute(
        "INSERT OR REPLACE INTO suppression (email, reason, created_at) "
        "VALUES (?,?,?)",
        (email, reason, time.time()),
    )
    # If this address matches a known lead, reflect it in the lead status too.
    conn.execute(
        "UPDATE leads SET status = ?, last_updated = ? WHERE lower(email) = ?",
        (STATUS_UNSUBSCRIBED, time.time(), email),
    )
    conn.commit()


def is_suppressed(conn: sqlite3.Connection, email: str) -> bool:
    email = (email or "").strip().lower()
    if not email:
        return False
    row = conn.execute(
        "SELECT 1 FROM suppression WHERE email = ? LIMIT 1", (email,)
    ).fetchone()
    return row is not None


def list_suppression(conn: sqlite3.Connection) -> List[dict]:
    rows = conn.execute(
        "SELECT * FROM suppression ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

def stats(conn: sqlite3.Connection) -> Dict[str, int]:
    total = conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"]
    with_email = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE COALESCE(email,'') != ''"
    ).fetchone()["n"]
    contacted = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE status = ?", (STATUS_CONTACTED,)
    ).fetchone()["n"]
    suppressed = conn.execute(
        "SELECT COUNT(*) AS n FROM suppression"
    ).fetchone()["n"]
    return {
        "total_leads": total,
        "with_email": with_email,
        "contacted": contacted,
        "suppressed": suppressed,
        "sent_today": sends_today(conn),
    }


def backup_bytes(conn: sqlite3.Connection) -> bytes:
    """Return a consistent snapshot of the whole database as bytes.

    Uses SQLite's online backup API into a temporary file so the snapshot is
    safe even if writes are in flight. Suitable for a one-click download.
    """
    import os
    import tempfile

    fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        dest = sqlite3.connect(tmp_path)
        with dest:
            conn.backup(dest)
        dest.close()
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def is_valid_sqlite(data: bytes) -> bool:
    """Cheap sanity check that uploaded bytes look like a SQLite database."""
    return data[:16] == b"SQLite format 3\x00"


if __name__ == "__main__":
    # Smoke test: create an in-memory DB, insert a lead, exercise the API.
    c = connect(":memory:")
    demo = {"name": "Tony's Trattoria", "city": "Vienna", "email": "hi@tonys.com",
            "family_score": 8, "family_signals": ["family owned"]}
    print("upsert:", upsert_leads(c, [demo, demo]))   # second is a dedupe update
    lead = list_leads(c)[0]
    print("lead:", lead["name"], lead["status"])
    record_send(c, lead["id"], lead["email"], "Hi", "Body", SEND_SENT, "smtp")
    print("already_sent:", already_sent(c, lead["id"]))
    print("stats:", stats(c))
