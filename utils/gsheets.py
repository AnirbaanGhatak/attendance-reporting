"""
utils/gsheets.py
────────────────
All Google Sheets I/O lives here so the rest of the app stays clean.

Expected sheet tabs inside the one Spreadsheet:
  • users                  – Name | PIN | Role
  • out_office_attendance  – Name | Date | Time | Latitude | Longitude
  • salary_data            – Employee | Month | Base_Salary | Bank |
                             Recipient_Name | Recipient_Amount | Saved_At
"""

from __future__ import annotations

import streamlit as st
import gspread
import pandas as pd
from google.oauth2.service_account import Credentials
from datetime import datetime, date


# ── GCP scopes ────────────────────────────────────────────────────────────────
_SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

# ── Sheet tab names ────────────────────────────────────────────────────────────
TAB_USERS       = "users"
TAB_ATTENDANCE  = "out_office_attendance"
TAB_SALARY      = "salary_data"


# ── Connection helpers ─────────────────────────────────────────────────────────

@st.cache_resource(ttl=300)
def _get_client() -> gspread.Client:
    """Return an authorised gspread client (cached for 5 minutes)."""
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]),
        scopes=_SCOPES,
    )
    return gspread.authorize(creds)


def _get_worksheet(tab: str) -> gspread.Worksheet:
    client = _get_client()
    spreadsheet_id = st.secrets["sheets"]["spreadsheet_id"]
    sh = client.open_by_key(spreadsheet_id)
    return sh.worksheet(tab)


# ── Users ──────────────────────────────────────────────────────────────────────

def fetch_users() -> pd.DataFrame:
    """
    Returns DataFrame with columns: name, pin, role
    Creates the tab + header row if it doesn't exist yet.
    """
    ws = _get_worksheet(TAB_USERS)
    records = ws.get_all_records()
    if not records:
        return pd.DataFrame(columns=["name", "pin", "role"])
    return pd.DataFrame(records)


def add_user(name: str, pin: str, role: str = "associate") -> None:
    """Append a new user row (plain PIN stored – advise hashing for production)."""
    ws = _get_worksheet(TAB_USERS)
    # Ensure header exists
    if not ws.row_values(1):
        ws.append_row(["name", "pin", "role"])
    ws.append_row([name, pin, role])


def verify_associate(name: str, pin: str) -> bool:
    """Return True if name+pin match a row with role='associate'."""
    try:
        users = fetch_users()
        match = users[
            (users["name"].str.strip().str.lower() == name.strip().lower()) &
            (users["pin"].astype(str).str.strip() == pin.strip()) &
            (users["role"].str.strip().str.lower() == "associate")
        ]
        return not match.empty
    except Exception:
        return False


def get_associate_names() -> list[str]:
    """Return sorted list of all associate names."""
    try:
        users = fetch_users()
        associates = users[users["role"].str.strip().str.lower() == "associate"]
        return sorted(associates["name"].tolist())
    except Exception:
        return []


# ── Out-office Attendance ──────────────────────────────────────────────────────

def mark_attendance(
    name: str,
    latitude: float,
    longitude: float,
) -> dict:
    """
    Append today's attendance for `name`.
    Returns {"success": bool, "message": str}
    """
    today = date.today().isoformat()          # YYYY-MM-DD
    now   = datetime.now().strftime("%H:%M:%S")

    # Duplicate check – same name + same date
    try:
        ws = _get_worksheet(TAB_ATTENDANCE)
        if not ws.row_values(1):
            ws.append_row(["Name", "Date", "Time", "Latitude", "Longitude"])

        existing = ws.get_all_records()
        for row in existing:
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
    """
    Fetch all out-office attendance records.
    Optionally filter by month string in 'YYYY-MM' format.
    """
    try:
        ws = _get_worksheet(TAB_ATTENDANCE)
        records = ws.get_all_records()
        if not records:
            return pd.DataFrame(columns=["Name", "Date", "Time", "Latitude", "Longitude"])
        df = pd.DataFrame(records)
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        if month:
            df = df[df["Date"].dt.strftime("%Y-%m") == month]
        df["Date"] = df["Date"].dt.strftime("%Y-%m-%d")
        return df
    except Exception as e:
        st.error(f"Could not fetch attendance data: {e}")
        return pd.DataFrame()


# ── Salary Data ────────────────────────────────────────────────────────────────

def save_salary_record(
    employee: str,
    month: str,
    base_salary: float,
    bank: str,
    splits: list[dict],  # [{"name": str, "amount": float}, ...]
    saved_at: str | None = None,
) -> dict:
    """
    Write one row per split recipient.
    If splits is empty, write a single row with Recipient_Name = employee.
    Returns {"success": bool, "message": str}
    """
    try:
        ws = _get_worksheet(TAB_SALARY)
        if not ws.row_values(1):
            ws.append_row([
                "Employee", "Month", "Base_Salary",
                "Bank", "Recipient_Name", "Recipient_Amount", "Saved_At"
            ])

        saved_at = saved_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not splits:
            ws.append_row([employee, month, base_salary, bank, employee, base_salary, saved_at])
        else:
            for split in splits:
                ws.append_row([
                    employee,
                    month,
                    base_salary,
                    bank,
                    split.get("name", ""),
                    split.get("amount", 0),
                    saved_at,
                ])
        return {"success": True, "message": f"✅ Salary record saved for {employee}."}
    except Exception as e:
        return {"success": False, "message": f"Error saving salary: {e}"}


def fetch_all_employee_names() -> list[str]:
    """Pull unique employee names from the users sheet (associates)."""
    return get_associate_names()
