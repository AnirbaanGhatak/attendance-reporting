"""
utils/auth.py
─────────────
Session-state helpers for login / logout.
"""

import streamlit as st


def init_session():
    defaults = {
        "logged_in": False,
        "user_role": None,          # "associate" | "admin"
        "user_name": None,
        "split_count": 0,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def login_associate(name: str):
    st.session_state.logged_in = True
    st.session_state.user_role = "associate"
    st.session_state.user_name = name


def login_admin(name: str):
    st.session_state.logged_in = True
    st.session_state.user_role = "admin"
    st.session_state.user_name = name


def logout():
    for key in ["logged_in", "user_role", "user_name", "split_count"]:
        st.session_state[key] = None if key != "split_count" else 0
    st.session_state.logged_in = False


def verify_admin(username: str, password: str) -> bool:
    try:
        return (
            username.strip() == st.secrets["admin"]["username"]
            and password.strip() == st.secrets["admin"]["password"]
        )
    except Exception:
        return False
