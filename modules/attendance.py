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
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry-point called by app.py
# ─────────────────────────────────────────────────────────────────────────────

def show_attendance_module():
    """Renders the full attendance module (login + mark-attendance screen)."""
    if not st.session_state.get("logged_in") or st.session_state.user_role != "associate":
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

    name = st.session_state.user_name
    today = datetime.date.today()

    st.title("📍 Mark Attendance")
    st.markdown(f"**Associate:** {name}")
    st.markdown(f"**Date:** {today.strftime('%A, %d %B %Y')}")
    st.markdown("---")

    # ── Location capture via browser Geolocation API ──────────────────────────
    st.info("📡 Fetching your location… Please allow location access when prompted.")

    location = get_geolocation()   # streamlit-js-eval call

    lat, lon = None, None
    if location:
        try:
            lat = location["coords"]["latitude"]
            lon = location["coords"]["longitude"]
            acc = location["coords"].get("accuracy", "?")
            st.success(f"📌 Location captured — Lat: `{lat:.5f}`, Lon: `{lon:.5f}` (±{acc:.0f} m)")
        except (KeyError, TypeError):
            st.warning("Location data incomplete. Please refresh and allow location access.")
    else:
        st.warning("Waiting for location… (ensure your browser allows location for this site)")

    st.markdown("---")

    col1, col2 = st.columns([3, 1])
    with col1:
        mark_btn = st.button(
            "✅ Mark Attendance",
            disabled=(lat is None or lon is None),
            use_container_width=True,
            type="primary",
        )
    with col2:
        if st.button("Logout", use_container_width=True):
            logout()
            st.rerun()

    if mark_btn:
        if lat is None or lon is None:
            st.error("Cannot mark attendance without a valid location.")
            return

        with st.spinner("Saving attendance…"):
            result = mark_attendance(name, lat, lon)

        if result["success"]:
            st.balloons()
            st.success(result["message"])
        else:
            st.error(result["message"])
