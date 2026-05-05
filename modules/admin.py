"""
modules/admin.py
────────────────
Module 2 – Admin Dashboard

Sidebar pages:
  📊 Attendance Report   – upload XLS files → merged monthly report → CSV
  💰 Salary Processing   – full salary calculation with leave ledger
  👥 Manage Users        – add / view associates

─────────────────────────────────────────────────────────────────────────────
SALARY CALCULATION LOGIC
─────────────────────────────────────────────────────────────────────────────

Every month per employee:

  ADD_SUBSTRACT = B_Forward + CL_PL + Sunday_C_Off − Leave_Availed

  Study Leave month:
    → CL_PL = 0, Sunday_C_Off = 0, Leave_Availed = 0
    → Salary_Paid = 0, Deduction = 0
    → C_Forward = B_Forward  (balance just carries through untouched)

  Normal month, ADD_SUBSTRACT >= 0:
    → No deduction, Salary_Paid = Base_Salary
    → C_Forward = ADD_SUBSTRACT

  Normal month, ADD_SUBSTRACT < 0:
    → Deduction = Base_Salary / days_in_month × |ADD_SUBSTRACT|
    → Salary_Paid = Base_Salary − Deduction
    → C_Forward = 0

Fixed public holidays (employee gets day off; if they worked → earns comp off):
  26-Jan, 03-Mar, 19-Mar, 01-May, 15-Aug, 14-Sep, 20-Oct,
  09-Nov, 10-Nov, 11-Nov, 25-Dec

Sunday/C_Off is auto-calculated from the attendance XLS:
  Count rows where status is Present/½Present AND date is a Sunday
  or one of the fixed holidays above.
  Admin can then add an Extra Holiday Adjustment (± days) on top.

Leave_Availed is auto-calculated from attendance XLS:
  Absent              → 1.0 day
  Absent(No OutPunch) → 1.0 day
  ½Present            → 0.5 day
  All other statuses  → 0.0

XLS FILE FORMAT (your attendance system export):
  Row 0-7: headers/metadata
  Row 8:   employee name at col index 7
  Row 9:   column labels
  Row 10+: daily data (col 1=Date, 3=InTime, 4=OutTime, 6=Shift,
                        7=TotalDuration, 8=Status)
  Last row: summary text (skipped)
"""

from __future__ import annotations

import calendar
import io
from datetime import datetime, date

import pandas as pd
import streamlit as st

from utils.auth import logout, verify_admin
from utils.gsheets import (
    fetch_out_office_attendance,
    save_salary_ledger_row,
    save_salary_splits,
    fetch_carry_forward,
    ledger_month_exists,
    fetch_salary_ledger,
    fetch_all_employee_names,
    add_user,
    fetch_users,
)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Fixed public holidays as (month, day) tuples — year-agnostic
FIXED_HOLIDAYS: set[tuple[int, int]] = {
    (1, 26),   # Republic Day
    (3, 3),    # (company holiday)
    (3, 19),   # (company holiday)
    (5, 1),    # Labour Day
    (8, 15),   # Independence Day
    (9, 14),   # (company holiday)
    (10, 20),  # Diwali
    (11, 9),   # Diwali holiday
    (11, 10),  # Diwali holiday
    (11, 11),  # Diwali holiday
    (12, 25),  # Christmas
}


def _is_holiday(d: date) -> bool:
    """Return True if the date is a Sunday or a fixed public holiday."""
    return d.weekday() == 6 or (d.month, d.day) in FIXED_HOLIDAYS


# ─────────────────────────────────────────────────────────────────────────────
# XLS Parser
# ─────────────────────────────────────────────────────────────────────────────
def _parse_flat_attendance(file_obj, employee_name: str) -> pd.DataFrame | None:
    """
    Parses the flat XLSX exported from the attendance report page.
    Expected columns: Date | InTime | OutTime | Shift | Total_Duration | Status | Days_Count | Source
    """
    try:
        df = pd.read_excel(file_obj, sheet_name=0, header=0, engine="openpyxl")
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None

    # Normalise column names
    df.columns = [c.strip() for c in df.columns]

    def _status_to_days(status: str) -> float:
        s = str(status).strip().lower().replace(" ", "")
        if s in ("present", "weeklyoff", "presentonod", "weeklyoffpresent", "holidaypresent"):
            return 1.0
        if "\u00bdpresent" in s or "half" in s or "1/2" in s:
            return 0.5
        return 0.0

    df["Days_Count"] = df["Status"].apply(_status_to_days)


    if "Status" not in df.columns or "Date" not in df.columns:
        st.error("File does not have expected columns (Date, Status). Check the file.")
        return None

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df = df.dropna(subset=["Date"])
    df["Employee_Name"] = employee_name
    df["Source"]        = df.get("Source", "In-Office")

    return df[[
        "Employee_Name", "Date", "InTime", "OutTime",
        "Shift", "Total_Duration", "Status", "Days_Count", "Source",
    ]]




def _parse_attendance_xls(file_obj) -> pd.DataFrame | None:
    """
    Parse one XLS file from the attendance machine.

    Returns DataFrame with columns:
        Employee_Name | Date (YYYY-MM-DD) | InTime | OutTime | Shift |
        Total_Duration | Status | Days_Count | Source

    Returns None (and shows st.error) on any failure.
    """
    try:
        filename = getattr(file_obj, "name", ""),
        filename = filename[0]
        engine = "openpyxl" if filename.endswith(".xlsx") else "xlrd"
        raw = pd.read_excel(file_obj, sheet_name=0, header=None, engine=engine)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None

    # ── Employee name: row 8, col 7 ───────────────────────────────────────────
    employee_name = ""
    try:
        val = str(raw.iloc[8, 7]).strip()
        if val and val.lower() != "nan":
            employee_name = val
    except Exception:
        pass

    if not employee_name:
        for col_idx in range(7, raw.shape[1]):
            val = str(raw.iloc[8, col_idx]).strip()
            if val and val.lower() != "nan":
                employee_name = val
                break

    if not employee_name:
        st.error("Could not find employee name in this file (expected row 9, col 8).")
        return None

    # ── Data rows: index 10 → second-to-last ──────────────────────────────────
    data_start, data_end = 10, len(raw) - 1
    if data_end <= data_start:
        st.error(f"Not enough data rows in file for {employee_name}.")
        return None

    data = raw.iloc[data_start:data_end].reset_index(drop=True)

    col_map = {
        1: "Date", 3: "InTime", 4: "OutTime",
        6: "Shift", 7: "Total_Duration", 8: "Status",
    }
    df = pd.DataFrame()
    for pos, name in col_map.items():
        df[name] = data.iloc[:, pos].astype(str).str.strip() if pos < data.shape[1] else ""

    # ── Keep only valid date rows ─────────────────────────────────────────────
    df = df[df["Date"].str.match(r"^\d{2}-[A-Za-z]{3}-\d{4}$", na=False)].copy()
    if df.empty:
        st.error(f"No valid date rows found for {employee_name}.")
        return None

    df["Date"] = (
        pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce")
        .dt.strftime("%Y-%m-%d")
    )
    df = df.dropna(subset=["Date"])

    # ── Days_Count ────────────────────────────────────────────────────────────
    def _days_count(status: str) -> float:
        s = status.strip().lower()
        if s in ("present", "weeklyoff", "presentonod", "weeklyoffpresent","holidaypresent"):
            return 1.0
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
# Salary calculation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _auto_leave_availed(df: pd.DataFrame) -> float:
    """
    Count leave days from parsed attendance rows for one employee/month.

    Absent / Absent(No OutPunch) → 1.0 each
    ½Present                     → 0.5 each
    Everything else              → 0.0
    """
    total = 0.0
    for _, row in df.iterrows():
        s = str(row["Status"]).strip().lower()
        if s in ("weeklyoff", "weeklyoffpresent", "holidaypresent", "presentonod"):
            continue
        elif "absent" in s:
            total += 1.0
        elif "\u00bdpresent" in s or "half" in s or "1/2" in s:
            total += 0.5
    return total


def _auto_sunday_c_off(df: pd.DataFrame, extra_adjustment: float = 0.0) -> float:
    """
    Count days where the employee was Present on a Sunday or fixed holiday
    (i.e. they earned a compensatory off).
    Then add the admin's manual extra_adjustment.
    """
    total = 0.0
    for _, row in df.iterrows():
        s = str(row["Status"]).strip().lower()
        if s in ("weeklyoffpresent", "holidaypresent"):
            total += 1.0
            continue

        worked = s in ("present", "presentonod") or "\u00bdpresent" in s or "1/2" in s or "half" in s
        if not worked:
            continue
        try:
            d = date.fromisoformat(str(row["Date"]))
            if _is_holiday(d):
                total += 1.0 if s == "present" else 0.5
        except ValueError:
            continue
    return max(0.0, total + extra_adjustment)


def _calculate_salary(
    b_forward: float,
    cl_pl: float,
    sunday_c_off: float,
    leave_availed: float,
    base_salary: float,
    days_in_month: int,
    is_study_leave: bool,
) -> dict:
    """
    Core salary formula. Returns a dict with all derived values.
    """
    if is_study_leave:
        return {
            "b_forward"     : b_forward,
            "cl_pl"         : 0.0,
            "sunday_c_off"  : 0.0,
            "leave_availed" : 0.0,
            "add_substract" : b_forward,   # balance just passes through
            "c_forward"     : b_forward,
            "deduction"     : 0.0,
            "salary_paid"   : 0.0,
        }

    add_substract = b_forward + cl_pl + sunday_c_off - leave_availed

    if add_substract >= 0:
        c_forward   = add_substract
        deduction   = 0.0
        salary_paid = base_salary
    else:
        c_forward   = 0.0
        deduction   = (base_salary / days_in_month) * abs(add_substract)
        salary_paid = base_salary - deduction

    return {
        "b_forward"     : b_forward,
        "cl_pl"         : cl_pl,
        "sunday_c_off"  : sunday_c_off,
        "leave_availed" : leave_availed,
        "add_substract" : add_substract,
        "c_forward"     : c_forward,
        "deduction"     : deduction,
        "salary_paid"   : salary_paid,
    }


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

def _show_attendance_consolidation2():
    st.title("📊 Monthly Attendance Report")
    st.caption(
        "Upload one .xls per employee from your attendance machine. "
        "The employee name is read from inside the file automatically."
    )
    st.markdown("---")

    col1, col2 = st.columns(2)
    with col1:
        year_opts = list(range(2024, datetime.now().year + 2))
        year = st.selectbox("Year", options=year_opts,
                            index=year_opts.index(datetime.now().year))
    with col2:
        month_num = st.selectbox(
            "Month", options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1,
        )
    month_str   = f"{year}-{month_num:02d}"
    month_label = datetime(year, month_num, 1).strftime("%B %Y")
    st.markdown("---")

    st.subheader("Step 1 — Upload Attendance Files")
    uploaded_files = st.file_uploader(
        "Upload XLS files (one per employee)",
        type=["xls", "xlsx"], accept_multiple_files=True,
        label_visibility="collapsed",
    )

    in_office_frames: list[pd.DataFrame] = []
    if uploaded_files:
        st.write(f"**{len(uploaded_files)} file(s) uploaded — parsing…**")
        for uf in uploaded_files:
            parsed = _parse_attendance_xls(uf)
            if parsed is None:
                continue
            month_rows = parsed[parsed["Date"].str.startswith(month_str, na=False)]
            if month_rows.empty:
                st.warning(f"⚠️ `{uf.name}` — no rows for {month_label}. Skipped.")
                continue
            emp_name      = month_rows["Employee_Name"].iloc[0]
            working_days  = month_rows["Days_Count"].sum()
            st.success(
                f"✅ `{uf.name}` → **{emp_name}**  |  "
                f"{len(month_rows)} calendar days  |  **{working_days:.1f} working days**"
            )
            in_office_frames.append(month_rows)

    in_office_df = (
        pd.concat(in_office_frames, ignore_index=True)
        if in_office_frames else pd.DataFrame()
    )

    st.markdown("---")
    st.subheader("Step 2 — Merge with Out-Office Records")

    if st.button("🔄 Generate Report", type="primary", use_container_width=True):
        with st.spinner("Fetching out-office data from Google Sheets…"):
            out_df = fetch_out_office_attendance(month=month_str)

        if out_df.empty and in_office_df.empty:
            st.warning("No data found for the selected month.")
            return

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

        frames   = [f for f in [in_office_df, out_df_proc] if not f.empty]
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values(["Employee_Name", "Date"]).reset_index(drop=True)

        summary_rows = []
        for emp, grp in combined.groupby("Employee_Name"):
            present    = int((grp["Days_Count"] == 1.0).sum())
            half       = int((grp["Days_Count"] == 0.5).sum())
            weekly_off = int(
                grp["Status"].str.lower().str.contains("weeklyoff", na=False).sum()
            )
            absent = int((grp["Days_Count"] == 0.0).sum()) - weekly_off
            summary_rows.append({
                "Employee"           : emp,
                "Present Days"       : present,
                "Half Days"          : half,
                "Absent Days"        : max(absent, 0),
                "Weekly Off"         : weekly_off,
                "Total Working Days" : grp["Days_Count"].sum(),
            })
        summary = pd.DataFrame(summary_rows)

        st.markdown(f"#### Detail — {month_label}")
        st.dataframe(combined, use_container_width=True)
        st.markdown("#### Summary")
        st.dataframe(summary, use_container_width=True)

        st.markdown("#### Individual Records")
        employees = combined["Employee_Name"].unique()
        for emp in sorted(employees):
            emp_df = combined[combined["Employee_Name"] == emp].drop(columns=["Employee_Name"])
            with st.expander(f"📄 {emp}"):
                st.dataframe(emp_df.reset_index(drop=True), use_container_width=True)

        col_a, col_b = st.columns(2)
        with col_a:
            buf = io.StringIO()
            combined.to_csv(buf, index=False)
            st.download_button("⬇️ Full Report (CSV)", buf.getvalue(),
                               f"attendance_detail_{month_str}.csv", "text/csv",
                               use_container_width=True)
        with col_b:
            buf = io.StringIO()
            summary.to_csv(buf, index=False)
            st.download_button("⬇️ Summary (CSV)", buf.getvalue(),
                               f"attendance_summary_{month_str}.csv", "text/csv",
                               use_container_width=True)

def _show_attendance_consolidation():
    st.title("📊 Monthly Attendance Report")
    st.caption(
        "Upload one .xls per employee from your attendance machine. "
        "Select the matching name from your system if the names differ."
    )
    st.markdown("---")

    col1, col2 = st.columns(2)
    with col1:
        year_opts = list(range(2024, datetime.now().year + 2))
        year = st.selectbox("Year", options=year_opts,
                            index=year_opts.index(datetime.now().year))
    with col2:
        month_num = st.selectbox(
            "Month", options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1,
        )
    month_str   = f"{year}-{month_num:02d}"
    month_label = datetime(year, month_num, 1).strftime("%B %Y")
    st.markdown("---")

    # ── Step 1: Upload ────────────────────────────────────────────────────────
    st.subheader("Step 1 — Upload Attendance Files")
    uploaded_files = st.file_uploader(
        "Upload XLS files (one per employee)",
        type=["xls", "xlsx"], accept_multiple_files=True,
        label_visibility="collapsed",
    )

    # name_map: { xls_name -> system_name }
    # stored in session state so selectboxes persist across reruns
    if "att_name_map" not in st.session_state:
        st.session_state.att_name_map = {}

    system_names = ["— same as file —"] + fetch_all_employee_names()
    in_office_frames: list[pd.DataFrame] = []

    if uploaded_files:
        st.write(f"**{len(uploaded_files)} file(s) uploaded**")
        for uf in uploaded_files:
            parsed = _parse_attendance_xls(uf)
            if parsed is None:
                continue

            xls_name   = parsed["Employee_Name"].iloc[0]
            month_rows = parsed[parsed["Date"].str.startswith(month_str, na=False)].copy()

            if month_rows.empty:
                st.warning(f"⚠️ `{uf.name}` — no rows for {month_label}. Skipped.")
                continue

            # ── Name mapping selectbox ─────────────────────────────────────
            col_a, col_b = st.columns([2, 3])
            with col_a:
                st.markdown(f"**Name in file:** `{xls_name}`")
            with col_b:
                default_idx = 0
                if xls_name in st.session_state.att_name_map:
                    saved = st.session_state.att_name_map[xls_name]
                    if saved in system_names:
                        default_idx = system_names.index(saved)

                chosen = st.selectbox(
                    f"Map to system name",
                    options=system_names,
                    index=default_idx,
                    key=f"name_map_{xls_name}",
                    label_visibility="collapsed",
                )
                st.session_state.att_name_map[xls_name] = chosen

            # Apply mapped name
            final_name = xls_name if chosen == "— same as file —" else chosen
            month_rows["Employee_Name"] = final_name

            working_days = month_rows["Days_Count"].sum()
            st.success(
                f"✅ **{final_name}**  |  "
                f"{len(month_rows)} calendar days  |  "
                f"**{working_days:.1f} working days**"
            )
            in_office_frames.append(month_rows)
            st.markdown("---")

    in_office_df = (
        pd.concat(in_office_frames, ignore_index=True)
        if in_office_frames else pd.DataFrame()
    )

    # ── Step 2: Generate ─────────────────────────────────────────────────────
    st.subheader("Step 2 — Merge with Out-Office Records")

    if st.button("🔄 Generate Report", type="primary", use_container_width=True):
        with st.spinner("Fetching out-office data from Google Sheets…"):
            out_df = fetch_out_office_attendance(month=month_str)

        if out_df.empty and in_office_df.empty:
            st.warning("No data found for the selected month.")
            return

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

        frames   = [f for f in [in_office_df, out_df_proc] if not f.empty]
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values(["Employee_Name", "Date"]).reset_index(drop=True)
        combined = combined.drop_duplicates(subset=["Employee_Name", "Date", "Source"]).copy()
        # ── Summary table (all employees, one row each) ───────────────────────
        summary_rows = []
        for emp, grp in combined.groupby("Employee_Name"):
            present    = int((grp["Days_Count"] == 1.0).sum())
            half       = int((grp["Days_Count"] == 0.5).sum())
            weekly_off = int(grp["Status"].str.lower().str.contains("weeklyoff", na=False).sum())
            absent     = int((grp["Days_Count"] == 0.0).sum()) - weekly_off
            summary_rows.append({
                "Employee"           : emp,
                "Present Days"       : present,
                "Half Days"          : half,
                "Absent Days"        : max(absent, 0),
                "Weekly Off"         : weekly_off,
                "Total Working Days" : grp["Days_Count"].sum(),
            })
        summary = pd.DataFrame(summary_rows)

        st.markdown(f"### Summary — {month_label}")
        st.dataframe(
            summary.reset_index(drop=True),
            use_container_width=True,
            column_config={
                "Employee"           : st.column_config.TextColumn("Employee", width="large"),
                "Present Days"       : st.column_config.NumberColumn("Present", width="small"),
                "Half Days"          : st.column_config.NumberColumn("Half Days", width="small"),
                "Absent Days"        : st.column_config.NumberColumn("Absent", width="small"),
                "Weekly Off"         : st.column_config.NumberColumn("Weekly Off", width="small"),
                "Total Working Days" : st.column_config.NumberColumn("Total Working Days", width="medium"),
            },
            hide_index=True,
        )

        # ── Per-employee breakdown ────────────────────────────────────────────
        st.markdown("### Individual Records")
        for emp in sorted(combined["Employee_Name"].unique()):
            emp_df = combined[combined["Employee_Name"] == emp].drop(
                columns=["Employee_Name"]
            ).reset_index(drop=True)

            emp_summary = summary[summary["Employee"] == emp].drop(
                columns=["Employee"]
            ).reset_index(drop=True)

            with st.expander(f"📄 {emp}"):
                st.markdown("**Summary**")
                st.dataframe(
                    emp_summary,
                    use_container_width=True,
                    column_config={
                        "Present Days"       : st.column_config.NumberColumn("Present"),
                        "Half Days"          : st.column_config.NumberColumn("Half Days"),
                        "Absent Days"        : st.column_config.NumberColumn("Absent"),
                        "Weekly Off"         : st.column_config.NumberColumn("Weekly Off"),
                        "Total Working Days" : st.column_config.NumberColumn("Total Working Days"),
                    },
                    hide_index=True,
                )

                st.markdown("**Daily Breakdown**")
                st.dataframe(
                    emp_df,
                    use_container_width=True,
                    column_config={
                        "Date"           : st.column_config.TextColumn("Date", width="medium"),
                        "InTime"         : st.column_config.TextColumn("In", width="small"),
                        "OutTime"        : st.column_config.TextColumn("Out", width="small"),
                        "Shift"          : st.column_config.TextColumn("Shift", width="small"),
                        "Total_Duration" : st.column_config.TextColumn("Duration", width="small"),
                        "Status"         : st.column_config.TextColumn("Status", width="medium"),
                        "Days_Count"     : st.column_config.NumberColumn("Days", width="small"),
                        "Source"         : st.column_config.TextColumn("Source", width="small"),
                    },
                    hide_index=True,
                )

                # Individual downloads
                dl_col1, dl_col2 = st.columns(2)
                with dl_col1:
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        emp_df.to_excel(writer, index=False, sheet_name="Attendance")
                    st.download_button(
                        f"⬇️ Full Report (XLSX)",
                        data=buf.getvalue(),
                        file_name=f"{emp}_attendance_{month_str}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        key=f"dl_full_{emp}",
                    )
                with dl_col2:
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        emp_summary.to_excel(writer, index=False, sheet_name="Summary")
                    st.download_button(
                        f"⬇️ Summary (XLSX)",
                        data=buf.getvalue(),
                        file_name=f"{emp}_summary_{month_str}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        key=f"dl_sum_{emp}",
                    )
# ─────────────────────────────────────────────────────────────────────────────
# Phase B – Salary Processing
# ─────────────────────────────────────────────────────────────────────────────

def _show_salary_processing():
    st.title("💰 Salary Processing")
    st.markdown(
        "Upload the attendance XLS for an employee, review the auto-calculated "
        "values, adjust if needed, then save to the ledger."
    )
    st.markdown("---")

    # ── Session state init ────────────────────────────────────────────────────
    for key, default in [
        ("sal_split_count", 0),
        ("sal_calc_result", None),   # stores last calculation dict
        ("sal_xls_df", None),        # parsed attendance rows for selected month
        ("sal_xls_employee", ""),    # employee name read from XLS
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    # ── Employee + month ──────────────────────────────────────────────────────
    employee_names = fetch_all_employee_names()
    if not employee_names:
        st.warning("No associates found. Add them via Manage Users first.")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        employee = st.selectbox("Employee", options=employee_names, key="sal_employee")
    with col2:
        year_opts = list(range(2024, datetime.now().year + 2))
        sal_year  = st.selectbox("Year", options=year_opts,
                                 index=year_opts.index(datetime.now().year),
                                 key="sal_year")
    with col3:
        sal_month = st.selectbox(
            "Month", options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1,
            key="sal_month",
        )

    month_str   = f"{sal_year}-{sal_month:02d}"
    month_label = datetime(sal_year, sal_month, 1).strftime("%B %Y")
    days_in_month = calendar.monthrange(sal_year, sal_month)[1]

    # ── Duplicate warning ─────────────────────────────────────────────────────
    if ledger_month_exists(employee, month_str):
        st.warning(
            f"⚠️ A ledger record already exists for **{employee}** in **{month_label}**. "
            "Saving again will be blocked. Delete the existing row from the sheet to re-process."
        )

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 1 — Upload attendance XLS
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("1 — Upload Attendance File")
    st.caption("Upload the XLS for this employee. The system auto-calculates Leave Availed and Sunday/C Off.")

    uploaded = st.file_uploader(
        "Upload XLS", type=["xls", "xlsx"], key="sal_xls_upload_v2",
        label_visibility="collapsed",
    )

    month_df = pd.DataFrame()    # rows for selected month only
    auto_leave_availed  = 0.0
    auto_sunday_c_off   = 0.0
    xls_employee_name   = ""

    if uploaded:
        import io as _io

        filename = getattr(uploaded, "name", "")
        if isinstance(filename, tuple):
            filename = filename[0]
        filename = str(filename)

        file_bytes = uploaded.read()
        is_xlsx = filename.lower().endswith(".xlsx")
        print(is_xlsx)
        peek_engine = "openpyxl" if is_xlsx else "xlrd"

        try:
            peek = pd.read_excel(_io.BytesIO(file_bytes), header=None, engine=peek_engine, nrows=1)
            
            is_flat = str(peek.iloc[0,0]).strip().lower() == "date"
        except:
            is_flat = False

        if is_flat:
            parsed = _parse_flat_attendance(_io.BytesIO(file_bytes), employee)
        else:
            parsed = _parse_attendance_xls(_io.BytesIO(file_bytes))

        if parsed is not None:
            xls_employee_name = parsed["Employee_Name"].iloc[0]
            month_df = parsed[parsed["Date"].str.startswith(month_str, na=False)].copy()

            if month_df.empty:
                st.warning(
                    f"No rows found for {month_label} in this file. "
                    "Check that you selected the right month above."
                )
            else:
                auto_leave_availed = _auto_leave_availed(month_df)
                auto_sunday_c_off  = _auto_sunday_c_off(month_df)
                
                st.session_state["sal_leave_availed"] = float(auto_leave_availed)
                st.session_state["sal_sc_off_base"]   = float(auto_sunday_c_off)
              
                st.success(
                    f"✅ **{xls_employee_name}** — {len(month_df)} calendar days found  |  "
                    f"Auto Leave Availed: **{auto_leave_availed}**  |  "
                    f"Auto Sunday/C Off: **{auto_sunday_c_off}**"
                )

                with st.expander("View raw attendance rows for this month"):
                    st.dataframe(
                        month_df[["Date", "InTime", "OutTime", "Status", "Days_Count"]],
                        use_container_width=True,
                    )

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 2 — Study Leave toggle
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("2 — Study Leave")
    is_study_leave = st.toggle(
        "This is a Study Leave month (no salary paid, no CL/PL accrual, balance carries through)",
        value=False,
        key="sal_study_leave",
    )

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 3 — Leave parameters (editable, auto-filled from XLS)
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("3 — Leave Parameters")

    if is_study_leave:
        st.info(
            "Study Leave month — all leave fields are zeroed out automatically. "
            "Only B/Forward carries through."
        )

    # B/Forward: auto-loaded from ledger
    b_forward_auto = fetch_carry_forward(employee, month_str)

    col_a, col_b = st.columns(2)
    with col_a:
        b_forward = st.number_input(
            "B/Forward (auto-loaded from previous month)",
            value=float(b_forward_auto),
            step=0.5,
            format="%.1f",
            disabled=is_study_leave,
            key="sal_b_forward",
            help="Carry-forward from the last saved month. Edit only if correcting an error.",
        )
    with col_b:
        is_article = False
        try:
            users = fetch_users()
            user_row = users[users["name"].str.strip().str.lower()==employee.strip().lower()]
            if not user_row.empty:
                is_article = user_row.iloc[0]["role"].strip().lower() == "article"
        except Exception:
            pass
        cl_pl_default = 0.0 if (is_study_leave or is_article) else 1.5
        cl_pl_label = "CL/PL Eligible (0 - Article)" if is_article else "CL/PL Eligible (always 1.5)"
     
        cl_pl = st.number_input(
            "CL/PL Eligible (always 1.5)",
            value=0.0 if is_study_leave else 1.5,
            step=0.5,
            format="%.1f",
            disabled=True,
            key="sal_cl_pl",
        )

    col_c, col_d, col_e = st.columns(3)
    with col_c:
        sunday_c_off_base = st.number_input(
            "Sunday/C Off (auto from XLS)",
            value=0.0 if is_study_leave else float(auto_sunday_c_off),
            step=0.5,
            format="%.1f",
            disabled=is_study_leave,
            key="sal_sc_off_base",
            help="Days the employee worked on a Sunday or fixed public holiday.",
        )
    with col_d:
        extra_adj = st.number_input(
            "Extra Holiday Adjustment (±)",
            value=0.0,
            step=0.5,
            format="%.1f",
            disabled=is_study_leave,
            key="sal_extra_adj",
            help=(
                "Use this to add or remove comp-off days for extra declared holidays "
                "not in the standard list. E.g. +1 if a surprise holiday was given."
            ),
        )
    with col_e:
        sunday_c_off_final = max(0.0, sunday_c_off_base + extra_adj) if not is_study_leave else 0.0
        st.number_input(
            "Final Sunday/C Off",
            value=sunday_c_off_final,
            disabled=True,
            format="%.1f",
            key="sal_sc_off_final",
        )

    leave_availed = st.number_input(
        "Leave Availed (auto from XLS — edit if needed)",
        value=0.0 if is_study_leave else float(auto_leave_availed),
        step=0.5,
        min_value=0.0,
        format="%.1f",
        disabled=is_study_leave,
        key="sal_leave_availed",
        help=(
            "Absent = 1 day, Absent(No OutPunch) = 1 day, ½Present = 0.5 day. "
            "Edit if the raw count needs a manual correction."
        ),
    )

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 4 — Salary & Bank
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("4 — Salary & Bank")

    col_f, col_g = st.columns(2)
    with col_f:
        base_salary = st.number_input(
            "Base Salary (₹)", min_value=0.0, step=500.0,
            value=0.0, format="%.2f", key="sal_base",
        )
    with col_g:
        try:
            bank_options = list(st.secrets["banks"]["accounts"])
        except Exception:
            bank_options = ["FF/JSB", "M&RFSPL/UBI", "PTBS/UBI", "MCH", "AUSM/HDFC"]
        bank = st.selectbox("Pay From (Entity/Bank)", options=bank_options, key="sal_bank")

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 5 — Live Calculation Preview
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("5 — Calculation Preview")
    st.caption(f"Month has **{days_in_month} days**. Formula: Salary ÷ {days_in_month} × |deductible days|")

    if base_salary > 0:
        result = _calculate_salary(
            b_forward      = b_forward,
            cl_pl          = 0.0 if is_study_leave else cl_pl,
            sunday_c_off   = sunday_c_off_final,
            leave_availed  = leave_availed,
            base_salary    = base_salary,
            days_in_month  = days_in_month,
            is_study_leave = is_study_leave,
        )

        # ── Display the ledger row as a clean table ───────────────────────────
        preview_data = {
            "Field": [
                "B/Forward (in)",
                "CL/PL Eligible",
                "Sunday/C Off",
                "Leave Availed",
                "ADD/SUBSTRACT",
                "C/Forward (out)",
                "Base Salary",
                "Deduction",
                "Salary Paid",
            ],
            "Value": [
                f"{result['b_forward']:.1f} days",
                f"{result['cl_pl']:.1f} days",
                f"{result['sunday_c_off']:.1f} days",
                f"{result['leave_availed']:.1f} days",
                f"{result['add_substract']:.1f} days",
                f"{result['c_forward']:.1f} days",
                f"₹{base_salary:,.2f}",
                f"₹{result['deduction']:,.2f}",
                f"₹{result['salary_paid']:,.2f}",
            ],
        }
        preview_df = pd.DataFrame(preview_data)
        st.dataframe(preview_df, use_container_width=True, hide_index=True)

        # Colour-coded verdict
        if is_study_leave:
            st.info("📚 Study Leave — no salary paid this month. Balance carries through.")
        elif result["add_substract"] >= 0:
            st.success(
                f"✅ No deduction — **{result['add_substract']:.1f} days** carry forward next month."
            )
        else:
            st.error(
                f"⚠️ Deduction of **₹{result['deduction']:,.2f}** applied. "
                f"({base_salary:,.0f} ÷ {days_in_month} × {abs(result['add_substract']):.1f})"
            )

        # Deduction formula shown explicitly
        if not is_study_leave and result["add_substract"] < 0:
            st.caption(
                f"Formula: ₹{base_salary:,.0f} ÷ {days_in_month} × "
                f"{abs(result['add_substract']):.1f} = ₹{result['deduction']:,.2f}"
            )

        st.session_state.sal_calc_result = result

    else:
        st.info("Enter a base salary above to see the calculation preview.")
        st.session_state.sal_calc_result = None

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 6 — Salary Split
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("6 — Salary Split")
    st.caption(
        "If the salary is paid to multiple recipients, press **Split** for each one. "
        "Leave empty to record the full salary directly to the employee."
    )

    btn1, btn2 = st.columns([1, 1])
    with btn1:
        if st.button("➕ Split", use_container_width=True):
            st.session_state.sal_split_count += 1
    with btn2:
        if st.session_state.sal_split_count > 0:
            if st.button("🗑️ Clear All Splits", use_container_width=True):
                st.session_state.sal_split_count = 0
                st.rerun()

    splits: list[dict] = []
    running_total = 0.0

    if st.session_state.sal_split_count > 0:
        hc = st.columns([3, 2,2])
        hc[0].markdown("**Recipient Name**")
        hc[1].markdown("**Amount (₹)**")
        hc[2].markdown("**Bank Account**")

        for i in range(st.session_state.sal_split_count):
            rc = st.columns([3, 2])
            with rc[0]:
                rec_name = st.text_input(
                    f"n{i}", key=f"sp_name_{i}",
                    label_visibility="collapsed",
                    placeholder=f"Recipient {i + 1}",
                )
            with rc[1]:
                rec_amount = st.number_input(
                    f"a{i}", key=f"sp_amt_{i}",
                    label_visibility="collapsed",
                    min_value=0.0, step=100.0, format="%.2f",
                )

            with rc[2]:
                rec_bank = st.selectbox(
                    f"b{i}", key=f"sp_bank_{i}",
                    options=bank_options,
                    label_visibikity="collapsed",
                )

            splits.append({"name": rec_name, "amount": rec_amount, "bank":rec_bank})
            running_total += rec_amount

        if base_salary > 0 and st.session_state.sal_calc_result:
            salary_paid = st.session_state.sal_calc_result["salary_paid"]
            diff = salary_paid - running_total
            if abs(diff) < 0.01:
                st.success(f"✅ Split total ₹{running_total:,.2f} matches Salary Paid.")
            elif diff > 0:
                st.warning(f"⚠️ Unallocated: ₹{diff:,.2f}")
            else:
                st.error(f"❌ Over-allocated by ₹{abs(diff):,.2f}")

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 7 — Save
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("7 — Save to Google Sheets")

    if st.button("💾 Save Salary Record", type="primary", use_container_width=True):

        # Validations
        if base_salary <= 0:
            st.error("Enter a base salary > 0 before saving.")
            st.stop()

        if st.session_state.sal_calc_result is None:
            st.error("Calculation result is missing. Check your inputs.")
            st.stop()

        invalid_splits = [s for s in splits if not s["name"].strip() or s["amount"] <= 0]
        if invalid_splits:
            st.error("Each split row needs a recipient name and amount > 0.")
            st.stop()

        result = st.session_state.sal_calc_result

        with st.spinner("Saving to Google Sheets…"):
            # 1. Save main ledger row
            ledger_result = save_salary_ledger_row(
                employee       = employee,
                month          = month_str,
                b_forward      = result["b_forward"],
                cl_pl          = result["cl_pl"],
                sunday_c_off   = result["sunday_c_off"],
                leave_availed  = result["leave_availed"],
                add_substract  = result["add_substract"],
                c_forward      = result["c_forward"],
                base_salary    = base_salary,
                days_in_month  = days_in_month,
                deduction      = result["deduction"],
                salary_paid    = result["salary_paid"],
                bank           = bank,
                is_study_leave = is_study_leave,
            )

            if not ledger_result["success"]:
                st.error(ledger_result["message"])
                st.stop()

            # 2. Save splits (if any)
            if splits:
                splits_result = save_salary_splits(employee, month_str, splits)
                if not splits_result["success"]:
                    st.warning(
                        f"Ledger saved, but splits failed: {splits_result['message']}"
                    )

        st.success(ledger_result["message"])
        st.balloons()
        st.session_state.sal_split_count  = 0
        st.session_state.sal_calc_result  = None

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # SECTION 8 — View Ledger History (collapsible)
    # ════════════════════════════════════════════════════════════════════════
    with st.expander("📋 View full salary ledger (all employees, all months)"):
        with st.spinner("Loading ledger…"):
            ledger_df = fetch_salary_ledger()
        if ledger_df.empty:
            st.info("No records saved yet.")
        else:
            # Filter to selected employee if desired
            show_all = st.checkbox("Show all employees", value=False)
            if not show_all:
                ledger_df = ledger_df[
                    ledger_df["Employee"].str.strip().str.lower()
                    == employee.strip().lower()
                ]
            st.dataframe(ledger_df, use_container_width=True)

            buf = io.StringIO()
            ledger_df.to_csv(buf, index=False)
            st.download_button(
                "⬇️ Download Ledger (CSV)",
                buf.getvalue(),
                "salary_ledger.csv",
                "text/csv",
            )


# ─────────────────────────────────────────────────────────────────────────────
# User Management
# ─────────────────────────────────────────────────────────────────────────────

def _show_user_management():
    st.title("👥 Manage Associates")
    st.caption("Create login credentials for associates who mark out-office attendance.")
    st.markdown("---")

    st.subheader("Add New Associate")
    with st.form("add_user_form"):
        new_name  = st.text_input("Full Name", placeholder="e.g. Aparna Pradyot Maitra")
        new_pin   = st.text_input("3-Digit PIN", type="password",
                              max_chars=3, placeholder="• • •")
        new_role  = st.selectbox("Role", options=["employee", "article", "partner", "admin"])
        submitted = st.form_submit_button("Add User", use_container_width=True)

    if submitted:
        if not new_name.strip():
            st.error("Please enter a name.")
        elif len(new_pin) != 3 or not new_pin.isdigit():
            st.error("PIN must be exactly 3 digits (numbers only).")
        else:
            try:
                add_user(new_name.strip(), new_pin.strip(), role=new_role)
                role_label = {"article": "Article", "employee": "Employee", "admin": "Admin"}
                st.success(f"✅ '{new_name.strip()}' added as {role_label.get(new_role, new_pin)}.")
            except Exception as e:
                st.error(f"Error: {e}")

    st.markdown("---")
    st.subheader("Current Users")
    try:
        users = fetch_users()
        if users.empty:
            st.info("No users added yet.")
        else:
            display = users[["name", "role"]].copy()
            display.columns = ["Name", "Role"]
            st.dataframe(display.reset_index(drop=True), use_container_width=True)
    except Exception as e:
        st.error(f"Could not load users: {e}")  