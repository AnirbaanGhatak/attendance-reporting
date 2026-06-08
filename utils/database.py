"""
utils/database.py
─────────────────
All database I/O via Supabase (PostgreSQL).

Supabase tables:
    users           – id | name | pin | role | email | base_salary |
                      process_salary | track_attendance | created_at

    attendance      – id | name | date | company | in_time | out_time |
                      in_latitude | in_longitude | out_latitude |
                      out_longitude | source | status | created_at

                      source: "in-office" | "out-office"
                      status: eTimeTrackLite status for in-office rows
                              (Present, WeeklyOff, ½Present, Absent, etc.)
                              "Present" for all out-office rows

    salary_ledger   – id | employee | month | b_forward | cl_pl |
                      sunday_c_off | leave_availed | add_subtract |
                      c_forward | base_salary | days_in_month |
                      deduction | salary_paid | bank |
                      is_study_leave | saved_at

    salary_splits   – id | employee | month | recipient_name |
                      recipient_amount | bank | saved_at

Roles:
    partner   – full access, salary processing, attendance check-in
    admin     – attendance report upload, manage users, salary ledger view
    employee  – attendance check-in + payslip
    article   – attendance check-in + payslip, fixed holiday bank, no CL/PL

Flags per user:
    process_salary    – appears in salary processing dropdown if True
    track_attendance  – appears in attendance login dropdown if True
"""

from __future__ import annotations

from datetime import datetime, date, timezone, timedelta

import pandas as pd
import streamlit as st
from supabase import create_client, Client

# ── IST timezone ──────────────────────────────────────────────────────────────
IST = timezone(timedelta(hours=5, minutes=30))

# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────

def _get_client() -> Client:
    """Return a Supabase client using service_role key (bypasses RLS)."""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["key"]
    return create_client(url, key)

# ─────────────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────────────

def fetch_users() -> pd.DataFrame:
    """Return all users as a DataFrame."""
    try:
        client = _get_client()
        res    = client.table("users").select("*").order("name").execute()
        if not res.data:
            return pd.DataFrame(columns=[
                "id", "name", "pin", "role", "email",
                "base_salary", "process_salary", "track_attendance", "created_at",
            ])
        return pd.DataFrame(res.data)
    except Exception as e:
        st.error(f"Could not fetch users: {e}")
        return pd.DataFrame()


def fetch_user_by_name(name: str) -> dict | None:
    """Return a single user record as a dict, or None if not found."""
    try:
        client = _get_client()
        res    = client.table("users") \
                       .select("*") \
                       .ilike("name", name.strip()) \
                       .limit(1) \
                       .execute()
        return res.data[0] if res.data else None
    except Exception:
        return None


def add_user(
    name: str,
    pin: str,
    role: str              = "employee",
    email: str             = "",
    base_salary: float     = 0.0,
    process_salary: bool   = True,
    track_attendance: bool = True,
) -> dict:
    """
    Insert a new user.
    Returns {"success": bool, "message": str}.
    """
    try:
        client = _get_client()
        client.table("users").insert({
            "name"            : name.strip(),
            "pin"             : pin.strip(),
            "role"            : role.strip().lower(),
            "email"           : email.strip(),
            "base_salary"     : base_salary,
            "process_salary"  : process_salary,
            "track_attendance": track_attendance,
        }).execute()
        return {"success": True, "message": f"✅ '{name}' added as {role}."}
    except Exception as e:
        return {"success": False, "message": f"Error adding user: {e}"}


def update_user_salary(name: str, base_salary: float) -> dict:
    """
    Partner-only — update base salary for a user.
    Returns {"success": bool, "message": str}.
    """
    try:
        client = _get_client()
        client.table("users") \
              .update({"base_salary": base_salary}) \
              .ilike("name", name.strip()) \
              .execute()
        return {"success": True, "message": f"✅ Salary updated for {name}."}
    except Exception as e:
        return {"success": False, "message": f"Error updating salary: {e}"}


def update_user(name: str, updates: dict) -> dict:
    """
    Generic user update — pass a dict of columns to update.
    Returns {"success": bool, "message": str}.
    """
    try:
        client = _get_client()
        client.table("users") \
              .update(updates) \
              .ilike("name", name.strip()) \
              .execute()
        return {"success": True, "message": f"✅ User '{name}' updated."}
    except Exception as e:
        return {"success": False, "message": f"Error updating user: {e}"}


def verify_associate(name: str, pin: str) -> bool:
    """
    Return True if name + pin match a user with track_attendance = True.
    Accepts all roles — partner, admin, employee, article.
    """
    try:
        client = _get_client()
        res    = client.table("users") \
                       .select("pin, track_attendance") \
                       .ilike("name", name.strip()) \
                       .execute()
        if not res.data:
            return False
        for row in res.data:
            if (
                str(row["pin"]).strip() == pin.strip()
                and row.get("track_attendance", True)
            ):
                return True
        return False
    except Exception:
        return False


def verify_admin(username: str, password: str) -> bool:
    """
    Verify admin or partner login.
    Checks secrets.toml fallback first, then users table (PIN as password).
    """
    try:
        try:
            if (
                username.strip() == st.secrets["admin"]["username"]
                and password.strip() == st.secrets["admin"]["password"]
            ):
                return True
        except Exception:
            pass

        client = _get_client()
        res    = client.table("users") \
                       .select("pin, role") \
                       .ilike("name", username.strip()) \
                       .execute()
        if not res.data:
            return False
        for row in res.data:
            if (
                str(row["pin"]).strip() == password.strip()
                and row["role"].strip().lower() in ("admin", "partner")
            ):
                return True
        return False
    except Exception:
        return False


def get_user_role(name: str) -> str:
    """Return the role for a user, defaulting to 'employee'."""
    try:
        user = fetch_user_by_name(name)
        return str(user.get("role", "employee")).strip().lower() if user else "employee"
    except Exception:
        return "employee"


def get_associate_names() -> list[str]:
    """
    Return sorted list of names where track_attendance = True.
    Used for the attendance login dropdown.
    """
    try:
        client = _get_client()
        res    = client.table("users") \
                       .select("name") \
                       .eq("track_attendance", True) \
                       .order("name") \
                       .execute()
        return [r["name"] for r in res.data] if res.data else []
    except Exception:
        return []


def fetch_all_employee_names() -> list[str]:
    """
    Return sorted list of names where process_salary = True.
    Used for the salary processing dropdown.
    """
    try:
        client = _get_client()
        res    = client.table("users") \
                       .select("name") \
                       .eq("process_salary", True) \
                       .order("name") \
                       .execute()
        return [r["name"] for r in res.data] if res.data else []
    except Exception:
        return []

# ─────────────────────────────────────────────────────────────────────────────
# Attendance — out-office (real-time check-in / check-out)
# ─────────────────────────────────────────────────────────────────────────────

def mark_attendance(
    name: str,
    company: str,
    latitude: float,
    longitude: float,
) -> dict:
    """
    Insert a new out-office check-in row.
    Blocks if there is an open entry (checked in, not yet out).
    Returns {"success": bool, "message": str}.
    """
    today = datetime.now(IST).date().isoformat()
    now   = datetime.now(IST).strftime("%H:%M:%S")
    try:
        client = _get_client()

        # Block if open entry exists
        existing = client.table("attendance") \
                         .select("id") \
                         .ilike("name", name.strip()) \
                         .eq("date", today) \
                         .eq("source", "out-office") \
                         .is_("out_time", "null") \
                         .execute()

        if existing.data:
            return {
                "success": False,
                "message": "You have an open check-in. Please check out first.",
            }

        client.table("attendance").insert({
            "name"        : name.strip(),
            "date"        : today,
            "company"     : company.strip(),
            "in_time"     : now,
            "out_time"    : None,
            "in_latitude" : latitude,
            "in_longitude": longitude,
            "source"      : "out-office",
            "status"      : "Present",
        }).execute()

        return {"success": True, "message": f"✅ Checked in at {now}."}
    except Exception as e:
        return {"success": False, "message": f"Error: {e}"}


def mark_checkout(
    name: str,
    latitude: float,
    longitude: float,
) -> dict:
    """
    Update the open out-office check-in row with out_time and coordinates.
    Returns {"success": bool, "message": str}.
    """
    today = datetime.now(IST).date().isoformat()
    now   = datetime.now(IST).strftime("%H:%M:%S")
    try:
        client = _get_client()

        open_entry = client.table("attendance") \
                           .select("id") \
                           .ilike("name", name.strip()) \
                           .eq("date", today) \
                           .eq("source", "out-office") \
                           .is_("out_time", "null") \
                           .execute()

        if not open_entry.data:
            return {
                "success": False,
                "message": "No open check-in found. Please check in first.",
            }

        row_id = open_entry.data[0]["id"]

        client.table("attendance") \
              .update({
                  "out_time"     : now,
                  "out_latitude" : latitude,
                  "out_longitude": longitude,
              }) \
              .eq("id", row_id) \
              .execute()

        return {"success": True, "message": f"✅ Checked out at {now}."}
    except Exception as e:
        return {"success": False, "message": f"Error: {e}"}


def get_today_status(name: str) -> dict:
    """
    Return today's attendance state for an out-office user.
    state: "none" | "checked_in" | "checked_out"
    Also returns trip count, last in/out times, and company.
    """
    today = datetime.now(IST).date().isoformat()
    try:
        client = _get_client()
        res    = client.table("attendance") \
                       .select("*") \
                       .ilike("name", name.strip()) \
                       .eq("date", today) \
                       .eq("source", "out-office") \
                       .order("id", desc=False) \
                       .execute()

        if not res.data:
            return {"state": "none", "in_time": "", "out_time": "", "company": "", "trips": 0}

        rows            = res.data
        completed_trips = sum(1 for r in rows if r.get("out_time"))
        open_entry      = next((r for r in rows if not r.get("out_time")), None)

        if open_entry:
            return {
                "state"   : "checked_in",
                "in_time" : str(open_entry.get("in_time",  "") or ""),
                "out_time": "",
                "company" : str(open_entry.get("company",  "") or ""),
                "trips"   : completed_trips,
            }
        else:
            last = rows[-1]
            return {
                "state"   : "checked_out",
                "in_time" : str(last.get("in_time",  "") or ""),
                "out_time": str(last.get("out_time", "") or ""),
                "company" : str(last.get("company",  "") or ""),
                "trips"   : completed_trips,
            }
    except Exception:
        return {"state": "none", "in_time": "", "out_time": "", "company": "", "trips": 0}

# ─────────────────────────────────────────────────────────────────────────────
# Attendance — in-office (admin XLS upload → saved to Supabase)
# ─────────────────────────────────────────────────────────────────────────────

def save_in_office_attendance(rows: list[dict]) -> dict:
    """
    Insert in-office attendance rows parsed from the master XLS.
    Each row must have: name, date, in_time, out_time, status.

    Deduplication: skips any row where (name, date, source="in-office")
    already exists in the table to prevent double-saving on re-upload.

    Returns {"success": bool, "inserted": int, "skipped": int, "message": str}.
    """
    if not rows:
        return {"success": True, "inserted": 0, "skipped": 0, "message": "No rows to save."}

    try:
        client = _get_client()

        # Fetch all existing in-office rows for the months covered by this upload
        # to build a dedup set without per-row queries
        dates = {r["date"] for r in rows}
        min_date = min(dates)
        max_date = max(dates)

        existing_res = client.table("attendance") \
                             .select("name, date") \
                             .eq("source", "in-office") \
                             .gte("date", min_date) \
                             .lte("date", max_date) \
                             .execute()

        existing_set: set[tuple[str, str]] = set()
        if existing_res.data:
            for r in existing_res.data:
                existing_set.add((
                    str(r["name"]).strip().lower(),
                    str(r["date"]).strip(),
                ))

        to_insert = []
        skipped   = 0

        for row in rows:
            key = (str(row["name"]).strip().lower(), str(row["date"]).strip())
            if key in existing_set:
                skipped += 1
                continue
            def _clean_time(val) -> str | None:
                """Return None for empty/nan time values, string otherwise."""
                s = str(val).strip() if val is not None else ""
                if not s or s.lower() == "nan" or s == "":
                    return None
                return s

            to_insert.append({
                "name"    : str(row["name"]).strip(),
                "date"    : str(row["date"]).strip(),
                "company" : "",
                "in_time" : _clean_time(row.get("in_time")),
                "out_time": _clean_time(row.get("out_time")),
                "in_latitude"  : None,
                "in_longitude" : None,
                "out_latitude" : None,
                "out_longitude": None,
                "source"  : "in-office",
                "status"  : str(row.get("status", "Present")).strip(),
            })

        if to_insert:
            # Insert in batches of 500 to stay within Supabase limits
            batch_size = 500
            for i in range(0, len(to_insert), batch_size):
                client.table("attendance").insert(to_insert[i:i + batch_size]).execute()

        inserted = len(to_insert)
        msg = f"✅ Saved {inserted} row(s) to Supabase."
        if skipped:
            msg += f" {skipped} row(s) skipped (already exist)."

        return {"success": True, "inserted": inserted, "skipped": skipped, "message": msg}

    except Exception as e:
        return {"success": False, "inserted": 0, "skipped": 0, "message": f"Error saving attendance: {e}"}

# ─────────────────────────────────────────────────────────────────────────────
# Attendance — fetch (used by report generation + salary processing)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_attendance(
    month: str | None = None,
    source: str | None = None,
) -> pd.DataFrame:
    """
    Fetch attendance rows from Supabase.

    Args:
        month:  filter to YYYY-MM (optional)
        source: "in-office" | "out-office" | None (both)

    Returns DataFrame with columns:
        Name | Date | Company | InTime | OutTime |
        In_Latitude | In_Longitude | Out_Latitude | Out_Longitude |
        Source | Status
    """
    try:
        import calendar as _calendar
        client = _get_client()
        query  = client.table("attendance").select("*")

        if month:
            year, mon = int(month.split("-")[0]), int(month.split("-")[1])
            last_day  = _calendar.monthrange(year, mon)[1]
            query     = query.gte("date", f"{month}-01") \
                             .lte("date", f"{month}-{last_day:02d}")

        if source:
            query = query.eq("source", source)

        res = query.order("date").order("name").execute()

        if not res.data:
            return pd.DataFrame(columns=[
                "Name", "Date", "Company", "InTime", "OutTime",
                "In_Latitude", "In_Longitude", "Out_Latitude", "Out_Longitude",
                "Source", "Status",
            ])

        df = pd.DataFrame(res.data).rename(columns={
            "name"         : "Name",
            "date"         : "Date",
            "company"      : "Company",
            "in_time"      : "InTime",
            "out_time"     : "OutTime",
            "in_latitude"  : "In_Latitude",
            "in_longitude" : "In_Longitude",
            "out_latitude" : "Out_Latitude",
            "out_longitude": "Out_Longitude",
            "source"       : "Source",
            "status"       : "Status",
        })

        df["Date"] = pd.to_datetime(df["Date"], errors="coerce").dt.strftime("%Y-%m-%d")
        return df

    except Exception as e:
        st.error(f"Could not fetch attendance: {e}")
        return pd.DataFrame()


def fetch_out_office_attendance(month: str | None = None) -> pd.DataFrame:
    """
    Convenience wrapper — fetch out-office rows only.
    Kept for backward compatibility with any future callers.
    """
    return fetch_attendance(month=month, source="out-office")

# ─────────────────────────────────────────────────────────────────────────────
# Salary Ledger
# ─────────────────────────────────────────────────────────────────────────────

def fetch_salary_ledger(employee: str | None = None) -> pd.DataFrame:
    """
    Fetch salary ledger rows, optionally filtered by employee name.
    """
    try:
        client = _get_client()
        query  = client.table("salary_ledger").select("*").order("month")
        if employee:
            query = query.ilike("employee", employee.strip())
        res = query.execute()
        if not res.data:
            return pd.DataFrame()
        return pd.DataFrame(res.data)
    except Exception as e:
        st.error(f"Could not fetch salary ledger: {e}")
        return pd.DataFrame()


def fetch_carry_forward(employee: str, month: str) -> float:
    """
    Return c_forward from the most recent saved ledger month
    strictly before the given month (YYYY-MM).
    Returns 0.0 if no prior record exists.
    """
    try:
        client = _get_client()
        res    = client.table("salary_ledger") \
                       .select("month, c_forward") \
                       .ilike("employee", employee.strip()) \
                       .lt("month", month) \
                       .order("month", desc=True) \
                       .limit(1) \
                       .execute()
        if res.data:
            return float(res.data[0].get("c_forward", 0.0) or 0.0)
        return 0.0
    except Exception as e:
        st.warning(f"Could not fetch carry-forward for {employee}: {e}")
        return 0.0


def ledger_month_exists(employee: str, month: str) -> bool:
    """Return True if a ledger record exists for this employee + month."""
    try:
        client = _get_client()
        res    = client.table("salary_ledger") \
                       .select("id") \
                       .ilike("employee", employee.strip()) \
                       .eq("month", month) \
                       .execute()
        return bool(res.data)
    except Exception:
        return False


def save_salary_ledger_row(
    employee: str,
    month: str,
    b_forward: float,
    cl_pl: float,
    sunday_c_off: float,
    leave_availed: float,
    add_subtract: float,
    c_forward: float,
    base_salary: float,
    days_in_month: int,
    deduction: float,
    salary_paid: float,
    bank: str,
    is_study_leave: bool,
) -> dict:
    """
    Insert one row into salary_ledger.
    Rejects duplicate employee + month entries.
    Returns {"success": bool, "message": str}.
    """
    if ledger_month_exists(employee, month):
        return {
            "success": False,
            "message": (
                f"A record for {employee} / {month} already exists. "
                "Delete it from Supabase first to re-save."
            ),
        }
    try:
        client   = _get_client()
        saved_at = datetime.now(IST).isoformat()
        client.table("salary_ledger").insert({
            "employee"      : employee.strip(),
            "month"         : month,
            "b_forward"     : b_forward,
            "cl_pl"         : cl_pl,
            "sunday_c_off"  : sunday_c_off,
            "leave_availed" : leave_availed,
            "add_subtract" : add_subtract,
            "c_forward"     : c_forward,
            "base_salary"   : base_salary,
            "days_in_month" : days_in_month,
            "deduction"     : round(deduction, 2),
            "salary_paid"   : round(salary_paid, 2),
            "bank"          : bank,
            "is_study_leave": is_study_leave,
            "saved_at"      : saved_at,
        }).execute()
        return {"success": True, "message": f"✅ Ledger saved for {employee} — {month}."}
    except Exception as e:
        return {"success": False, "message": f"Error saving ledger: {e}"}

# ─────────────────────────────────────────────────────────────────────────────
# Salary Splits
# ─────────────────────────────────────────────────────────────────────────────

def save_salary_splits(
    employee: str,
    month: str,
    splits: list[dict],
) -> dict:
    """
    Insert one row per split recipient into salary_splits.
    Returns {"success": bool, "message": str}.
    """
    if not splits:
        return {"success": True, "message": "No splits to save."}
    try:
        client   = _get_client()
        saved_at = datetime.now(IST).isoformat()
        rows     = [
            {
                "employee"        : employee.strip(),
                "month"           : month,
                "recipient_name"  : sp.get("name", "").strip(),
                "recipient_amount": round(float(sp.get("amount", 0)), 2),
                "bank"            : sp.get("bank", "").strip(),
                "saved_at"        : saved_at,
            }
            for sp in splits
        ]
        client.table("salary_splits").insert(rows).execute()
        return {"success": True, "message": f"✅ {len(splits)} split(s) saved."}
    except Exception as e:
        return {"success": False, "message": f"Error saving splits: {e}"}


def fetch_salary_splits(employee: str, month: str) -> pd.DataFrame:
    """Fetch salary splits for a given employee and month."""
    try:
        client = _get_client()
        res    = client.table("salary_splits") \
                       .select("*") \
                       .ilike("employee", employee.strip()) \
                       .eq("month", month) \
                       .execute()
        if not res.data:
            return pd.DataFrame()
        return pd.DataFrame(res.data)
    except Exception as e:
        st.error(f"Could not fetch splits: {e}")
        return pd.DataFrame()