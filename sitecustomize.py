"""Startup compatibility helpers for the Streamlit app.

This file is loaded by Python at interpreter startup when the repository root is
on PYTHONPATH / the service working directory. It provides two compatibility
helpers without requiring app.py to be rewritten immediately:

1. Define enforce_geo_access() early so app.py can call it before its local
   function definition is reached.
2. Inject a public visit counter above the app title, aligned to the right.
"""

from __future__ import annotations

import builtins
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_ALLOWED_COUNTRY = "Canada"
_TRACK_DB = "access_log.db"


def _safe_str(value, default: str = "") -> str:
    return default if value is None else str(value)


def _get_client_ip(st) -> str:
    try:
        headers = dict(st.context.headers)
        headers_lc = {str(k).lower(): v for k, v in headers.items()}

        xff = headers_lc.get("x-forwarded-for")
        if xff:
            return _safe_str(xff).split(",")[0].strip()

        real_ip = headers_lc.get("x-real-ip")
        if real_ip:
            return _safe_str(real_ip).strip()
    except Exception as exc:
        logger.debug("Could not read Streamlit headers: %s", exc)

    try:
        ip = _safe_str(getattr(st.context, "ip_address", ""))
        if ip:
            return ip
    except Exception as exc:
        logger.debug("Could not read Streamlit context IP: %s", exc)

    return "local"


def _get_user_agent(st) -> str:
    try:
        headers = dict(st.context.headers)
        headers_lc = {str(k).lower(): v for k, v in headers.items()}
        return _safe_str(headers_lc.get("user-agent", ""))
    except Exception:
        return ""


def _is_local_ip(ip: str) -> bool:
    return (
        not ip
        or ip == "local"
        or ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip == "::1"
    )


def _lookup_location(ip: str) -> dict:
    if _is_local_ip(ip):
        return {}

    try:
        fields = "status,message,country,countryCode,region,regionName,city,zip,lat,lon,isp,query"
        req = Request(f"http://ip-api.com/json/{ip}?fields={fields}")
        data = json.loads(urlopen(req, timeout=3).read())
        if data.get("status") != "success":
            logger.warning("Geo lookup failed for %s: %s", ip, data.get("message", "unknown error"))
            return {}
        return data
    except Exception as exc:
        logger.warning("Geo lookup failed for %s: %s", ip, exc)
        return {}


def enforce_geo_access() -> None:
    """Allow only visitors geolocated to Canada."""
    import streamlit as st

    ip = _get_client_ip(st)

    # Local development should not be blocked.
    if _is_local_ip(ip):
        return

    data = _lookup_location(ip)
    country = _safe_str(data.get("country"))
    country_code = _safe_str(data.get("countryCode"))
    region_name = _safe_str(data.get("regionName"))
    region_code = _safe_str(data.get("region"))
    city = _safe_str(data.get("city"))

    is_canada = country.casefold() == _ALLOWED_COUNTRY.casefold() or country_code.casefold() == "ca"

    if not is_canada:
        st.error("Access restricted. This application is currently available only in Canada.")
        detected = ", ".join(x for x in [city, region_name or region_code, country or country_code] if x)
        if detected:
            st.caption(f"Detected location: {detected}")
        st.stop()


def _ensure_access_log_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc        TEXT NOT NULL,
            session_id    TEXT,
            ip            TEXT,
            city          TEXT,
            region        TEXT,
            country       TEXT,
            action        TEXT NOT NULL,
            document_name TEXT,
            user_agent    TEXT,
            zip           TEXT,
            lat           TEXT,
            lon           TEXT,
            isp           TEXT
        )
        """
    )
    conn.commit()


def _record_site_visit_once(st) -> None:
    """Count one public website visit per Streamlit browser session."""
    if st.session_state.get("site_visit_recorded"):
        return

    try:
        ip = _get_client_ip(st)
        user_agent = _get_user_agent(st)
        session_id = st.session_state.get("tracker_session_id")
        if not session_id:
            session_id = str(uuid.uuid4())
            st.session_state["tracker_session_id"] = session_id

        data = _lookup_location(ip)
        city = _safe_str(data.get("city"))
        region = _safe_str(data.get("regionName"))
        country = _safe_str(data.get("country"))
        zip_code = _safe_str(data.get("zip"))
        lat = _safe_str(data.get("lat"))
        lon = _safe_str(data.get("lon"))
        isp = _safe_str(data.get("isp"))

        conn = sqlite3.connect(_TRACK_DB, check_same_thread=False, timeout=10)
        _ensure_access_log_table(conn)
        conn.execute(
            """
            INSERT INTO access_log
                (ts_utc, session_id, ip, city, region, country, action,
                 document_name, user_agent, zip, lat, lon, isp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                session_id,
                ip,
                city,
                region,
                country,
                "site_visit",
                "",
                user_agent,
                zip_code,
                lat,
                lon,
                isp,
            ),
        )
        conn.commit()
        conn.close()
        st.session_state["site_visit_recorded"] = True
    except Exception as exc:
        logger.warning("record_site_visit_once failed: %s", exc)


def _get_total_site_visits() -> int:
    try:
        conn = sqlite3.connect(_TRACK_DB, check_same_thread=False, timeout=10)
        _ensure_access_log_table(conn)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM access_log WHERE action = 'site_visit'")
        count = cur.fetchone()[0] or 0
        conn.close()
        return int(count)
    except Exception as exc:
        logger.warning("get_total_site_visits failed: %s", exc)
        return 0


def _render_public_visit_counter(st, markdown_func) -> None:
    _record_site_visit_once(st)
    total_visits = _get_total_site_visits()
    markdown_func(
        f"""
        <div style="display:flex;justify-content:flex-end;width:100%;margin-bottom:.4rem;">
            <div style="
                display:inline-flex;
                align-items:center;
                gap:7px;
                background:#ffffff;
                border:1px solid #e6edf5;
                border-radius:999px;
                padding:7px 13px;
                font-size:.86rem;
                color:#344054;
                box-shadow:0 1px 2px rgba(16,24,40,.04);
            ">
                👀 Total visits: <strong>{total_visits:,}</strong>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _install_streamlit_visit_counter_patch() -> None:
    """Inject the public counter immediately before the hero title is rendered."""
    try:
        import streamlit as st
    except Exception as exc:
        logger.debug("Streamlit not available for visit counter patch: %s", exc)
        return

    original_markdown = st.markdown
    if getattr(original_markdown, "_gensolutions_visit_counter_patch", False):
        return

    def patched_markdown(body, *args, **kwargs):
        try:
            should_inject = (
                isinstance(body, str)
                and "hero-title" in body
                and "Bank Statement Processor" in body
            )
            if should_inject:
                _render_public_visit_counter(st, original_markdown)
        except Exception as exc:
            logger.warning("Visit counter render failed: %s", exc)

        return original_markdown(body, *args, **kwargs)

    patched_markdown._gensolutions_visit_counter_patch = True
    st.markdown = patched_markdown


builtins.enforce_geo_access = enforce_geo_access
_install_streamlit_visit_counter_patch()
