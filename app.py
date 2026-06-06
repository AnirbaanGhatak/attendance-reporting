"""
app.py
──────
Entry point for the Attendance & Salary Streamlit application.

Run with:
    streamlit run app.py

The app routes to one of two modules based on which login tab is used:
  • Associate Login  →  Module 1: Out-Office Attendance
  • Admin Login      →  Module 2: Salary Processing & Reports
"""

import streamlit as st
from utils.auth import init_session
from modules.attendance import show_attendance_module
from modules.admin import show_admin_module


# ── Page config (must be the very first Streamlit call) ──────────────────────
st.set_page_config(
    page_title="Attendance System",
    page_icon="📋",
    layout="centered",
    initial_sidebar_state="auto",
)


# ── Minimal global CSS ────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
        /* Slightly larger base font for readability on mobile */
        html, body, [class*="css"] { font-size: 16px; }
        /* Remove Streamlit's default hamburger menu */
        #MainMenu { visibility: hidden; }
        footer    { visibility: hidden; }
    </style>
    """,
    unsafe_allow_html=True,
)


def main():

    init_session()

    if st.session_state.logged_in:
        role = st.session_state.user_role

        if role in ["employee", "article"]:
            # Attendance only — no admin panel at all
            show_attendance_module()

        elif role in ["admin", "partner"]:
            # Show both attendance and admin via sidebar
            _show_authenticated_layout()
    else:
        _show_landing()


        
def _show_authenticated_layout():
    """
    Authenticated layout for admin and partner.
    Sidebar lets them switch between attendance and admin panel.
    """
    role = st.session_state.user_role
    name = st.session_state.user_name

    st.sidebar.title(f"👋 {name}")
    st.sidebar.caption(f"Role: {role.capitalize()}")
    st.sidebar.markdown("---")

    section = st.sidebar.radio(
        "Module",
        ["📍 My Attendance", "🔧 Admin Panel"],
    )

    if section == "📍 My Attendance":
        show_attendance_module()
    else:
        show_admin_module()



def _show_landing():
    st.title("📋 Attendance System")
    st.caption("Out-Office Attendance Tracking Management")
    st.markdown("---")

    tab1, tab2 = st.tabs(["👤  Users — Mark Attendance", "🔐  Admin"])

    with tab1:
        # Delegates to the attendance module's login section
        show_attendance_module()

    with tab2:
        # Delegates to the admin module's login section
        show_admin_module()


if __name__ == "__main__":
    main()
