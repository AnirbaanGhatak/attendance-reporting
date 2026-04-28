"""
utils/gsheets.py
────────────────
All Google Sheets I/O lives here so the rest of the app stays clean.

Spreadsheet tabs:
  • users                  – name | pin | role
  • out_office_attendance  – Name | Date | Time | Latitude | Longitude
  • salary_ledger          – Employee | Month | B_Forward | CL_PL |
                             Sunday_C_Off | Leave_Availed | Add_Substract |
                             C_Forward | Base_Salary | Days_In_Month |
                             Deduction | Salary_Paid | Bank |
                             Is_Study_Leave | Saved_At
  • salary_splits          – Employee | Month | Recipient_Name |
                             Recipient_Amount | Bank | Saved_At
"""

from __future__ import annotations

from datetime import datetime, date

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials


# ── GCP scopes ────────────────────────────────────────────────────────────────
_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# ── Tab names (must match Google Sheet exactly) ───────────────────────────────
TAB_USERS      = "users"
TAB_ATTENDANCE = "out_office_attendance"
TAB_LEDGER     = "salary_ledger"
TAB_SPLITS     = "salary_splits"

# ── salary_ledger column order ─────────────────────────────────────────────────
LEDGER_HEADERS = [
    "Employee", "Month", "B_Forward", "CL_PL",
    "Sunday_C_Off", "Leave_Availed", "Add_Substract",
    "C_Forward", "Base_Salary", "Days_In_Month",
    "Deduction", "Salary_Paid", "Bank",
    "Is_Study_Leave", "Saved_At",
]

SPLITS_HEADERS = [
    "Employee", "Month", "Recipient_Name",
    "Recipient_Amount", "Bank", "Saved_At",
]


# ─────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource(ttl=300)
def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _get_worksheet(tab: str) -> gspread.Worksheet:
    client = _get_client()
    sh = client.open_by_key(st.secrets["sheets"]["spreadsheet_id"])
    return sh.worksheet(tab)


def _ensure_header(ws: gspread.Worksheet, headers: list[str]) -> None:
    """Write header row only if the sheet is completely empty."""
    if not ws.row_values(1):
        ws.append_row(headers)


# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────

def fetch_users() -> pd.DataFrame:
    ws      = _get_worksheet(TAB_USERS)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=["name", "pin", "role"])
    return pd.DataFrame(records)


def add_user(name: str, pin: str, role: str = "associate") -> None:
    ws = _get_worksheet(TAB_USERS)
    _ensure_header(ws, ["name", "pin", "role"])
    ws.append_row([name, pin, role])


def verify_associate(name: str, pin: str) -> bool:
    try:
        users = fetch_users()
        match = users[
            (users["name"].str.strip().str.lower() == name.strip().lower()) &
            (users["pin"].astype(str).str.strip()  == pin.strip()) &
            (users["role"].str.strip().str.lower()  == "associate")
        ]
        return not match.empty
    except Exception:
        return False


def get_associate_names() -> list[str]:
    try:
        users      = fetch_users()
        associates = users[users["role"].str.strip().str.lower() == "associate"]
        return sorted(associates["name"].tolist())
    except Exception:
        return []


def fetch_all_employee_names() -> list[str]:
    return get_associate_names()


# ─────────────────────────────────────────────────────────────────────────────
# Out-office Attendance
# ─────────────────────────────────────────────────────────────────────────────

def mark_attendance(name: str, latitude: float, longitude: float) -> dict:
    today = date.today().isoformat()
    now   = datetime.now().strftime("%H:%M:%S")
    try:
        ws = _get_worksheet(TAB_ATTENDANCE)
        _ensure_header(ws, ["Name", "Date", "Time", "Latitude", "Longitude"])
        for row in ws.get_all_records():
            if (
                str(row.get("Name", "")).strip().lower() == name.strip().lower()
                and str(row.get("Date", "")).strip() == today
            ):
                return {
                    "success": False,
                    "message": f"Attendance already marked for {name} on {today}.",
                }
        ws.append_row([name, today, now, latitude, longitude])
        return {
            "success": True,
            "message": f"✅ Attendance marked for {name} on {today} at {now}.",
        }
    except Exception as e:
        return {"success": False, "message": f"Error writing to sheet: {e}"}


def fetch_out_office_attendance(month: str | None = None) -> pd.DataFrame:
    try:
        ws      = _get_worksheet(TAB_ATTENDANCE)
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=["Name", "Date", "Time", "Latitude", "Longitude"])
        df         = pd.DataFrame(records)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        if month:
            df = df[df["Date"].dt.strftime("%Y-%m") == month]
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        return df
    except Exception as e:
        st.error(f"Could not fetch attendance data: {e}")
        return pd.DataFrame()


# ─────────────────────────────────────────────────────────────────────────────
# Salary Ledger
# ─────────────────────────────────────────────────────────────────────────────

def fetch_salary_ledger() -> pd.DataFrame:
    """Return the full salary_ledger sheet as a DataFrame."""
    try:
        ws      = _get_worksheet(TAB_LEDGER)
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=LEDGER_HEADERS)
        return pd.DataFrame(records)
    except Exception as e:
        st.error(f"Could not fetch salary ledger: {e}")
        return pd.DataFrame(columns=LEDGER_HEADERS)


def fetch_carry_forward(employee: str, month: str) -> float:
    """
    Return the C_Forward value from the most recent saved record for this
    employee that comes BEFORE the given month (YYYY-MM).

    Returns 0.0 if no prior record exists.
    """
    try:
        ledger = fetch_salary_ledger()
        if ledger.empty:
            return 0.0

        emp_rows = ledger[
            ledger["Employee"].str.strip().str.lower() == employee.strip().lower()
        ].copy()

        if emp_rows.empty:
            return 0.0

        # Keep only months strictly before the requested month
        emp_rows = emp_rows[emp_rows["Month"] < month]

        if emp_rows.empty:
            return 0.0

        # Most recent prior month
        latest = emp_rows.sort_values("Month").iloc[-1]
        return float(latest.get("C_Forward", 0.0) or 0.0)
    except Exception:
        return 0.0


def ledger_month_exists(employee: str, month: str) -> bool:
    """True if a record already exists for this employee + month."""
    try:
        ledger = fetch_salary_ledger()
        if ledger.empty:
            return False
        match = ledger[
            (ledger["Employee"].str.strip().str.lower() == employee.strip().lower()) &
            (ledger["Month"] == month)
        ]
        return not match.empty
    except Exception:
        return False


def save_salary_ledger_row(
    employee: str,
    month: str,
    b_forward: float,
    cl_pl: float,
    sunday_c_off: float,
    leave_availed: float,
    add_substract: float,
    c_forward: float,
    base_salary: float,
    days_in_month: int,
    deduction: float,
    salary_paid: float,
    bank: str,
    is_study_leave: bool,
) -> dict:
    """
    Append one row to salary_ledger.
    Rejects duplicate employee+month entries.
    """
    try:
        ws = _get_worksheet(TAB_LEDGER)
        _ensure_header(ws, LEDGER_HEADERS)

        if ledger_month_exists(employee, month):
            return {
                "success": False,
                "message": (
                    f"A record for {employee} / {month} already exists. "
                    "Delete it from the sheet first if you want to re-save."
                ),
            }

        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ws.append_row([
            employee, month, b_forward, cl_pl,
            sunday_c_off, leave_availed, add_substract,
            c_forward, base_salary, days_in_month,
            round(deduction, 2), round(salary_paid, 2), bank,
            "Yes" if is_study_leave else "No",
            saved_at,
        ])
        return {"success": True, "message": f"✅ Ledger saved for {employee} — {month}."}
    except Exception as e:
        return {"success": False, "message": f"Error saving ledger: {e}"}


# ─────────────────────────────────────────────────────────────────────────────
# Salary Splits
# ─────────────────────────────────────────────────────────────────────────────

def save_salary_splits(
    employee: str,
    month: str,
    bank: str,
    splits: list[dict],  # [{"name": str, "amount": float}, ...]
) -> dict:
    """
    Write one row per split recipient into salary_splits.
    If splits is empty this is a no-op (full amount goes to employee directly).
    """
    if not splits:
        return {"success": True, "message": "No splits to save."}
    try:
        ws = _get_worksheet(TAB_SPLITS)
        _ensure_header(ws, SPLITS_HEADERS)
        saved_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for sp in splits:
            ws.append_row([
                employee, month,
                sp.get("name", ""),
                round(float(sp.get("amount", 0)), 2),
                bank, saved_at,
            ])
        return {"success": True, "message": f"✅ {len(splits)} split(s) saved."}
    except Exception as e:
        return {"success": False, "message": f"Error saving splits: {e}"}
