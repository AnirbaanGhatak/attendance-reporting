"""
modules/admin.py
────────────────
Module 2 – Admin Dashboard

Role scope:
    admin   → Attendance Report, Manage Users, Salary Ledger (read-only)
    partner → everything admin has + Salary Processing

Sidebar pages:
    📊 Attendance Report   – upload master XLS → merged monthly report → XLSX
    👥 Manage Users        – add / view / edit users (all roles)
    📋 Salary Ledger       – read-only view of saved records per month,
                             graceful "not yet processed" state per employee
    💰 Salary Processing   – full calculation engine (partner only)

SALARY CALCULATION LOGIC
────────────────────────
  ADD_SUBSTRACT = B_Forward + CL_PL + Sunday_C_Off − Leave_Availed

  Study Leave month:
    CL_PL=0, Sunday_C_Off=0, Leave_Availed=0
    Salary_Paid=0, Deduction=0, C_Forward=B_Forward

  ADD_SUBSTRACT >= 0:  no deduction, C_Forward = ADD_SUBSTRACT
  ADD_SUBSTRACT  < 0:  Deduction = Base/days × |ADD_SUBSTRACT|, C_Forward = 0

Articles: no monthly 1.5 CL/PL (cl_pl always 0)
Employees: 1.5 CL/PL per month

MASTER XLS FORMAT (eTimeTrackLite Employee Wise export):
  Header row: col[1]="Employee Code:", col[3]=code, col[7]=name
  Data rows:  col[1]=Date, col[3]=InTime, col[4]=OutTime,
              col[6]=Shift, col[7]=Total_Duration, col[8]=Status
  End of block: col[1] contains "Total Duration="
"""

from __future__ import annotations

import calendar
import io
from datetime import datetime, date

import pandas as pd
import streamlit as st

from utils.auth import logout
from utils.database import (
    fetch_attendance,
    fetch_out_office_attendance,
    save_in_office_attendance,
    save_salary_ledger_row,
    save_salary_splits,
    fetch_salary_splits,
    fetch_carry_forward,
    ledger_month_exists,
    fetch_salary_ledger,
    fetch_all_employee_names,
    fetch_user_by_name,
    update_user_salary,
    add_user,
    fetch_users,
    verify_admin,
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

FIXED_HOLIDAYS: set[tuple[int, int]] = {
    (1, 26), (3, 3),  (3, 19), (5, 1),  (8, 15),
    (9, 14), (10, 20),(11, 9), (11, 10),(11, 11),(12, 25),
}


def _is_holiday(d: date) -> bool:
    return d.weekday() == 6 or (d.month, d.day) in FIXED_HOLIDAYS


# ─────────────────────────────────────────────────────────────────────────────
# XLS Parsers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_master_attendance_xls(file_obj) -> dict[str, pd.DataFrame] | None:
    """
    Parse the combined master XLS from eTimeTrackLite (Employee Wise export).
    Verified column positions against actual May 2026 export.
    Returns dict { employee_name: DataFrame } or None on failure.
    """
    import io as _io

    try:
        file_bytes = file_obj if isinstance(file_obj, bytes) else file_obj.read()
        raw = pd.read_excel(
            _io.BytesIO(file_bytes), sheet_name=0, header=None, engine="xlrd",
        )
    except Exception as e:
        st.error(f"Could not read master XLS: {e}")
        return None

    employee_starts: list[tuple[int, str]] = []
    for i in range(len(raw)):
        try:
            cell = str(raw.iloc[i, 1]).strip()
        except Exception:
            continue
        if cell != "Employee Code:":
            continue
        try:
            code = str(raw.iloc[i, 3]).strip()
            name = str(raw.iloc[i, 7]).strip()
            if not name or name.lower() == "nan" or name == code:
                name = f"Unknown_{code}"
        except Exception:
            name = f"Unknown_{i}"
        employee_starts.append((i, name))

    if not employee_starts:
        st.error("No employee records found in the file. Check the file format.")
        return None

    def _days_count(status: str) -> float:
        s = str(status).strip().lower().replace(" ", "")
        if s in ("present", "weeklyoff", "presentonod", "weeklyoffpresent", "holidaypresent"):
            return 1.0
        if "\u00bdpresent" in s or "half" in s or "1/2" in s:
            return 0.5
        return 0.0

    results: dict[str, pd.DataFrame] = {}

    for block_idx, (start_row, emp_name) in enumerate(employee_starts):
        data_start = start_row + 2
        search_end = (
            employee_starts[block_idx + 1][0]
            if block_idx + 1 < len(employee_starts)
            else len(raw)
        )

        summary_row = None
        for i in range(data_start, search_end):
            try:
                if "Total Duration=" in str(raw.iloc[i, 1]):
                    summary_row = i
                    break
            except Exception:
                continue

        if summary_row is None:
            st.warning(f"⚠️ Could not find summary row for **{emp_name}**. Skipped.")
            continue

        data = raw.iloc[data_start:summary_row].reset_index(drop=True)
        col_map = {1: "Date", 3: "InTime", 4: "OutTime", 6: "Shift", 7: "Total_Duration", 8: "Status"}
        df = pd.DataFrame()
        for pos, col_name in col_map.items():
            df[col_name] = (
                data.iloc[:, pos].astype(str).str.strip() if pos < data.shape[1] else ""
            )

        df = df[df["Date"].str.match(r"^\d{2}-[A-Za-z]{3}-\d{4}$", na=False)].copy()
        if df.empty:
            st.warning(f"⚠️ No valid date rows for **{emp_name}**. Skipped.")
            continue

        df["Date"] = (
            pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce")
            .dt.strftime("%Y-%m-%d")
        )
        df = df.dropna(subset=["Date"])
        df["Days_Count"]    = df["Status"].apply(_days_count)
        df["Employee_Name"] = emp_name
        df["Source"]        = "In-Office"

        results[emp_name] = df[[
            "Employee_Name", "Date", "InTime", "OutTime",
            "Shift", "Total_Duration", "Status", "Days_Count", "Source",
        ]]

    if not results:
        st.error("No employee data could be parsed from the file.")
        return None

    return results


def _parse_attendance_xls(file_obj) -> pd.DataFrame | None:
    """Parse a single-employee XLS from the attendance machine."""
    import io as _io

    try:
        filename = getattr(file_obj, "name", "")
        if isinstance(filename, tuple):
            filename = filename[0]
        filename   = str(filename)
        engine     = "openpyxl" if filename.lower().endswith(".xlsx") else "xlrd"
        file_bytes = file_obj.read() if hasattr(file_obj, "read") else file_obj
        raw        = pd.read_excel(_io.BytesIO(file_bytes), sheet_name=0, header=None, engine=engine)
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None

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
        st.error("Could not find employee name in this file.")
        return None

    data_start, data_end = 10, len(raw) - 1
    if data_end <= data_start:
        st.error(f"Not enough data rows for {employee_name}.")
        return None

    data    = raw.iloc[data_start:data_end].reset_index(drop=True)
    col_map = {1: "Date", 3: "InTime", 4: "OutTime", 6: "Shift", 7: "Total_Duration", 8: "Status"}
    df = pd.DataFrame()
    for pos, col_name in col_map.items():
        df[col_name] = (
            data.iloc[:, pos].astype(str).str.strip() if pos < data.shape[1] else ""
        )

    df = df[df["Date"].str.match(r"^\d{2}-[A-Za-z]{3}-\d{4}$", na=False)].copy()
    if df.empty:
        st.error(f"No valid date rows found for {employee_name}.")
        return None

    df["Date"] = (
        pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce").dt.strftime("%Y-%m-%d")
    )
    df = df.dropna(subset=["Date"])

    def _days_count(status: str) -> float:
        s = str(status).strip().lower().replace(" ", "")
        if s in ("present", "weeklyoff", "presentonod", "weeklyoffpresent", "holidaypresent"):
            return 1.0
        if "\u00bdpresent" in s or "half" in s or "1/2" in s:
            return 0.5
        return 0.0

    df["Days_Count"]    = df["Status"].apply(_days_count)
    df["Employee_Name"] = employee_name
    df["Source"]        = "In-Office"

    return df[["Employee_Name", "Date", "InTime", "OutTime", "Shift", "Total_Duration", "Status", "Days_Count", "Source"]]


def _parse_flat_attendance(file_obj, employee_name: str) -> pd.DataFrame | None:
    """Parse a flat XLSX exported from the attendance report page."""
    import io as _io

    try:
        file_bytes = file_obj.read() if hasattr(file_obj, "read") else file_obj
        df = pd.read_excel(_io.BytesIO(file_bytes), sheet_name=0, header=0, engine="openpyxl")
    except Exception as e:
        st.error(f"Could not read file: {e}")
        return None

    df.columns = [c.strip() for c in df.columns]
    if "Status" not in df.columns or "Date" not in df.columns:
        st.error("File does not have expected columns (Date, Status).")
        return None

    def _status_to_days(status: str) -> float:
        s = str(status).strip().lower().replace(" ", "")
        if s in ("present", "weeklyoff", "presentonod", "weeklyoffpresent", "holidaypresent"):
            return 1.0
        if "\u00bdpresent" in s or "half" in s or "1/2" in s:
            return 0.5
        return 0.0

    df["Days_Count"]    = df["Status"].apply(_status_to_days)
    df["Date"]          = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
    df                  = df.dropna(subset=["Date"])
    df["Employee_Name"] = employee_name
    df["Source"]        = df.get("Source", "In-Office")

    return df[["Employee_Name", "Date", "InTime", "OutTime", "Shift", "Total_Duration", "Status", "Days_Count", "Source"]]


# ─────────────────────────────────────────────────────────────────────────────
# Attendance helpers
# ─────────────────────────────────────────────────────────────────────────────

def _apply_sandwich_rule(df: pd.DataFrame) -> pd.DataFrame:
    """Saturday absent + Monday absent → Sunday automatically absent."""
    if df.empty:
        return df

    result_frames = []
    for emp, grp in df.groupby("Employee_Name"):
        grp = grp.copy()
        grp["_date_obj"] = pd.to_datetime(grp["Date"], errors="coerce")
        grp = grp.sort_values("_date_obj").reset_index(drop=True)
        date_to_idx = {row["_date_obj"]: i for i, row in grp.iterrows()}

        for i, row in grp.iterrows():
            d = row["_date_obj"]
            if pd.isna(d) or d.weekday() != 6:
                continue
            sat_idx = date_to_idx.get(d - pd.Timedelta(days=1))
            mon_idx = date_to_idx.get(d + pd.Timedelta(days=1))
            if sat_idx is None or mon_idx is None:
                continue
            if (
                "absent" in str(grp.at[sat_idx, "Status"]).lower()
                and "absent" in str(grp.at[mon_idx, "Status"]).lower()
            ):
                grp.at[i, "Status"]     = "Absent (Sandwich Rule)"
                grp.at[i, "Days_Count"] = 0.0

        result_frames.append(grp.drop(columns=["_date_obj"]))

    return pd.concat(result_frames, ignore_index=True) if result_frames else df


# ─────────────────────────────────────────────────────────────────────────────
# Salary calculation helpers
# ─────────────────────────────────────────────────────────────────────────────

def _auto_leave_availed(df: pd.DataFrame) -> float:
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
                total += 1.0 if s in ("present", "presentonod") else 0.5
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
    is_article: bool = False
) -> dict:
    if is_study_leave:
        return {
            "b_forward": b_forward, "cl_pl": 0.0, "sunday_c_off": 0.0,
            "leave_availed": 0.0, "add_subtract": b_forward,
            "c_forward": b_forward, "deduction": 0.0, "salary_paid": 0.0,
        }
    
    effective_cl_pl = 0.0 if is_article else cl_pl
    add_substract = b_forward + effective_cl_pl + sunday_c_off - leave_availed

    if add_substract >= 0:
        c_forward   = add_substract
        deduction   = 0.0
        salary_paid = base_salary
    else:
        c_forward   = 0.0
        deduction   = (base_salary / days_in_month) * abs(add_substract)
        salary_paid = base_salary - deduction

    return {
        "b_forward": b_forward, "cl_pl": cl_pl, "sunday_c_off": sunday_c_off,
        "leave_availed": leave_availed, "add_subtract": add_subtract,
        "c_forward": 0.0, "deduction": deduction, "salary_paid": base_salary - deduction,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Public entry-point
# ─────────────────────────────────────────────────────────────────────────────

def show_admin_module():
    if (
        not st.session_state.get("logged_in")
        or st.session_state.user_role not in ("admin", "partner")
    ):
        _show_admin_login()
        return

    role  = st.session_state.user_role
    pages = ["📊 Attendance Report", "👥 Manage Users", "📋 Salary Ledger"]
    if role == "partner":
        pages.append("💰 Salary Processing")

    page = st.sidebar.radio("Go to", pages)

    if st.sidebar.button("Logout"):
        logout()
        st.rerun()
    st.sidebar.markdown("---")

    if page == "📊 Attendance Report":
        _show_attendance_consolidation()
    elif page == "👥 Manage Users":
        _show_user_management()
    elif page == "📋 Salary Ledger":
        _show_salary_ledger()
    elif page == "💰 Salary Processing":
        _show_salary_processing()


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
            user = fetch_user_by_name(username)
            role = user["role"].strip().lower() if user else "admin"
            st.session_state.logged_in = True
            st.session_state.user_role = role
            st.session_state.user_name = username
            st.success("Login successful!")
            st.rerun()
        else:
            st.error("Invalid credentials.")


# ─────────────────────────────────────────────────────────────────────────────
# Attendance Consolidation
# ─────────────────────────────────────────────────────────────────────────────

def _show_attendance_consolidation():
    st.title("📊 Monthly Attendance Report")
    st.caption(
        "Upload the master XLS from eTimeTrackLite (all employees in one file). "
        "Map any unrecognised names to system names, save to Supabase, "
        "then generate the monthly report."
    )
    st.markdown("---")

    # ── Month selector ────────────────────────────────────────────────────────
    col1, col2 = st.columns(2)
    with col1:
        year_opts = list(range(2024, datetime.now().year + 2))
        year = st.selectbox(
            "Year", options=year_opts,
            index=year_opts.index(datetime.now().year),
        )
    with col2:
        month_num = st.selectbox(
            "Month", options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1,
        )

    month_str   = f"{year}-{month_num:02d}"
    month_label = datetime(year, month_num, 1).strftime("%B %Y")
    st.markdown("---")

    # ── Step 1: Upload & parse ────────────────────────────────────────────────
    st.subheader("Step 1 — Upload Master Attendance File")
    st.caption(
        "Export from eTimeTrackLite: Daily Attendance Report → Employee Wise → All employees."
    )

    master_file = st.file_uploader(
        "Upload master XLS", type=["xls"], label_visibility="collapsed",
    )

    if "att_name_map" not in st.session_state:
        st.session_state.att_name_map = {}

    system_names      = ["— same as file —"] + fetch_all_employee_names()
    in_office_frames: list[pd.DataFrame] = []

    if master_file:
        file_bytes = master_file.read()
        with st.spinner("Parsing master file…"):
            parsed_dict = _parse_master_attendance_xls(file_bytes)

        if not parsed_dict:
            return

        st.write(f"**{len(parsed_dict)} employee(s) found in file**")
        st.markdown("---")

        # Build a lowercase set of system names for fast matching
        system_name_set = {n.strip().lower() for n in fetch_all_employee_names()}

        for xls_name, parsed in parsed_dict.items():
            month_rows = parsed[
                parsed["Date"].str.startswith(month_str, na=False)
            ].copy()

            if month_rows.empty:
                st.warning(f"⚠️ **{xls_name}** — no rows for {month_label}. Skipped.")
                continue

            col_a, col_b = st.columns([2, 3])
            with col_a:
                st.markdown(f"**Name in file:** `{xls_name}`")
            with col_b:
                saved     = st.session_state.att_name_map.get(xls_name, "— same as file —")
                saved_idx = system_names.index(saved) if saved in system_names else 0
                chosen    = st.selectbox(
                    "Map to system name", options=system_names,
                    index=saved_idx, key=f"master_map_{xls_name}",
                    label_visibility="collapsed",
                )
                st.session_state.att_name_map[xls_name] = chosen

            final_name = xls_name if chosen == "— same as file —" else chosen

            # Skip employees not in the system (no longer employed / not tracked)
            if final_name.strip().lower() not in system_name_set:
                st.caption(f"⏭️ **{final_name}** — not found in system. Will be skipped.")
                st.markdown("---")
                continue

            month_rows["Employee_Name"] = final_name
            working_days                = month_rows["Days_Count"].sum()
            st.success(
                f"✅ **{final_name}**  |  {len(month_rows)} calendar days  |  "
                f"**{working_days:.1f} working days**"
            )
            in_office_frames.append(month_rows)
            st.markdown("---")

    in_office_df = (
        pd.concat(in_office_frames, ignore_index=True)
        if in_office_frames else pd.DataFrame()
    )

    # ── Step 2: Save to Supabase ──────────────────────────────────────────────
    st.subheader("Step 2 — Save to Supabase")

    if not in_office_df.empty:
        emp_count = in_office_df["Employee_Name"].nunique()
        st.caption(
            f"**{emp_count} employee(s)** matched to system  |  "
            f"**{len(in_office_df)} row(s)** for **{month_label}**  |  "
            "Employees not in the system have already been excluded. "
            "Rows that already exist in Supabase will be skipped automatically."
        )

        if st.button("💾 Save In-Office Attendance", type="primary", use_container_width=True):

            def _clean_time(val) -> str:
                """Return empty string for nan/None time values."""
                s = str(val).strip() if val is not None else ""
                return "" if (not s or s.lower() == "nan") else s

            rows_to_save = []
            for _, row in in_office_df.iterrows():
                rows_to_save.append({
                    "name"    : str(row["Employee_Name"]),
                    "date"    : str(row["Date"]),
                    "in_time" : _clean_time(row.get("InTime")),
                    "out_time": _clean_time(row.get("OutTime")),
                    "status"  : str(row.get("Status", "Present") or "Present"),
                })

            with st.spinner("Saving to Supabase…"):
                result = save_in_office_attendance(rows_to_save)

            if result["success"]:
                st.success(result["message"])
            else:
                st.error(result["message"])
    else:
        st.info("Upload a master XLS file above to enable saving.")

    st.markdown("---")

    # ── Step 3: Generate report from Supabase ────────────────────────────────
    st.subheader("Step 3 — Generate Monthly Report")
    st.caption(
        "Reads all saved attendance (in-office + out-office) for the selected "
        "month directly from Supabase."
    )

    if st.button("🔄 Generate Report", type="primary", use_container_width=True):
        with st.spinner("Fetching attendance from Supabase…"):
            all_att = fetch_attendance(month=month_str)

        if all_att.empty:
            st.warning(
                f"No attendance records found for {month_label}. "
                "Upload and save the master XLS first."
            )
            return

        # ── Recompute Days_Count from Status ──────────────────────────────────
        def _days_count(status: str) -> float:
            s = str(status).strip().lower().replace(" ", "")
            if s in ("present", "weeklyoff", "presentonod",
                     "weeklyoffpresent", "holidaypresent"):
                return 1.0
            if "\u00bdpresent" in s or "half" in s or "1/2" in s:
                return 0.5
            return 0.0

        all_att["Days_Count"] = all_att["Status"].apply(_days_count)

        # ── Deduplicate (same employee+date from both sources) ─────────────────
        # If an employee has both an in-office and out-office record on the same
        # day, keep both — they may have been in office in the morning and
        # out-office in the afternoon. Dedup only exact duplicates.
        combined = all_att.drop_duplicates(
            subset=["Name", "Date", "Source"]
        ).copy()

        # Rename for downstream consistency
        combined = combined.rename(columns={
            "Name"  : "Employee_Name",
            "InTime": "InTime",
            "OutTime": "OutTime",
        })

        # ── Sandwich rule ─────────────────────────────────────────────────────
        combined = _apply_sandwich_rule(combined)

        # ── Summary ───────────────────────────────────────────────────────────
        summary_rows = []
        for emp, grp in combined.groupby("Employee_Name"):
            weekly_off = int(
                grp["Status"].str.strip().str.lower()
                .str.replace(" ", "").str.contains("weeklyoff", na=False).sum()
            )
            absent = max(int((grp["Days_Count"] == 0.0).sum()) - weekly_off, 0)
            summary_rows.append({
                "Employee"          : emp,
                "Present Days"      : int((grp["Days_Count"] == 1.0).sum()),
                "Half Days"         : int((grp["Days_Count"] == 0.5).sum()),
                "Absent Days"       : absent,
                "Weekly Off"        : weekly_off,
                "Total Working Days": grp["Days_Count"].sum(),
            })
        summary = pd.DataFrame(summary_rows)

        st.markdown(f"### Summary — {month_label}")
        st.dataframe(
            summary.reset_index(drop=True),
            use_container_width=True,
            column_config={
                "Employee"          : st.column_config.TextColumn("Employee", width="large"),
                "Present Days"      : st.column_config.NumberColumn("Present", width="small"),
                "Half Days"         : st.column_config.NumberColumn("Half Days", width="small"),
                "Absent Days"       : st.column_config.NumberColumn("Absent", width="small"),
                "Weekly Off"        : st.column_config.NumberColumn("Weekly Off", width="small"),
                "Total Working Days": st.column_config.NumberColumn("Total Working Days", width="medium"),
            },
            hide_index=True,
        )

        # ── Per-employee breakdown ─────────────────────────────────────────────
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
                st.dataframe(emp_summary, use_container_width=True, hide_index=True)

                st.markdown("**Daily Breakdown**")
                display_cols = [c for c in [
                    "Date", "InTime", "OutTime", "Status",
                    "Days_Count", "Source",
                ] if c in emp_df.columns]
                st.dataframe(
                    emp_df[display_cols],
                    use_container_width=True,
                    column_config={
                        "Date"      : st.column_config.TextColumn("Date", width="medium"),
                        "InTime"    : st.column_config.TextColumn("In", width="small"),
                        "OutTime"   : st.column_config.TextColumn("Out", width="small"),
                        "Status"    : st.column_config.TextColumn("Status", width="medium"),
                        "Days_Count": st.column_config.NumberColumn("Days", width="small"),
                        "Source"    : st.column_config.TextColumn("Source", width="small"),
                    },
                    hide_index=True,
                )

                dl1, dl2 = st.columns(2)
                with dl1:
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        emp_df.to_excel(writer, index=False, sheet_name="Attendance")
                    st.download_button(
                        "⬇️ Full Report (XLSX)", data=buf.getvalue(),
                        file_name=f"{emp}_attendance_{month_str}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True, key=f"dl_full_{emp}",
                    )
                with dl2:
                    buf = io.BytesIO()
                    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                        emp_summary.to_excel(writer, index=False, sheet_name="Summary")
                    st.download_button(
                        "⬇️ Summary (XLSX)", data=buf.getvalue(),
                        file_name=f"{emp}_summary_{month_str}.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True, key=f"dl_sum_{emp}",
                    )


def _show_salary_ledger():
    st.title("📋 Salary Ledger")
    st.caption("Read-only view of processed salary records by month.")
    st.markdown("---")

    col1, col2 = st.columns(2)
    with col1:
        year_opts = list(range(2024, datetime.now().year + 2))
        year = st.selectbox(
            "Year", options=year_opts,
            index=year_opts.index(datetime.now().year), key="ledger_year",
        )
    with col2:
        month_num = st.selectbox(
            "Month", options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1, key="ledger_month",
        )

    month_str   = f"{year}-{month_num:02d}"
    month_label = datetime(year, month_num, 1).strftime("%B %Y")
    st.markdown("---")

    with st.spinner("Loading…"):
        all_employees = fetch_all_employee_names()
        ledger_df     = fetch_salary_ledger()

    if not all_employees:
        st.warning("No employees found. Add users via Manage Users first.")
        return

    month_ledger = pd.DataFrame()
    if not ledger_df.empty and "month" in ledger_df.columns:
        month_ledger = ledger_df[ledger_df["month"] == month_str].copy()

    processed_names = (
        set(month_ledger["employee"].str.strip().str.lower().tolist())
        if not month_ledger.empty else set()
    )
    processed_count   = sum(1 for e in all_employees if e.strip().lower() in processed_names)
    unprocessed_count = len(all_employees) - processed_count

    st.markdown(f"### {month_label}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Total Employees", len(all_employees))
    c2.metric("Processed", processed_count)
    c3.metric("Pending", unprocessed_count)
    st.markdown("---")

    is_partner = st.session_state.user_role == "partner"

    for emp in all_employees:
        emp_lower = emp.strip().lower()
        emp_row   = (
            month_ledger[month_ledger["employee"].str.strip().str.lower() == emp_lower]
            if not month_ledger.empty else pd.DataFrame()
        )

        if emp_row.empty:
            with st.expander(f"⏳ {emp}  —  *not yet processed*"):
                st.info(
                    f"No salary record for **{emp}** in **{month_label}**.  \n"
                    + ("Process this month via 💰 Salary Processing."
                       if is_partner
                       else "Ask a partner to process this month.")
                )
        else:
            row = emp_row.iloc[0]

            base_salary   = float(row.get("base_salary",  0) or 0)
            deduction     = float(row.get("deduction",    0) or 0)
            salary_paid   = float(row.get("salary_paid",  0) or 0)
            b_forward     = float(row.get("b_forward",    0) or 0)
            cl_pl         = float(row.get("cl_pl",        0) or 0)
            sunday_c_off  = float(row.get("sunday_c_off", 0) or 0)
            leave_availed = float(row.get("leave_availed",0) or 0)
            add_sub       = float(row.get("add_subtract",0) or 0)
            c_forward     = float(row.get("c_forward",    0) or 0)
            bank          = str(row.get("bank",           "") or "")
            study_leave   = bool(row.get("is_study_leave", False))
            days_in_month = int(row.get("days_in_month",  0) or 0)

            icon = "📚" if study_leave else ("✅" if deduction == 0 else "⚠️")
            label = (
                f"{icon} {emp}  —  ₹{salary_paid:,.2f} paid"
                + ("  *(Study Leave)*" if study_leave else "")
            )

            with st.expander(label):
                left, right = st.columns(2)

                with left:
                    st.markdown("**Leave Ledger**")
                    st.dataframe(
                        pd.DataFrame({
                            "Field": ["B/Forward (in)", "CL/PL", "Sunday/C Off",
                                      "Leave Availed", "ADD/SUBSTRACT", "C/Forward (out)"],
                            "Days" : [f"{b_forward:.1f}", f"{cl_pl:.1f}", f"{sunday_c_off:.1f}",
                                      f"{leave_availed:.1f}", f"{add_sub:.1f}", f"{c_forward:.1f}"],
                        }),
                        use_container_width=True, hide_index=True,
                    )

                with right:
                    st.markdown("**Salary**")
                    st.dataframe(
                        pd.DataFrame({
                            "Field": ["Base Salary", "Days in Month", "Deduction", "Net Paid", "Paid From"],
                            "Value": [f"₹{base_salary:,.2f}", str(days_in_month),
                                      f"₹{deduction:,.2f}", f"₹{salary_paid:,.2f}", bank],
                        }),
                        use_container_width=True, hide_index=True,
                    )

                splits_df = fetch_salary_splits(employee=emp, month=month_str)
                if not splits_df.empty:
                    st.markdown("**Payment Breakdown**")
                    st.dataframe(
                        splits_df[["recipient_name", "recipient_amount", "bank"]].rename(columns={
                            "recipient_name"  : "Recipient",
                            "recipient_amount": "Amount (₹)",
                            "bank"            : "Bank",
                        }),
                        use_container_width=True, hide_index=True,
                    )

    st.markdown("---")

    if not month_ledger.empty:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            month_ledger.to_excel(writer, index=False, sheet_name="Ledger")
        st.download_button(
            f"⬇️ Download Full Ledger — {month_label} (XLSX)",
            data=buf.getvalue(),
            file_name=f"salary_ledger_{month_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Salary Processing — partner only
# ─────────────────────────────────────────────────────────────────────────────

def _build_salary_attendance(employee: str, month_str: str) -> pd.DataFrame:
    """
    Fetch and merge in-office + out-office attendance for one employee/month
    into a single authoritative DataFrame for salary calculation.

    Merge rules (per date):
      Both in-office AND out-office row exist:
        - In-office ½Present + out-office Present → merged as Present (1.0)
        - In-office Absent   + out-office Present → merged as Present (1.0)
        - In-office Present  + out-office Present → Present (1.0), no change
        - Out-office row discarded after merge — in-office row is kept, status upgraded

      Out-office only (no in-office row for that date):
        - Status = "Present", Days_Count = 1.0
        - If date is Sunday or fixed holiday → earns comp off

      In-office only:
        - Use status as-is

    Returns DataFrame with columns:
        Date | Status | Days_Count | Source
    """
    from utils.database import fetch_attendance

    raw = fetch_attendance(month=month_str)

    if raw.empty:
        return pd.DataFrame(columns=["Date", "Status", "Days_Count", "Source"])

    # Filter to this employee (case-insensitive)
    emp_df = raw[
        raw["Name"].str.strip().str.lower() == employee.strip().lower()
    ].copy()

    if emp_df.empty:
        return pd.DataFrame(columns=["Date", "Status", "Days_Count", "Source"])

    def _days_count(status: str) -> float:
        s = str(status).strip().lower().replace(" ", "")
        if s in ("present", "weeklyoff", "presentonod",
                 "weeklyoffpresent", "holidaypresent"):
            return 1.0
        if "\u00bdpresent" in s or "half" in s or "1/2" in s:
            return 0.5
        return 0.0

    emp_df["Days_Count"] = emp_df["Status"].apply(_days_count)

    # Split by source
    in_office  = emp_df[emp_df["Source"] == "in-office"].copy()
    out_office = emp_df[emp_df["Source"] == "out-office"].copy()

    in_dates  = set(in_office["Date"].unique())
    out_dates = set(out_office["Date"].unique())

    result_rows = []

    # ── Dates with BOTH sources ───────────────────────────────────────────────
    both_dates = in_dates & out_dates
    for d in both_dates:
        in_row = in_office[in_office["Date"] == d].iloc[0]
        # Out-office presence upgrades anything less than a full day to Present
        status = str(in_row["Status"]).strip()
        s_norm = status.lower().replace(" ", "")
        is_less_than_full = (
            "absent" in s_norm
            or "\u00bdpresent" in s_norm
            or "half" in s_norm
            or "1/2" in s_norm
        )
        if is_less_than_full:
            status    = "Present"
            days      = 1.0
        else:
            days = _days_count(status)
        result_rows.append({
            "Date"      : d,
            "Status"    : status,
            "Days_Count": days,
            "Source"    : "in-office+out-office",
        })

    # ── In-office only dates ──────────────────────────────────────────────────
    for d in in_dates - out_dates:
        in_row = in_office[in_office["Date"] == d].iloc[0]
        result_rows.append({
            "Date"      : d,
            "Status"    : str(in_row["Status"]).strip(),
            "Days_Count": float(in_row["Days_Count"]),
            "Source"    : "in-office",
        })

    # ── Out-office only dates ─────────────────────────────────────────────────
    for d in out_dates - in_dates:
        # Check if Sunday or fixed holiday → comp off
        try:
            d_obj    = date.fromisoformat(d)
            is_hol   = _is_holiday(d_obj)
            status   = "WeeklyOffPresent" if d_obj.weekday() == 6 else \
                       ("HolidayPresent"  if is_hol else "Present")
        except ValueError:
            status = "Present"
        result_rows.append({
            "Date"      : d,
            "Status"    : status,
            "Days_Count": 1.0,
            "Source"    : "out-office",
        })

    if not result_rows:
        return pd.DataFrame(columns=["Date", "Status", "Days_Count", "Source"])

    result = pd.DataFrame(result_rows).sort_values("Date").reset_index(drop=True)
    return result


def _show_salary_processing():
    st.title("💰 Salary Processing")
    st.markdown(
        "Select an employee and month. Attendance is fetched from Supabase "
        "automatically. Review the calculated values, adjust if needed, then save to the ledger."
    )
    st.markdown("---")

    for key, default in [
        ("sal_split_count",    0),
        ("sal_calc_result",    None),
        ("sal_xls_df",         None),
        ("sal_xls_employee",   ""),
        ("sal_last_selection", ""),   # tracks employee|month to detect real changes
    ]:
        if key not in st.session_state:
            st.session_state[key] = default

    employee_names = fetch_all_employee_names()
    if not employee_names:
        st.warning("No associates found. Add them via Manage Users first.")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        employee = st.selectbox("Employee", options=employee_names, key="sal_employee")
    with col2:
        year_opts = list(range(2024, datetime.now().year + 2))
        sal_year  = st.selectbox(
            "Year", options=year_opts,
            index=year_opts.index(datetime.now().year), key="sal_year",
        )
    with col3:
        sal_month = st.selectbox(
            "Month", options=list(range(1, 13)),
            format_func=lambda m: datetime(2000, m, 1).strftime("%B"),
            index=datetime.now().month - 1, key="sal_month",
        )

    month_str     = f"{sal_year}-{sal_month:02d}"
    month_label   = datetime(sal_year, sal_month, 1).strftime("%B %Y")
    days_in_month = calendar.monthrange(sal_year, sal_month)[1]

    if ledger_month_exists(employee, month_str):
        st.warning(
            f"⚠️ A ledger record already exists for **{employee}** in **{month_label}**. "
            "Saving again will be blocked. Delete the existing row from Supabase to re-process."
        )

    st.markdown("---")

    is_article = False
    try:
        users    = fetch_users()
        user_row = users[users["name"].str.strip().str.lower() == employee.strip().lower()]
        if not user_row.empty:
            is_article = user_row.iloc[0]["role"].strip().lower() == "article"
    except Exception:
        pass

    # ── 1: Fetch attendance from Supabase ─────────────────────────────────────
    st.subheader("1 — Attendance Data")
    st.caption(
        "Fetched directly from Supabase. In-office and out-office rows are "
        "merged automatically — half-day + out-office = full day."
    )

    month_df           = pd.DataFrame()
    auto_leave_availed = 0.0
    auto_sunday_c_off  = 0.0

    # Only re-fetch and re-seed when employee or month actually changes.
    # Without this guard, every widget interaction triggers a rerun which
    # re-seeds session state and resets any manual edits the partner made.
    current_selection = f"{employee}|{month_str}"
    selection_changed = st.session_state.get("sal_last_selection") != current_selection

    with st.spinner(f"Fetching attendance for {employee} — {month_label}…"):
        month_df = _build_salary_attendance(employee, month_str)

    if month_df.empty:
        st.warning(
            f"No attendance records found for **{employee}** in **{month_label}**. "
            "Make sure the monthly XLS has been uploaded and saved to Supabase first."
        )
    else:
        auto_leave_availed = _auto_leave_availed(month_df)
        auto_sunday_c_off  = _auto_sunday_c_off(month_df)

        # Only overwrite session state if employee/month changed — not on every rerun
        if selection_changed:
            st.session_state["sal_leave_availed"] = float(auto_leave_availed)
            st.session_state["sal_sc_off_base"]   = float(auto_sunday_c_off)
            st.session_state["sal_extra_adj"]     = 0.0
            st.session_state["sal_last_selection"] = current_selection

        st.success(
            f"✅ **{employee}** — {len(month_df)} day(s) found  |  "
            f"Leave Availed: **{auto_leave_availed}**  |  "
            f"Sunday/C Off: **{auto_sunday_c_off}**"
        )

        with st.expander("View merged attendance rows"):
            st.dataframe(
                month_df[["Date", "Status", "Days_Count", "Source"]],
                use_container_width=True,
                column_config={
                    "Date"      : st.column_config.TextColumn("Date", width="medium"),
                    "Status"    : st.column_config.TextColumn("Status", width="medium"),
                    "Days_Count": st.column_config.NumberColumn("Days", width="small"),
                    "Source"    : st.column_config.TextColumn("Source", width="medium"),
                },
                hide_index=True,
            )

    st.markdown("---")

    # ── 2: Study Leave ────────────────────────────────────────────────────────
    st.subheader("2 — Study Leave")
    is_study_leave = st.toggle(
        "This is a Study Leave month (no salary paid, no CL/PL accrual, balance carries through)",
        value=False, key="sal_study_leave",
    )
    st.markdown("---")

    # ── 3: Leave Parameters ───────────────────────────────────────────────────
    st.subheader("3 — Leave Parameters")
    if is_study_leave:
        st.info("Study Leave month — all leave fields zeroed. Only B/Forward carries through.")

    b_forward_auto = fetch_carry_forward(employee, month_str)

    # Only pre-seed b_forward when employee/month changes, not on every rerun
    if selection_changed:
        st.session_state["sal_b_forward"] = float(b_forward_auto)

    col_a, col_b = st.columns(2)
    with col_a:
        b_forward = st.number_input(
            "B/Forward (auto-loaded from previous month)",
            value=float(b_forward_auto), step=0.5, format="%.1f",
            disabled=is_study_leave, key="sal_b_forward",
        )
    with col_b:
        cl_pl_value = 0.0 if (is_study_leave or is_article) else 1.5
        st.number_input(
            "CL/PL Eligible (0 — Article, 1.5 — Employee)",
            value=cl_pl_value, step=0.5, format="%.1f",
            disabled=True, key="sal_cl_pl",
        )

    col_c, col_d, col_e = st.columns(3)
    with col_c:
        sunday_c_off_base = st.number_input(
            "Sunday/C Off (auto from Supabase)",
            value=0.0 if is_study_leave else float(
                st.session_state.get("sal_sc_off_base", auto_sunday_c_off)
            ),
            step=0.5, format="%.1f", disabled=is_study_leave, key="sal_sc_off_base",
        )
    with col_d:
        extra_adj = st.number_input(
            "Extra Holiday Adjustment (±)",
            value=float(st.session_state.get("sal_extra_adj", 0.0)),
            step=0.5, format="%.1f", disabled=is_study_leave, key="sal_extra_adj",
        )
    with col_e:
        sunday_c_off_final = max(0.0, sunday_c_off_base + extra_adj) if not is_study_leave else 0.0
        st.number_input("Final Sunday/C Off", value=sunday_c_off_final,
                        disabled=True, format="%.1f", key="sal_sc_off_final")

    leave_availed = st.number_input(
        "Leave Availed (auto from XLS — edit if needed)",
        value=0.0 if is_study_leave else float(
            st.session_state.get("sal_leave_availed", auto_leave_availed)
        ),
        step=0.5, min_value=0.0, format="%.1f",
        disabled=is_study_leave, key="sal_leave_availed",
    )
    st.markdown("---")

    # ── 4: Salary & Bank ──────────────────────────────────────────────────────
    st.subheader("4 — Salary & Bank")
    col_f, col_g = st.columns(2)
    with col_f:
        user_record   = fetch_user_by_name(employee)
        stored_salary = float(user_record.get("base_salary", 0.0)) if user_record else 0.0
        # Pre-seed only when employee/month changes, same guard as leave parameters
        if selection_changed:
            st.session_state["sal_base"] = stored_salary
        base_salary   = st.number_input(
            "Base Salary (₹)", min_value=0.0, step=500.0,
            value=stored_salary, format="%.2f", key="sal_base",
        )
    with col_g:
        try:
            bank_options = list(st.secrets["banks"]["accounts"])
        except Exception:
            bank_options = ["FF/JSB", "M&RFSPL/UBI", "PTBS/UBI", "MCHLLP/KOTAK", "MCHLLP/HDFC", "AUSM/HDFC", "MAJMUDAR", "OWFSPL/UBI", "FAPL/UBI", "VSPL/UBI", "MCO/KOTAK"]
        bank = st.selectbox("Pay From (Entity/Bank)", options=bank_options, key="sal_bank")

    st.markdown("---")

    # ── 5: Calculation Preview ────────────────────────────────────────────────
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
            is_article = is_article
        )
        if is_study_leave:
            st.info("📚 Study Leave — no salary paid. Balance carries through.")
        elif result["add_subtract"] >= 0:
            st.success(f"✅ No deduction — **{result['add_subtract']:.1f} days** carry forward.")
        else:
            st.error(
                f"⚠️ Deduction of **₹{result['deduction']:,.2f}** applied. "
                f"({base_salary:,.0f} ÷ {days_in_month} × {abs(result['add_subtract']):.1f})"
            )
        st.session_state.sal_calc_result = result
    else:
        st.info("Enter a base salary above to see the calculation preview.")
        st.session_state.sal_calc_result = None

    st.markdown("---")

    # ── 6: Salary Split ───────────────────────────────────────────────────────
    st.subheader("6 — Salary Split")
    st.caption("If the salary is paid to multiple recipients, press Split for each one.")

    btn1, btn2 = st.columns(2)
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
        hc = st.columns([3, 2, 2])
        hc[0].markdown("**Recipient Name**")
        hc[1].markdown("**Amount (₹)**")
        hc[2].markdown("**Bank Account**")

        for i in range(st.session_state.sal_split_count):
            rc = st.columns([3, 2, 2])
            with rc[0]:
                rec_name = st.text_input(f"n{i}", key=f"sp_name_{i}",
                                         label_visibility="collapsed", placeholder=f"Recipient {i+1}")
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
                    label_visibility="collapsed",
                )

            splits.append({"name": rec_name, "amount": rec_amount, "bank":rec_bank})
            running_total += rec_amount

        if base_salary > 0 and st.session_state.sal_calc_result:
            diff = st.session_state.sal_calc_result["salary_paid"] - running_total
            if abs(diff) < 0.01:
                st.success(f"✅ Split total ₹{running_total:,.2f} matches Salary Paid.")
            elif diff > 0:
                st.warning(f"⚠️ Unallocated: ₹{diff:,.2f}")
            else:
                st.error(f"❌ Over-allocated by ₹{abs(diff):,.2f}")

    st.markdown("---")

    # ── 7: Save ───────────────────────────────────────────────────────────────
    st.subheader("7 — Save to Ledger")

    if st.button("💾 Save Salary Record", type="primary", use_container_width=True):
        if base_salary <= 0:
            st.error("Enter a base salary > 0 before saving.")
            st.stop()
        if st.session_state.sal_calc_result is None:
            st.error("Calculation result is missing. Check your inputs.")
            st.stop()
        if any(not s["name"].strip() or s["amount"] <= 0 for s in splits):
            st.error("Each split row needs a recipient name and amount > 0.")
            st.stop()

        result = st.session_state.sal_calc_result
        with st.spinner("Saving…"):
            ledger_result = save_salary_ledger_row(
                employee=employee, month=month_str,
                b_forward=result["b_forward"], cl_pl=result["cl_pl"],
                sunday_c_off=result["sunday_c_off"], leave_availed=result["leave_availed"],
                add_subtract=result["add_subtract"], c_forward=result["c_forward"],
                base_salary=base_salary, days_in_month=days_in_month,
                deduction=result["deduction"], salary_paid=result["salary_paid"],
                bank=bank, is_study_leave=is_study_leave,
            )
            if not ledger_result["success"]:
                st.error(ledger_result["message"])
                st.stop()
            if splits:
                splits_result = save_salary_splits(employee, month_str, splits)
                if not splits_result["success"]:
                    st.warning(f"Ledger saved, but splits failed: {splits_result['message']}")

        st.success(ledger_result["message"])
        st.balloons()
        st.session_state.sal_split_count = 0
        st.session_state.sal_calc_result = None


# ─────────────────────────────────────────────────────────────────────────────
# User Management — admin + partner
# ─────────────────────────────────────────────────────────────────────────────

def _show_user_management():
    st.title("👥 Manage Users")
    st.caption("Create and manage login credentials for all users.")
    st.markdown("---")

    is_partner = st.session_state.user_role == "partner"

    st.subheader("Add New User")
    with st.form("add_user_form"):
        col1, col2 = st.columns(2)
        with col1:
            new_name  = st.text_input("Full Name", placeholder="e.g. Aparna Pradyot Maitra")
            new_pin   = st.text_input("3-Digit PIN", type="password", max_chars=3, placeholder="• • •")
            new_role  = st.selectbox("Role", options=["employee", "article", "admin", "partner"])
            new_email = st.text_input("Email Address", placeholder="e.g. aparna@office.com")
        with col2:
            new_salary           = st.number_input("Base Salary (₹)", min_value=0.0, step=500.0, format="%.2f")
            new_process_salary   = st.toggle("Process Salary", value=True,
                                              help="Include in salary processing dropdown")
            new_track_attendance = st.toggle("Track Attendance", value=True,
                                              help="Include in attendance login dropdown")
        submitted = st.form_submit_button("Add User", use_container_width=True)

    if submitted:
        if not new_name.strip():
            st.error("Please enter a name.")
        elif len(new_pin) != 3 or not new_pin.isdigit():
            st.error("PIN must be exactly 3 digits.")
        else:
            result = add_user(
                name=new_name.strip(), pin=new_pin.strip(), role=new_role,
                email=new_email.strip(), base_salary=new_salary,
                process_salary=new_process_salary, track_attendance=new_track_attendance,
            )
            if result["success"]:
                st.success(result["message"])
            else:
                st.error(result["message"])

    st.markdown("---")
    st.subheader("Current Users")

    try:
        users = fetch_users()
        if users.empty:
            st.info("No users added yet.")
        else:
            for _, row in users.iterrows():
                with st.expander(f"**{row['name']}** — {row['role']}"):
                    c1, c2, c3 = st.columns(3)
                    c1.markdown(f"**Email:** {row.get('email', '—')}")
                    c2.markdown(f"**Salary:** ₹{float(row.get('base_salary', 0)):,.2f}")
                    c3.markdown(
                        f"**Salary Processing:** {'✅' if row.get('process_salary') else '❌'}  \n"
                        f"**Attendance:** {'✅' if row.get('track_attendance') else '❌'}"
                    )
                    if is_partner:
                        with st.form(f"edit_salary_{row['name']}"):
                            new_sal = st.number_input(
                                "Update Base Salary (₹)",
                                value=float(row.get("base_salary", 0)),
                                step=500.0, format="%.2f", key=f"sal_edit_{row['name']}",
                            )
                            if st.form_submit_button("Update Salary", use_container_width=True):
                                res = update_user_salary(row["name"], new_sal)
                                st.success(res["message"]) if res["success"] else st.error(res["message"])
    except Exception as e:
        st.error(f"Could not load users: {e}")  
