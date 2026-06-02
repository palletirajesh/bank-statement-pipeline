"""Startup compatibility shim for Streamlit geo access control.

app.py currently calls enforce_geo_access() before the local function is defined.
Python falls back to builtins for unresolved names, so this file safely provides
that function at interpreter startup and keeps the live app from crashing.
"""

from __future__ import annotations

import builtins
import json
import logging
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

_ALLOWED_COUNTRY = "Canada"
_ALLOWED_REGION_NAME = "Ontario"
_ALLOWED_REGION_CODE = "ON"


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
        fields = "status,message,country,countryCode,region,regionName,city,query"
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
    """Allow only visitors geolocated to Ontario, Canada."""
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
    is_ontario = (
        region_name.casefold() == _ALLOWED_REGION_NAME.casefold()
        or region_code.casefold() == _ALLOWED_REGION_CODE.casefold()
    )

    if not (is_canada and is_ontario):
        st.error("Access restricted. This application is currently available only in Ontario, Canada.")
        detected = ", ".join(x for x in [city, region_name or region_code, country or country_code] if x)
        if detected:
            st.caption(f"Detected location: {detected}")
        st.stop()


builtins.enforce_geo_access = enforce_geo_access
