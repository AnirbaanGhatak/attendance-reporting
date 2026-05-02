"""
modules/attendance.py
─────────────────────
Module 1 – Out-Office Attendance for Associates.

Flow:
  1. Associate selects their name and enters 3-digit PIN → verified against GSheets.
  2. On success, "Mark Attendance" page is shown.
  3. Browser Geolocation API fires (requires HTTPS in production).
  4. On button click: date = today, time = now, coords from JS → appended to GSheets.
"""

import streamlit as st
from streamlit_js_eval import get_geolocation

from utils.auth import login_associate, logout, verify_admin
from utils.gsheets import (
    get_associate_names,
    verify_associate,
    mark_attendance,
    mark_checkout,
    get_today_status,
    fetch_users,
    fetch_carry_forward
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry-point called by app.py
# ─────────────────────────────────────────────────────────────────────────────

def show_attendance_module():
    """Renders the full attendance module (login + mark-attendance screen)."""
    if not st.session_state.get("logged_in") or st.session_state.user_role not in ["article", "employee"]:
        _show_associate_login()
    else:
        _show_mark_attendance()


# ─────────────────────────────────────────────────────────────────────────────
# Associate Login
# ─────────────────────────────────────────────────────────────────────────────

def _show_associate_login():
    st.title("👤 Associate Login")
    st.caption("Mark your out-office attendance")
    st.markdown("---")

    names = get_associate_names()
    if not names:
        st.warning(
            "No associates found in the system. "
            "Ask your admin to add users via the Admin panel."
        )
        return

    with st.form("associate_login_form"):
        name = st.selectbox("Select Your Name", options=names)
        pin  = st.text_input(
            "Enter 3-Digit PIN",
            type="password",
            max_chars=3,
            placeholder="• • •",
        )
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        if len(pin) != 3 or not pin.isdigit():
            st.error("PIN must be exactly 3 digits.")
            return

        with st.spinner("Verifying…"):
            if verify_associate(name, pin):
                login_associate(name)
                st.success(f"Welcome, {name}!")
                st.rerun()
            else:
                st.error("Incorrect name or PIN. Please try again.")


# ─────────────────────────────────────────────────────────────────────────────
# Mark Attendance Screen
# ─────────────────────────────────────────────────────────────────────────────

def _show_mark_attendance():
    import datetime

    name  = st.session_state.user_name
    today = datetime.date.today()

    st.title("📍 Attendance")
    st.markdown(f"**Associate:** {name}")
    st.markdown(f"**Date:** {today.strftime('%A, %d %B %Y')}")

    try:
        current_month = today.strftime("%Y-%m")
        leave_balance = fetch_carry_forward(name, current_month)
        # current month's accrual isn't saved yet so add 1.5 as preview
        try:
            users    = fetch_users()
            user_row = users[users["name"].str.strip().str.lower() == name.strip().lower()]
            role     = user_row.iloc[0]["role"].strip().lower() if not user_row.empty else "employee"
        except Exception:
            role = "employee"

        if role == "article":
            st.info(
                f"🗓️ **Leave Balance**  \n"
                f"Holidays remaining: **{leave_balance:.1f} days** *(out of 24)*"
            )
        else:
            projected = leave_balance + 1.5
            st.info(
                f"🗓️ **Leave Balance**  \n"
                f"Carried forward: **{leave_balance:.1f} days**  \n"
                f"After this month's CL/PL: **{projected:.1f} days** *(estimated)*"
            )
    except Exception:
        pass   # silently skip if ledger unavailable


    st.markdown("---")

    # Check today's status first
    status = get_today_status(name)
    state  = status["state"]

    if state == "checked_out":
        st.success(
            f"✅ Attendance complete for today.\n\n"
            f"**Company:** {status['company']}  \n"
            f"**In:** {status['in_time']}  |  **Out:** {status['out_time']}"
        )
        if st.button("Logout"):
            logout()
            st.rerun()
        return

    # Location (needed for check-in only)
    if state == "none":
        st.info("📡 Fetching your location… Allow location access when prompted.")
    location = get_geolocation()
    lat, lon = None, None
    if location:
        try:
            lat = location["coords"]["latitude"]
            lon = location["coords"]["longitude"]
            acc = location["coords"].get("accuracy", "?")
            st.success(f"📌 Location captured — Lat: `{lat:.5f}`, Lon: `{lon:.5f}` (±{acc:.0f} m)")
        except (KeyError, TypeError):
            st.warning("Location data incomplete. Refresh and allow location access.")

    st.markdown("---")

    # ── CHECK IN ──────────────────────────────────────────────────────────────
    if state == "none":
        st.subheader("Check In")
        company = st.text_input(
            "Company / Client Office",
            placeholder="e.g. ABC Pvt. Ltd., Andheri",
        )

        col1, col2 = st.columns([3, 1])
        with col1:
            check_in_btn = st.button(
                "🟢 Check In",
                disabled=(lat is None or lon is None or not company.strip()),
                use_container_width=True,
                type="primary",
            )
        with col2:
            if st.button("Logout", use_container_width=True):
                logout()
                st.rerun()

        if not company.strip():
            st.caption("Enter the company name to enable Check In.")

        if check_in_btn:
            with st.spinner("Saving…"):
                result = mark_attendance(name, company, lat, lon)
            if result["success"]:
                st.success(result["message"])
                st.rerun()
            else:
                st.error(result["message"])

    # ── CHECK OUT ─────────────────────────────────────────────────────────────
    elif state == "checked_in":
        st.info(
            f"**Checked in** at **{status['in_time']}**  \n"
            f"**Company:** {status['company']}"
        )
        st.subheader("Check Out")

        col1, col2 = st.columns([3, 1])
        with col1:
            check_out_btn = st.button(
                "🔴 Check Out",
                use_container_width=True,
                type="primary",
            )
        with col2:
            if st.button("Logout", use_container_width=True):
                logout()
                st.rerun()

        if check_out_btn:
            with st.spinner("Saving…"):
                result = mark_checkout(name)
            if result["success"]:
                st.success(result["message"])
                st.rerun()
            else:
                st.error(result["message"])