"""
utils/auth.py
─────────────
Session-state helpers for login / logout.
"""

import streamlit as st
from utils.database import verify_admin


def init_session():
    defaults = {
        "logged_in"      : False,
        "user_role"      : None,
        "user_name"      : None,
        "sal_split_count": 0,
    }
    for key, val in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = val


def login_associate(name: str, role: str = "employee"):
    st.session_state.logged_in = True
    st.session_state.user_role = role
    st.session_state.user_name = name


def login_admin(name: str, role: str = "admin"):
    st.session_state.logged_in = True
    st.session_state.user_role = role
    st.session_state.user_name = name


def logout():
    st.session_state.logged_in       = False
    st.session_state.user_role       = None
    st.session_state.user_name       = None
    st.session_state.sal_split_count = 0
