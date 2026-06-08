"""
app.py
──────
Entry point for the Attendance & Salary Streamlit application.

Run with:
    streamlit run app.py

Role routing:
    employee / article  →  Attendance check-in + payslip only
    partner             →  Attendance check-in + Admin panel (all pages incl. Salary Processing)
    admin               →  Admin panel only (Attendance Report, Manage Users, Salary Ledger)
"""

import streamlit as st
from utils.auth import init_session
from modules.attendance import show_attendance_module
from modules.admin import show_admin_module

st.set_page_config(
    page_title="Attendance System",
    page_icon="📋",
    layout="centered",
    initial_sidebar_state="auto",
)

st.markdown(
    """
    <style>
        html, body, [class*="css"] { font-size: 16px; }
        #MainMenu { visibility: hidden; }
        footer    { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


def main():
    init_session()

    if not st.session_state.logged_in:
        _show_landing()
        return

    role = st.session_state.user_role

    if role in ("employee", "article"):
        show_attendance_module()

    elif role == "partner":
        _show_partner_layout()

    elif role == "admin":
        show_admin_module()


def _show_partner_layout():
    """
    Partner sees two top-level sections in the sidebar:
      📍 My Attendance  →  check-in / check-out (same as employee)
      🔧 Admin Panel    →  all admin pages + salary processing
    """
    name = st.session_state.user_name
    st.sidebar.title(f"👋 {name}")
    st.sidebar.caption("Role: Partner")
    st.sidebar.markdown("---")

    section = st.sidebar.radio("Module", ["📍 My Attendance", "🔧 Admin Panel"])

    if section == "📍 My Attendance":
        show_attendance_module()
    else:
        show_admin_module()


def _show_landing():
    """
    Landing page for unauthenticated users.
    Tab 1: employee / article / partner login → attendance check-in
    Tab 2: admin / partner login → admin panel
    """
    st.title("📋 Attendance System")
    st.caption("Out-Office Attendance & Salary Management")
    st.markdown("---")

    tab1, tab2 = st.tabs(["👤  Mark Attendance", "🔐  Admin"])

    with tab1:
        show_attendance_module()

    with tab2:
        show_admin_module()


if __name__ == "__main__":
    main()
