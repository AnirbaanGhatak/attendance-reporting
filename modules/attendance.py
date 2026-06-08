"""
modules/attendance.py
─────────────────────
Module 1 – Out-Office Attendance for Associates.

Flow:
  1. Associate selects their name and enters 3-digit PIN → verified against Supabase.
  2. On success, "Mark Attendance" page is shown.
  3. Browser Geolocation API fires (requires HTTPS in production).
  4. On button click: date = today, time = now (IST), coords from JS → saved to Supabase.
  5. Associates can check in/out multiple times per day (multiple trips).
  6. Payslip button opens a styled payslip page for any processed month.
"""

import base64
import calendar as _cal
import os
from datetime import datetime, date

import streamlit as st
from streamlit_js_eval import get_geolocation

from utils.auth import login_associate, logout
from utils.database import (
    get_associate_names,
    verify_associate,
    mark_attendance,
    mark_checkout,
    get_today_status,
    fetch_users,
    fetch_carry_forward,
    fetch_user_by_name,
    fetch_salary_ledger,
    fetch_salary_splits,
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry-point
# ─────────────────────────────────────────────────────────────────────────────

def show_attendance_module():
    if (
        not st.session_state.get("logged_in")
        or st.session_state.user_role not in ("article", "employee", "partner", "admin")
    ):
        _show_associate_login()
    else:
        _show_mark_attendance()


# ─────────────────────────────────────────────────────────────────────────────
# Login
# ─────────────────────────────────────────────────────────────────────────────

def _show_associate_login():
    st.title("👤 Login")
    st.caption("Mark your out-office attendance")
    st.markdown("---")

    names = get_associate_names()
    if not names:
        st.warning(
            "No users found in the system. "
            "Ask your admin to add users via the Admin panel."
        )
        return

    with st.form("associate_login_form"):
        name      = st.selectbox("Select Your Name", options=names)
        pin       = st.text_input(
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
                user = fetch_user_by_name(name)
                role = user["role"] if user else "employee"
                login_associate(name, role)
                st.success(f"Welcome, {name}!")
                st.rerun()
            else:
                st.error("Incorrect name or PIN. Please try again.")


# ─────────────────────────────────────────────────────────────────────────────
# Payslip
# ─────────────────────────────────────────────────────────────────────────────

def _show_payslip(name: str):
    """
    Payslip viewer for the logged-in employee.

    - Month selector capped at current month
    - Pulls salary_ledger + salary_splits from Supabase
    - Graceful "no data" message if month not processed
    - Firm logo from assets/logo.jpngpg (falls back to text if missing)
    - Print-to-PDF via browser Ctrl+P / ⌘P
    """
    st.markdown("---")
    st.subheader("🧾 Payslip")

    # ── Month selector ────────────────────────────────────────────────────────
    now = datetime.now()
    col1, col2 = st.columns(2)
    with col1:
        year_opts = list(range(2023, now.year + 1))
        sel_year  = st.selectbox(
            "Year",
            options=year_opts,
            index=len(year_opts) - 1,
            key="ps_year",
        )
    with col2:
        max_month  = now.month if sel_year == now.year else 12
        month_opts = list(range(1, max_month + 1))
        sel_month  = st.selectbox(
            "Month",
            options=month_opts,
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=len(month_opts) - 1,
            key="ps_month",
        )

    month_str   = f"{sel_year}-{sel_month:02d}"
    month_label = datetime(sel_year, sel_month, 1).strftime("%B %Y")

    # ── Fetch ledger data ─────────────────────────────────────────────────────
    ledger_df = fetch_salary_ledger(employee=name)

    if ledger_df.empty:
        st.info(f"No payslip records found for **{name}** yet.")
        return

    row_match = ledger_df[ledger_df["month"] == month_str]
    if row_match.empty:
        st.info(
            f"No payslip record found for **{month_label}**.  \n"
            "This month may not have been processed yet, or you may have selected "
            "a future month."
        )
        return

    row = row_match.iloc[0]

    # ── Fetch splits ──────────────────────────────────────────────────────────
    splits_df = fetch_salary_splits(employee=name, month=month_str)

    # ── Logo ──────────────────────────────────────────────────────────────────
    # Resolves to <repo_root>/assets/logo.png
    logo_path = os.path.join(
        os.path.dirname(__file__), "..", "assets", "logo.png"
    )
    logo_b64 = ""
    if os.path.exists(logo_path):
        with open(logo_path, "rb") as f:
            logo_b64 = base64.b64encode(f.read()).decode()

    logo_html = (
        f'<img src="data:image/jpeg;base64,{logo_b64}" '
        f'style="max-height:72px; object-fit:contain;" />'
        if logo_b64
        else '<div style="font-size:1.2rem;font-weight:700;color:#fff;">Maitra &amp; Chopra</div>'
    )

    # ── Values ────────────────────────────────────────────────────────────────
    base_salary = float(row.get("base_salary",  0) or 0)
    deduction   = float(row.get("deduction",    0) or 0)
    salary_paid = float(row.get("salary_paid",  0) or 0)
    bank        = str(row.get("bank",           "") or "")
    study_leave = bool(row.get("is_study_leave", False))

    study_badge = (
        '<span style="background:#fef3c7;color:#92400e;padding:3px 10px;'
        'border-radius:4px;font-size:0.78rem;font-weight:600;'
        'letter-spacing:0.03em;">STUDY LEAVE</span>'
        if study_leave else ""
    )

    deduction_color = "#dc2626" if deduction > 0 else "#374151"
    deduction_prefix = "− " if deduction > 0 else ""

    # ── Splits table HTML ─────────────────────────────────────────────────────
    splits_section_html = ""
    if not splits_df.empty:
        split_rows_html = ""
        for _, sp in splits_df.iterrows():
            r_name   = str(sp.get("recipient_name",   "") or "")
            r_amount = float(sp.get("recipient_amount", 0) or 0)
            r_bank   = str(sp.get("bank",              "") or "")
            split_rows_html += f"""
              <tr style="border-top:1px solid #f3f4f6;">
                <td style="padding:7px 10px;">{r_name}</td>
                <td style="padding:7px 10px;text-align:right;">₹{r_amount:,.2f}</td>
                <td style="padding:7px 10px;color:#6b7280;">{r_bank}</td>
              </tr>"""

        splits_section_html = f"""
          <!-- Payment Breakdown -->
          <tr>
            <td colspan="2" style="padding:18px 0 6px 0;">
              <span style="font-weight:600;font-size:0.9rem;
                           color:#374151;text-transform:uppercase;
                           letter-spacing:0.05em;">Payment Breakdown</span>
            </td>
          </tr>
          <tr>
            <td colspan="2" style="padding:0 0 4px 0;">
              <table style="width:100%;border-collapse:collapse;
                            border:1px solid #e5e7eb;border-radius:6px;
                            overflow:hidden;font-size:0.88rem;">
                <thead>
                  <tr style="background:#f9fafb;">
                    <th style="padding:7px 10px;text-align:left;
                                font-weight:600;color:#374151;">Recipient</th>
                    <th style="padding:7px 10px;text-align:right;
                                font-weight:600;color:#374151;">Amount</th>
                    <th style="padding:7px 10px;text-align:left;
                                font-weight:600;color:#374151;">Bank / Account</th>
                  </tr>
                </thead>
                <tbody>{split_rows_html}</tbody>
              </table>
            </td>
          </tr>"""

    # ── Full payslip HTML ─────────────────────────────────────────────────────
    payslip_html = f"""
    <div style="
        font-family:'Segoe UI',Arial,sans-serif;
        max-width:620px;
        margin:8px auto 24px auto;
        border:1px solid #d1d5db;
        border-radius:12px;
        overflow:hidden;
        box-shadow:0 2px 12px rgba(0,0,0,0.09);
    ">

      <!-- ── Header ───────────────────────────────────────────────── -->
      <div style="
          background:#1e3a5f;
          padding:20px 28px;
          display:flex;
          align-items:center;
          justify-content:space-between;
      ">
        <div>{logo_html}</div>
        <div style="text-align:right;">
          <div style="color:#94a3b8;font-size:0.72rem;letter-spacing:0.08em;
                      text-transform:uppercase;margin-bottom:3px;">Salary Payslip</div>
          <div style="color:#ffffff;font-size:1.05rem;font-weight:600;">
            {month_label}
          </div>
        </div>
      </div>

      <!-- ── Employee bar ─────────────────────────────────────────── -->
      <div style="
          background:#f8fafc;
          border-bottom:1px solid #e5e7eb;
          padding:14px 28px;
          display:flex;
          align-items:center;
          justify-content:space-between;
      ">
        <div>
          <div style="font-size:0.7rem;color:#9ca3af;text-transform:uppercase;
                      letter-spacing:0.07em;margin-bottom:3px;">Employee</div>
          <div style="font-weight:600;font-size:0.98rem;color:#111827;">{name}</div>
        </div>
        <div>{study_badge}</div>
      </div>

      <!-- ── Salary table ─────────────────────────────────────────── -->
      <div style="padding:20px 28px;">
        <table style="width:100%;border-collapse:collapse;font-size:0.93rem;">
          <colgroup>
            <col style="width:55%">
            <col style="width:45%">
          </colgroup>

          <!-- Base Salary -->
          <tr style="border-bottom:1px solid #f3f4f6;">
            <td style="padding:11px 0;color:#374151;">Base Salary</td>
            <td style="padding:11px 0;text-align:right;font-weight:500;color:#111827;">
              ₹{base_salary:,.2f}
            </td>
          </tr>

          <!-- Deduction -->
          <tr style="border-bottom:1px solid #f3f4f6;">
            <td style="padding:11px 0;color:#374151;">Deduction</td>
            <td style="padding:11px 0;text-align:right;font-weight:500;
                        color:{deduction_color};">
              {deduction_prefix}₹{deduction:,.2f}
            </td>
          </tr>

          <!-- Net Salary Paid -->
          <tr style="border-bottom:2px solid #1e3a5f;">
            <td style="padding:13px 0;font-weight:700;font-size:0.98rem;color:#111827;">
              Net Salary Paid
            </td>
            <td style="padding:13px 0;text-align:right;font-weight:700;
                        font-size:0.98rem;color:#1e3a5f;">
              ₹{salary_paid:,.2f}
            </td>
          </tr>

          <!-- Paid From -->
          <tr style="border-bottom:1px solid #f3f4f6;">
            <td style="padding:11px 0;color:#374151;">Paid From</td>
            <td style="padding:11px 0;text-align:right;color:#374151;">{bank}</td>
          </tr>

          {splits_section_html}

        </table>
      </div>

      <!-- ── Footer ───────────────────────────────────────────────── -->
      <div style="
          background:#f8fafc;
          border-top:1px solid #e5e7eb;
          padding:11px 28px;
          font-size:0.72rem;
          color:#9ca3af;
          text-align:center;
      ">
        System-generated payslip &nbsp;|&nbsp; For queries contact your admin
        &nbsp;|&nbsp; Generated {datetime.now().strftime('%d %b %Y')}
      </div>

    </div>

    <div style="text-align:center;font-size:0.78rem;color:#9ca3af;margin-bottom:8px;">
      Use your browser's <strong>Print</strong> (Ctrl+P / ⌘P) to save as PDF.
    </div>
    """

    st.markdown(payslip_html, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# Mark Attendance
# ─────────────────────────────────────────────────────────────────────────────

def _show_mark_attendance():
    name  = st.session_state.user_name
    today = date.today()

    st.title("📍 Attendance")
    st.markdown(f"**Name:** {name}")
    st.markdown(f"**Date:** {today.strftime('%A, %d %B %Y')}")

    # ── Leave balance ─────────────────────────────────────────────────────────
    try:
        current_month = today.strftime("%Y-%m")
        leave_balance = fetch_carry_forward(name, current_month)

        user = fetch_user_by_name(name)
        role = user["role"].strip().lower() if user else "employee"

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
        pass

    # ── Payslip ───────────────────────────────────────────────────────────────
    if "show_payslip" not in st.session_state:
        st.session_state["show_payslip"] = False

    if st.button("🧾 View Payslip", use_container_width=True):
        st.session_state["show_payslip"] = True
        st.rerun()

    if st.session_state.get("show_payslip"):
        _show_payslip(name)
        if st.button("← Back to Attendance", key="payslip_back"):
            st.session_state["show_payslip"] = False
            st.rerun()
        return  # don't render attendance UI while payslip is open

    st.markdown("---")

    # ── Today's status ────────────────────────────────────────────────────────
    status = get_today_status(name)
    state  = status["state"]

    if state == "checked_out":
        trips = status.get("trips", 0)
        st.success(
            f"✅ Last check-out recorded.  \n"
            f"**Company:** {status['company']}  \n"
            f"**In:** {status['in_time']}  |  **Out:** {status['out_time']}  \n"
            f"**Trips completed today:** {trips}"
        )
        st.info("Going somewhere else? You can check in again below.")

    # ── Location ──────────────────────────────────────────────────────────────
    if state == "none":
        st.info("📡 Fetching your location… Allow location access when prompted.")

    location = get_geolocation()
    lat, lon = None, None
    if location:
        try:
            lat = location["coords"]["latitude"]
            lon = location["coords"]["longitude"]
            acc = location["coords"].get("accuracy", "?")
            st.success(
                f"📌 Location captured — "
                f"Lat: `{lat:.5f}`, Lon: `{lon:.5f}` (±{acc:.0f} m)"
            )
        except (KeyError, TypeError):
            st.warning("Location data incomplete. Refresh and allow location access.")

    st.markdown("---")

    # ── Check In ──────────────────────────────────────────────────────────────
    if state in ("none", "checked_out"):
        trips = status.get("trips", 0)
        label = "Check In Again" if state == "checked_out" else "Check In"
        st.subheader(f"{'🔄' if state == 'checked_out' else '📍'} {label}")
        if trips > 0:
            st.caption(f"Trip {trips + 1} today")

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
            if st.button("Logout", use_container_width=True, key="logout_in"):
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

    # ── Check Out ─────────────────────────────────────────────────────────────
    elif state == "checked_in":
        trips = status.get("trips", 0)
        st.info(
            f"**Checked in** at **{status['in_time']}**  \n"
            f"**Company:** {status['company']}  \n"
            f"**Trips completed today:** {trips}"
        )
        st.subheader("Check Out")
        st.caption("Your current location will be recorded as checkout location.")

        col1, col2 = st.columns([3, 1])
        with col1:
            check_out_btn = st.button(
                "🔴 Check Out",
                use_container_width=True,
                type="primary",
            )
        with col2:
            if st.button("Logout", use_container_width=True, key="logout_out"):
                logout()
                st.rerun()

        if check_out_btn:
            if lat is None or lon is None:
                st.error("Cannot check out without a valid location.")
            else:
                with st.spinner("Saving…"):
                    result = mark_checkout(name, lat, lon)
                if result["success"]:
                    st.success(result["message"])
                    st.rerun()
                else:
                    st.error(result["message"])
