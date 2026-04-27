"""
modules/admin.py
────────────────
Module 2 – Admin Dashboard

Phase A  Attendance Consolidation
  Upload one or more in-office XLS files (one per employee, exported from your
  existing attendance system) + pull out-office records from GSheets.
  Merge them into a final monthly report → download as CSV.

Phase B  Salary Calculation & Splitting
  Select employee → base salary → bank → dynamic split rows → save to GSheet

─────────────────────────────────────────────────────────────────────────────
ACTUAL XLS FILE FORMAT (reverse-engineered from the uploaded sample):
─────────────────────────────────────────────────────────────────────────────
  Sheet: DailyAttendance_SummaryReport   (single sheet, no real column headers)

  Row index 0   : blank
  Row index 1   : title  "Daily Attendance Report (Summary Report)"
  Row index 2   : blank
  Row index 3   : date range  e.g. "Apr 01 2026  To  Apr 30 2026"
  Row index 4   : Company info
  Row index 5-6 : blank
  Row index 7   : Department row
  Row index 8   : "Employee Code: ... Employee Name: APARNA PRADYOT MAITRA"
                    col[3] = employee code,  col[7] = employee name
  Row index 9   : column labels (Date / InTime / OutTime / Shift / Total Duration / Status / Remarks)
  Row index 10+ : daily data rows
  Last row      : summary string e.g. "Total Duration=83 Hrs 6 Min , PresentDays=16 ..."

  Useful columns in data rows (0-indexed):
    col 1  -> Date           e.g. "01-Apr-2026"
    col 3  -> InTime         e.g. "11:54"
    col 4  -> OutTime        e.g. "17:21"
    col 6  -> Shift          e.g. "GS"
    col 7  -> Total Duration e.g. "5:27"
    col 8  -> Status         Present | halfPresent | Absent | WeeklyOff |
                             Absent (No OutPunch)
    col 10 -> Remarks

  Status -> Days_Count mapping:
    Present                -> 1.0
    halfPresent (with ½)   -> 0.5
    Absent                 -> 0.0
    WeeklyOff              -> 0.0
    Absent (No OutPunch)   -> 0.0
"""

from __future__ import annotations

import io
from datetime import datetime

import pandas as pd
import streamlit as st

from utils.auth import logout, verify_admin
from utils.gsheets import (
    fetch_out_office_attendance,
    save_salary_record,
    fetch_all_employee_names,
    add_user,
    fetch_users,
)


# ─────────────────────────────────────────────────────────────────────────────
# XLS Parser — understands the actual file format
# ─────────────────────────────────────────────────────────────────────────────

def _parse_attendance_xls(file_obj) -> pd.DataFrame | None:
    """
    Parse one XLS file exported from the existing attendance system.

    Returns a DataFrame with columns:
        Employee_Name | Date (YYYY-MM-DD) | InTime | OutTime | Shift |
        Total_Duration | Status | Days_Count | Source

    Returns None and calls st.error() on failure.
    """
    try:
        raw = pd.read_excel(file_obj, sheet_name=0, header=None, engine="xlrd")
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None

    # ── Extract employee name from row 8, column 7 ────────────────────────────
    employee_name = ""
    try:
        val = str(raw.iloc[8, 7]).strip()
        if val and val.lower() != "nan":
            employee_name = val
    except Exception:
        pass

    if not employee_name:
        # Fallback: scan row 8 for any non-null value after column 6
        try:
            for col_idx in range(7, raw.shape[1]):
                val = str(raw.iloc[8, col_idx]).strip()
                if val and val.lower() != "nan":
                    employee_name = val
                    break
        except Exception:
            pass

    if not employee_name:
        st.error("Could not find the employee name in this file (expected row 9, col 8).")
        return None

    # ── Locate data rows ──────────────────────────────────────────────────────
    # Row 9 = column-label row, Row 10 onwards = data, last row = summary text
    data_start = 10
    data_end   = len(raw) - 1     # drop summary row

    if data_end <= data_start:
        st.error(f"Not enough data rows in file for {employee_name}.")
        return None

    data = raw.iloc[data_start:data_end].reset_index(drop=True)

    # ── Map positional columns to named columns ───────────────────────────────
    col_map = {
        1: "Date",
        3: "InTime",
        4: "OutTime",
        6: "Shift",
        7: "Total_Duration",
        8: "Status",
    }
    df = pd.DataFrame()
    for pos, name in col_map.items():
        if pos < data.shape[1]:
            df[name] = data.iloc[:, pos].astype(str).str.strip()
        else:
            df[name] = ""

    # ── Keep only rows where Date looks like "DD-Mon-YYYY" ────────────────────
    df = df[df["Date"].str.match(r"^\d{2}-[A-Za-z]{3}-\d{4}$", na=False)].copy()

    if df.empty:
        st.error(f"No valid date rows found for {employee_name}.")
        return None

    # ── Parse dates to YYYY-MM-DD ─────────────────────────────────────────────
    df["Date"] = (
        pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce")
        .dt.strftime("%Y-%m-%d")
    )
    df = df.dropna(subset=["Date"])

    # ── Days_Count from Status ─────────────────────────────────────────────────
    def _days_count(status: str) -> float:
        s = status.strip().lower()
        if s == "present":
            return 1.0
        # handles "½present", "1/2present", "halfpresent", etc.
        if "\u00bdpresent" in s or "half" in s or "1/2" in s:
            return 0.5
        return 0.0   # Absent, WeeklyOff, Absent (No OutPunch)

    df["Days_Count"]    = df["Status"].apply(_days_count)
    df["Employee_Name"] = employee_name
    df["Source"]        = "In-Office"

    return df[[
        "Employee_Name", "Date", "InTime", "OutTime",
        "Shift", "Total_Duration", "Status", "Days_Count", "Source",
    ]]


# ─────────────────────────────────────────────────────────────────────────────
# Public entry-point
# ─────────────────────────────────────────────────────────────────────────────

def show_admin_module():
    if not st.session_state.get("logged_in") or st.session_state.user_role != "admin":
        _show_admin_login()
        return

    st.sidebar.title("🔧 Admin Panel")
    st.sidebar.markdown(f"Logged in as **{st.session_state.user_name}**")
    page = st.sidebar.radio(
        "Go to",
        ["📊 Attendance Report", "💰 Salary Processing", "👥 Manage Users"],
    )
    if st.sidebar.button("Logout"):
        logout()
        st.rerun()
    st.sidebar.markdown("---")

    if page == "📊 Attendance Report":
        _show_attendance_consolidation()
    elif page == "💰 Salary Processing":
        _show_salary_processing()
    elif page == "👥 Manage Users":
        _show_user_management()


# ─────────────────────────────────────────────────────────────────────────────
# Admin Login
# ─────────────────────────────────────────────────────────────────────────────

def _show_admin_login():
    st.title("🔐 Admin Login")
    st.markdown("---")

    with st.form("admin_login_form"):
        username  = st.text_input("Username")
        password  = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Login", use_container_width=True)

    if submitted:
        if verify_admin(username, password):
            st.session_state.logged_in = True
            st.session_state.user_role = "admin"
            st.session_state.user_name = username
            st.success("Login successful!")
            st.rerun()
        else:
            st.error("Invalid credentials.")


# ─────────────────────────────────────────────────────────────────────────────
# Phase A – Attendance Consolidation
# ─────────────────────────────────────────────────────────────────────────────

def _show_attendance_consolidation():
    st.title("📊 Monthly Attendance Report")
    st.caption(
        "Upload one .xls file per employee (Daily Attendance Summary Report "
        "exported from your attendance machine). "
        "The app reads the employee name from inside each file automatically."
    )
    st.markdown("---")

    # ── Month selector ────────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        year_opts = list(range(2024, datetime.now().year + 2))
        year = st.selectbox(
            "Year", options=year_opts,
            index=year_opts.index(datetime.now().year)
        )
    with col2:
        month_num = st.selectbox(
            "Month",
            options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1,
        )
    month_str  = f"{year}-{month_num:02d}"
    month_label = datetime(year, month_num, 1).strftime("%B %Y")
    st.markdown("---")

    # ── Multi-file upload ─────────────────────────────────────────────────────
    st.subheader("Step 1 — Upload Attendance Files")
    uploaded_files = st.file_uploader(
        "Upload one XLS file per employee",
        type=["xls"],
        accept_multiple_files=True,
        label_visibility="collapsed",
    )

    in_office_frames: list[pd.DataFrame] = []

    if uploaded_files:
        st.write(f"**{len(uploaded_files)} file(s) uploaded — parsing…**")

        for uf in uploaded_files:
            parsed = _parse_attendance_xls(uf)
            if parsed is None:
                continue   # error already shown by _parse_attendance_xls

            month_rows = parsed[parsed["Date"].str.startswith(month_str, na=False)]

            if month_rows.empty:
                st.warning(
                    f"⚠️  `{uf.name}` — no rows found for {month_label}. "
                    f"Check that you selected the correct month above."
                )
                continue

            emp_name     = month_rows["Employee_Name"].iloc[0]
            present_days = month_rows["Days_Count"].sum()
            st.success(
                f"✅  `{uf.name}` → **{emp_name}**  |  "
                f"{len(month_rows)} calendar days  |  "
                f"**{present_days:.1f} working days**"
            )
            in_office_frames.append(month_rows)

    in_office_df = (
        pd.concat(in_office_frames, ignore_index=True)
        if in_office_frames else pd.DataFrame()
    )

    # ── Generate report ───────────────────────────────────────────────────────
    st.markdown("---")
    st.subheader("Step 2 — Merge with Out-Office Records")
    st.caption(
        "Clicking the button below fetches out-office attendance from Google Sheets "
        "and combines it with the files uploaded above."
    )

    if st.button("🔄 Generate Report", type="primary", use_container_width=True):
        with st.spinner("Fetching out-office data from Google Sheets…"):
            out_df = fetch_out_office_attendance(month=month_str)

        if out_df.empty and in_office_df.empty:
            st.warning("No data found for the selected month in either source.")
            return

        # ── Shape out-office data to match in-office columns ──────────────────
        out_df_proc = pd.DataFrame()
        if not out_df.empty:
            out_df_proc = pd.DataFrame({
                "Employee_Name" : out_df["Name"],
                "Date"          : out_df["Date"],
                "InTime"        : out_df.get("Time", ""),
                "OutTime"       : "",
                "Shift"         : "Out-Office",
                "Total_Duration": "",
                "Status"        : "Present (Out-Office)",
                "Days_Count"    : 1.0,
                "Source"        : "Out-Office",
            })

        # ── Combine & sort ─────────────────────────────────────────────────────
        frames  = [f for f in [in_office_df, out_df_proc] if not f.empty]
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values(["Employee_Name", "Date"]).reset_index(drop=True)

        # ── Per-employee summary ───────────────────────────────────────────────
        summary_rows = []
        for emp, grp in combined.groupby("Employee_Name"):
            present    = int((grp["Days_Count"] == 1.0).sum())
            half       = int((grp["Days_Count"] == 0.5).sum())
            weekly_off = int(grp["Status"].str.lower().str.contains("weeklyoff", na=False).sum())
            absent     = int((grp["Days_Count"] == 0.0).sum()) - weekly_off
            total      = grp["Days_Count"].sum()
            summary_rows.append({
                "Employee"          : emp,
                "Present Days"      : present,
                "Half Days"         : half,
                "Absent Days"       : max(absent, 0),
                "Weekly Off"        : weekly_off,
                "Total Working Days": total,
            })
        summary = pd.DataFrame(summary_rows)

        # ── Show ──────────────────────────────────────────────────────────────
        st.markdown(f"#### Detail — {month_label}")
        st.dataframe(combined, use_container_width=True)

        st.markdown("#### Summary")
        st.dataframe(summary, use_container_width=True)

        # ── Download buttons ──────────────────────────────────────────────────
        col_a, col_b = st.columns(2)
        with col_a:
            buf1 = io.StringIO()
            combined.to_csv(buf1, index=False)
            st.download_button(
                "⬇️ Full Report (CSV)",
                data=buf1.getvalue(),
                file_name=f"attendance_detail_{month_str}.csv",
                mime="text/csv",
                use_container_width=True,
            )
        with col_b:
            buf2 = io.StringIO()
            summary.to_csv(buf2, index=False)
            st.download_button(
                "⬇️ Summary (CSV)",
                data=buf2.getvalue(),
                file_name=f"attendance_summary_{month_str}.csv",
                mime="text/csv",
                use_container_width=True,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Phase B – Salary Calculation & Splitting
# ─────────────────────────────────────────────────────────────────────────────

def _show_salary_processing():
    st.title("💰 Salary Processing")
    st.markdown("---")

    if "split_count" not in st.session_state:
        st.session_state.split_count = 0

    # ── Employee / month ──────────────────────────────────────────────────────
    employee_names = fetch_all_employee_names()
    if not employee_names:
        st.warning("No associates found. Add them via Manage Users first.")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        employee = st.selectbox("Employee", options=employee_names)
    with col2:
        year_opts = list(range(2024, datetime.now().year + 2))
        sal_year  = st.selectbox(
            "Year", options=year_opts,
            index=year_opts.index(datetime.now().year),
            key="sal_year",
        )
    with col3:
        sal_month = st.selectbox(
            "Month",
            options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1,
            key="sal_month",
        )
    month_str = f"{sal_year}-{sal_month:02d}"

    # ── Salary + bank ─────────────────────────────────────────────────────────
    col4, col5 = st.columns(2)
    with col4:
        base_salary = st.number_input(
            "Base Salary (₹)", min_value=0.0, step=500.0, value=0.0, format="%.2f"
        )
    with col5:
        try:
            bank_options = list(st.secrets["banks"]["accounts"])
        except Exception:
            bank_options = ["Bank Account 1", "Bank Account 2"]
        bank = st.selectbox("Pay From (Bank Account)", options=bank_options)

    st.markdown("---")

    # ── Split mechanism ───────────────────────────────────────────────────────
    st.subheader("Salary Split")
    st.caption(
        "Press **Split** to add a recipient row — you can press it as many times as needed. "
        "Leave this section empty to save the full amount to the employee directly."
    )

    btn1, btn2 = st.columns([1, 1])
    with btn1:
        if st.button("➕ Split", use_container_width=True):
            st.session_state.split_count += 1
    with btn2:
        if st.session_state.split_count > 0:
            if st.button("🗑️ Clear All Splits", use_container_width=True):
                st.session_state.split_count = 0
                st.rerun()

    # ── Render split rows ─────────────────────────────────────────────────────
    splits: list[dict] = []
    running_total = 0.0

    if st.session_state.split_count > 0:
        hc = st.columns([3, 2])
        hc[0].markdown("**Recipient Name**")
        hc[1].markdown("**Amount (₹)**")

        for i in range(st.session_state.split_count):
            rc = st.columns([3, 2])
            with rc[0]:
                rec_name = st.text_input(
                    f"name_{i}", key=f"split_name_{i}",
                    label_visibility="collapsed",
                    placeholder=f"Recipient {i + 1}",
                )
            with rc[1]:
                rec_amount = st.number_input(
                    f"amt_{i}", key=f"split_amount_{i}",
                    label_visibility="collapsed",
                    min_value=0.0, step=100.0, format="%.2f",
                )
            splits.append({"name": rec_name, "amount": rec_amount})
            running_total += rec_amount

        # Balance indicator
        if base_salary > 0:
            diff = base_salary - running_total
            if abs(diff) < 0.01:
                st.success(f"✅ Split total ₹{running_total:,.2f} matches base salary.")
            elif diff > 0:
                st.warning(f"⚠️ Unallocated: ₹{diff:,.2f}")
            else:
                st.error(f"❌ Over-allocated by ₹{abs(diff):,.2f}")

    st.markdown("---")

    # ── Save ──────────────────────────────────────────────────────────────────
    if st.button("💾 Save to Sheet", type="primary", use_container_width=True):
        if base_salary <= 0:
            st.error("Enter a base salary greater than 0.")
            return

        invalid = [s for s in splits if not s["name"].strip() or s["amount"] <= 0]
        if invalid:
            st.error("Each split row must have a recipient name and an amount > 0.")
            return

        with st.spinner("Saving to Google Sheets…"):
            result = save_salary_record(
                employee=employee,
                month=month_str,
                base_salary=base_salary,
                bank=bank,
                splits=splits,
            )

        if result["success"]:
            st.success(result["message"])
            st.balloons()
            st.session_state.split_count = 0
        else:
            st.error(result["message"])


# ─────────────────────────────────────────────────────────────────────────────
# User Management
# ─────────────────────────────────────────────────────────────────────────────

def _show_user_management():
    st.title("👥 Manage Associates")
    st.caption("Create login credentials for associates who mark out-office attendance.")
    st.markdown("---")

    st.subheader("Add New Associate")
    with st.form("add_user_form"):
        new_name = st.text_input("Full Name", placeholder="e.g. Aparna Pradyot Maitra")
        new_pin  = st.text_input(
            "3-Digit PIN", type="password", max_chars=3, placeholder="• • •"
        )
        submitted = st.form_submit_button("Add Associate", use_container_width=True)

    if submitted:
        if not new_name.strip():
            st.error("Please enter a name.")
        elif len(new_pin) != 3 or not new_pin.isdigit():
            st.error("PIN must be exactly 3 digits (numbers only).")
        else:
            try:
                add_user(new_name.strip(), new_pin.strip(), role="associate")
                st.success(f"✅ '{new_name.strip()}' added successfully.")
            except Exception as e:
                st.error(f"Error adding user: {e}")

    st.markdown("---")
    st.subheader("Current Associates")
    try:
        users      = fetch_users()
        associates = users[users["role"].str.strip().str.lower() == "associate"][["name"]].copy()
        associates.columns = ["Name"]
        if associates.empty:
            st.info("No associates added yet.")
        else:
            st.dataframe(associates.reset_index(drop=True), use_container_width=True)
    except Exception as e:
        st.error(f"Could not load users: {e}")
