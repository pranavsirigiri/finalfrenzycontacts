#!/usr/bin/env python3
"""
app.py — Streamlit dashboard for the family-restaurant outreach tool.

    streamlit run app.py

Flow, top to bottom via tabs:
  1. Find leads   — run discovery (OSM / Google / demo) into the local DB.
  2. Review & send — filter leads, edit the email template, preview, then send.
  3. Sent & suppression — audit log of every email; manage unsubscribes.

Sending is DRY-RUN by default. A real send requires configuring a provider in
the sidebar AND ticking the confirmation box. Nothing goes out by accident.
"""

from __future__ import annotations

import argparse

import pandas as pd
import streamlit as st

import db
import sender
import find_family_restaurants as ffr

st.set_page_config(page_title="Restaurant Outreach", page_icon="🍝",
                   layout="wide")


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #

@st.cache_resource
def get_conn():
    # One connection reused across reruns; Streamlit may switch threads.
    return db.connect(db.DEFAULT_DB_PATH, check_same_thread=False)


conn = get_conn()

st.session_state.setdefault("subject_tmpl", sender.DEFAULT_SUBJECT)
st.session_state.setdefault("body_tmpl", sender.DEFAULT_BODY)


# --------------------------------------------------------------------------- #
# Sidebar — sender identity + provider (used only when sending for real)
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.header("Sender identity")
    st.caption("Used for the From header and the required CAN-SPAM footer.")
    from_name = st.text_input("Your name / business", "Frenzy Outreach")
    from_email = st.text_input("From email", "you@yourdomain.com")
    physical_address = st.text_input(
        "Physical mailing address", "123 Main St, Reston, VA 20190")
    unsubscribe_url = st.text_input("Unsubscribe URL (optional)", "")
    unsubscribe_mailto = st.text_input("Unsubscribe email (optional)", "")

    identity = sender.SenderIdentity(
        from_name=from_name, from_email=from_email,
        physical_address=physical_address,
        unsubscribe_url=unsubscribe_url,
        unsubscribe_mailto=unsubscribe_mailto)

    st.divider()
    st.header("Sending")
    provider_choice = st.radio(
        "Provider", ["Dry run (no email sent)", "SMTP"], index=0)
    daily_cap = st.number_input("Daily cap (real sends)", 1, 2000, 50)
    throttle = st.number_input("Seconds between sends", 0.0, 120.0, 5.0, step=1.0)

    smtp_cfg = {}
    if provider_choice == "SMTP":
        st.caption("Gmail: host smtp.gmail.com, port 587, an App Password.")
        smtp_cfg["host"] = st.text_input("SMTP host", "smtp.gmail.com")
        smtp_cfg["port"] = st.number_input("SMTP port", 1, 65535, 587)
        smtp_cfg["username"] = st.text_input("SMTP username", from_email)
        smtp_cfg["password"] = st.text_input("SMTP password", type="password")

    st.divider()
    s = db.stats(conn)
    st.metric("Leads", s["total_leads"])
    st.metric("With email", s["with_email"])
    st.metric("Contacted", s["contacted"])
    st.metric("Sent today", s["sent_today"])


def build_provider():
    if provider_choice == "SMTP":
        return sender.SMTPProvider(
            host=smtp_cfg["host"], port=int(smtp_cfg["port"]),
            username=smtp_cfg["username"], password=smtp_cfg["password"],
            from_email=from_email, from_name=from_name)
    return sender.DryRunProvider()


tab_find, tab_send, tab_log = st.tabs(
    ["1 · Find leads", "2 · Review & send", "3 · Sent & suppression"])


# --------------------------------------------------------------------------- #
# Tab 1 — Find leads
# --------------------------------------------------------------------------- #

with tab_find:
    st.subheader("Discover restaurants")
    col1, col2 = st.columns(2)
    with col1:
        source = st.selectbox(
            "Source",
            ["osm (free, no key)", "google (needs API key)", "demo (sample data)"])
        cities_text = st.text_area(
            "Cities (one per line)", "\n".join(ffr.DEFAULT_CITIES[:6]), height=160)
    with col2:
        max_per_city = st.number_input("Max per city", 1, 60, 20)
        chain_threshold = st.number_input(
            "Chain threshold (locations)", 2, 10, 3)
        require_family = st.checkbox("Require a family-owned signal", False)
        no_website = st.checkbox(
            "Skip website scraping (faster, no email/score)", False)
        api_key = ""
        if source.startswith("google"):
            api_key = st.text_input("Google Places API key", type="password")

    if source.startswith("osm") and not no_website:
        st.info("OSM discovery + website scraping can take a few minutes for "
                "many cities. Uncheck cities or tick 'Skip website scraping' "
                "for a quick first pass.")

    if st.button("Run search", type="primary"):
        cities = [c.strip() for c in cities_text.replace(",", "\n").splitlines()
                  if c.strip()]
        args = argparse.Namespace(
            source="google" if source.startswith("google") else "osm",
            api_key=api_key or None, cities=cities,
            max_per_city=int(max_per_city), chain_threshold=int(chain_threshold),
            require_family_signal=require_family, no_website_check=no_website)
        try:
            with st.spinner("Searching… this may take a while for OSM."):
                if source.startswith("demo"):
                    leads = ffr.build_demo_leads(args)
                else:
                    leads = ffr.gather(args)
                added, updated = db.upsert_leads(conn, leads)
            st.success(f"Done. {len(leads)} found · {added} new · "
                       f"{updated} updated in the database.")
        except Exception as e:  # noqa: BLE001
            st.error(f"Search failed: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# Tab 2 — Review & send
# --------------------------------------------------------------------------- #

with tab_send:
    st.subheader("Review leads and compose")

    fcol1, fcol2, fcol3 = st.columns(3)
    status_filter = fcol1.selectbox(
        "Status", ["new", "any", "contacted", "unsubscribed"])
    email_only = fcol2.checkbox("Only leads with an email", True)
    fcol3.write("")

    leads = db.list_leads(
        conn,
        status=None if status_filter == "any" else status_filter,
        has_email=True if email_only else None)

    st.markdown("**Email template** — placeholders: `{name}` `{city}` "
                "`{family_signal}` `{website}` `{phone}` `{address}`")
    st.session_state.subject_tmpl = st.text_input(
        "Subject", st.session_state.subject_tmpl)
    st.session_state.body_tmpl = st.text_area(
        "Body", st.session_state.body_tmpl, height=200)

    if not leads:
        st.warning("No leads match. Run a search in tab 1 first.")
    else:
        # Live preview against the first matching lead.
        with st.expander("Preview (first lead)", expanded=True):
            subj, body = sender.render_email(
                st.session_state.subject_tmpl, st.session_state.body_tmpl,
                leads[0], identity)
            st.text(f"To: {leads[0].get('email','')}\nSubject: {subj}\n\n{body}")

        # Download the full matching set as a spreadsheet (CSV opens in Excel /
        # Numbers / Google Sheets).
        export_df = pd.DataFrame([{
            "name": l["name"], "city": l["city"], "phone": l["phone"],
            "email": l["email"], "website": l["website"],
            "address": l["address"], "family_score": l["family_score"],
            "family_signals": "; ".join(l.get("family_signals") or []),
            "rating": l["rating"], "review_count": l["review_count"],
            "status": l["status"], "source": l["source"],
        } for l in leads])
        st.download_button(
            "⬇️ Download these leads as a spreadsheet (CSV)",
            data=export_df.to_csv(index=False).encode("utf-8"),
            file_name="restaurant_leads.csv", mime="text/csv")

        # Selectable table.
        df = pd.DataFrame([{
            "send": True,
            "id": l["id"],
            "name": l["name"],
            "city": l["city"],
            "email": l["email"],
            "family_score": l["family_score"],
            "status": l["status"],
        } for l in leads])
        edited = st.data_editor(
            df, hide_index=True, width="stretch",
            disabled=["id", "name", "city", "email", "family_score", "status"],
            column_config={"send": st.column_config.CheckboxColumn("Send?")})
        selected_ids = set(edited[edited["send"]]["id"].tolist())
        chosen = [l for l in leads if l["id"] in selected_ids]
        st.caption(f"{len(chosen)} selected.")

        c1, c2 = st.columns(2)

        if c1.button("Dry-run preview selected"):
            results = sender.send_campaign(
                conn, chosen, st.session_state.subject_tmpl,
                st.session_state.body_tmpl, identity, dry_run=True)
            for r in results:
                icon = "✅" if r.status == "dry_run" else "⏭️"
                with st.expander(f"{icon} {r.name} — {r.status}"
                                 f"{' · ' + r.reason if r.reason else ''}"):
                    if r.subject:
                        st.text(f"To: {r.to_email}\nSubject: {r.subject}\n\n"
                                f"{r.body}")

        with c2:
            confirm = st.checkbox("I confirm I want to really send")
            live = provider_choice != "Dry run (no email sent)"
            if st.button("Send for real", type="primary",
                         disabled=not (confirm and live and chosen)):
                prog = st.progress(0.0)
                results = sender.send_campaign(
                    conn, chosen, st.session_state.subject_tmpl,
                    st.session_state.body_tmpl, identity,
                    provider=build_provider(), dry_run=False,
                    daily_cap=int(daily_cap), throttle_seconds=float(throttle),
                    progress=lambda i, n, r: prog.progress(i / n))
                sent = sum(1 for r in results if r.status == "sent")
                failed = sum(1 for r in results if r.status == "failed")
                skipped = sum(1 for r in results if r.status == "skipped")
                st.success(f"Sent {sent} · failed {failed} · skipped {skipped}")
                for r in results:
                    if r.status != "sent":
                        st.write(f"⏭️ {r.name}: {r.status} — {r.reason}")
            if not live:
                st.caption("Provider is 'Dry run' — switch to SMTP in the "
                           "sidebar to enable real sending.")


# --------------------------------------------------------------------------- #
# Tab 3 — Sent log & suppression
# --------------------------------------------------------------------------- #

with tab_log:
    st.subheader("Sent log")
    sends = db.list_sends(conn)
    if sends:
        st.dataframe(pd.DataFrame([{
            "when": pd.to_datetime(s["created_at"], unit="s"),
            "lead": s["lead_name"], "city": s["lead_city"],
            "to": s["to_email"], "status": s["status"],
            "provider": s["provider"], "error": s["error"] or "",
        } for s in sends]), hide_index=True, width="stretch")
    else:
        st.caption("No emails logged yet.")

    st.divider()
    st.subheader("Suppression list (do-not-contact)")
    with st.form("suppress"):
        sup_email = st.text_input("Add email to suppress")
        sup_reason = st.text_input("Reason", "unsubscribe request")
        if st.form_submit_button("Suppress"):
            db.add_suppression(conn, sup_email, sup_reason)
            st.success(f"Suppressed {sup_email}")

    sup = db.list_suppression(conn)
    if sup:
        st.dataframe(pd.DataFrame([{
            "email": r["email"], "reason": r["reason"],
            "when": pd.to_datetime(r["created_at"], unit="s"),
        } for r in sup]), hide_index=True, width="stretch")
    else:
        st.caption("No suppressed addresses.")
