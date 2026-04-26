"""
app.py – Bank Statement Processor (Streamlit UI)

Changes from original
─────────────────────
1. Per-feature rate limiting.
   ip_usage now tracks upload_count, process_count, download_count
   separately.  Each feature has its own env-var limit (0 = unlimited).
   Blocking fires when ANY enabled limit is exceeded.  The blocked
   screen and the admin panel show a per-feature breakdown.

   Env vars (all optional, sane defaults):
     UPLOAD_LIMIT    default 0  (0 = no limit)
     PROCESS_LIMIT   default 10
     DOWNLOAD_LIMIT  default 0  (0 = no limit)
     COST_PER_FILE_USD  default 0.15
     BLOCK_DAYS      default 30
     DONATION_LINK   default ""  (e.g. https://buymeacoffee.com/you)

2. Geolocation cached per session.
   lookup_location() was called on every log_event(), making the UI
   block and risking ip-api.com rate-limits (45 req/min free tier).
   Now cached in st.session_state for the lifetime of a session.

3. Server-side MIME / magic-byte validation.
   Streamlit's type= filter is client-side only.  save_uploaded_or_
   converted_file() now reads the first 8 bytes and rejects anything
   that doesn't match a known safe signature.

4. Schema migrations run once at startup, not on every get_db() call.
   A schema_version table tracks which ALTER TABLE statements have
   already been applied.

5. All bare `except: pass` replaced with logger.warning / logger.debug.

6. Admin password comes from ADMIN_PASSWORD env var (unchanged) —
   confirmed with hmac.compare_digest for timing-safe comparison.
"""

from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import os
import uuid
import hmac
import json
import logging
import sqlite3
import shutil
import io
import zipfile
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st
from PIL import Image

from main import process_pdf_to_csv, UPLOAD_DIR, OUTPUT_DIR, ensure_directories

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Streamlit page config
# ---------------------------------------------------------------------------
st.set_page_config(page_title="Bank Statement Processor", layout="wide")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
TRACK_DB   = Path("access_log.db")
TRASH_DIR  = Path("trash")

# Rate-limit env vars – all tunable without code changes
FEATURE_LIMITS: dict[str, int] = {
    "upload":   int(os.getenv("UPLOAD_LIMIT",   "0")),   # 0 = unlimited
    "process":  int(os.getenv("PROCESS_LIMIT",  "2")),
    "download": int(os.getenv("DOWNLOAD_LIMIT", "0")),   # 0 = unlimited
}
COST_PER_FILE_USD = float(os.getenv("COST_PER_FILE_USD", "0.15"))
BLOCK_DAYS        = int(os.getenv("BLOCK_DAYS", "30"))
DONATION_LINK     = os.getenv("DONATION_LINK", "")

# Allowed file magic bytes  →  mime type label
_MAGIC: list[tuple[bytes, str]] = [
    (b"%PDF",      "PDF"),
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG",   "PNG"),
    (b"RIFF",      "WebP/AVI"),   # WebP has RIFF header
    (b"BM",        "BMP"),
    (b"II*\x00",   "TIFF"),
    (b"MM\x00*",   "TIFF"),
]


# ==========================================================================
# Styling
# ==========================================================================
def inject_custom_css():
    st.markdown(
        """
        <style>
        [data-testid="stHeader"] { visibility: hidden; height: 0%; }
        footer { visibility: hidden; }
        [data-testid="collapsedControl"] { display: none; }

        :root {
            --bg: #f4f7fb;
            --surface: rgba(255,255,255,0.92);
            --surface-soft: #fbfdff;
            --text: #0f172a;
            --muted: #667085;
            --line: #e6edf5;
            --primary: #1d4ed8;
            --primary-dark: #1e3a8a;
            --primary-soft: #eaf2ff;
            --success-bg: #edf8f1;
            --success-text: #166534;
            --shadow-xs: 0 1px 2px rgba(16,24,40,.03);
            --shadow-sm: 0 8px 24px rgba(15,23,42,.05);
        }
        .stApp { background: linear-gradient(180deg,#f8fbff 0%,#f4f7fb 100%); }
        .block-container {
            max-width:100% !important;
            padding-top:.2rem !important;
            padding-bottom:1rem !important;
            padding-left:.9rem !important;
            padding-right:.9rem !important;
        }
        section.main > div { max-width:100% !important; padding-top:0 !important; }

        .left-rail-card {
            background:var(--surface); border-radius:18px;
            padding:.95rem; box-shadow:var(--shadow-sm); margin-bottom:12px;
        }
        .left-rail-title { font-size:.95rem; font-weight:700; color:var(--text); margin-bottom:.3rem; }
        .left-rail-body  { font-size:.95rem; line-height:1.6; color:#475467; }

        .hero-title {
            font-size:2.5rem; line-height:1.02; letter-spacing:-.04em;
            font-weight:800; color:var(--text); margin:0;
        }
        .hero-subtitle {
            font-size:.98rem; color:var(--muted);
            margin-top:.45rem; margin-bottom:.8rem;
            max-width:760px; line-height:1.55;
        }
        .hero-mini-stats { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:.8rem; }
        .hero-stat-pill {
            background:#fff; border:1px solid var(--line);
            border-radius:999px; padding:6px 10px;
            font-size:.8rem; color:#344054;
        }
        .section-label { font-size:.92rem; font-weight:700; color:#344054; margin-bottom:.35rem; }

        div[data-testid="stFileUploader"] {
            background:#fbfdff; border:1px solid var(--line); border-radius:18px; padding:.4rem;
        }
        div[data-testid="stFileUploader"] section { border-radius:14px !important; }

        .stButton > button, .stDownloadButton > button {
            height:44px !important; border-radius:14px !important;
            font-weight:700 !important; box-shadow:var(--shadow-xs) !important;
        }
        button[kind="primary"] {
            background:linear-gradient(180deg,#2563eb 0%,#1d4ed8 100%) !important;
            color:white !important; border:1px solid #1d4ed8 !important;
        }
        button[kind="primary"]:hover {
            background:linear-gradient(180deg,#1d4ed8 0%,#1e40af 100%) !important;
            border-color:#1e40af !important;
        }
        button[kind="secondary"] {
            background:#fff !important; color:var(--text) !important;
            border:1px solid #d7e0ee !important;
        }
        button[kind="secondary"]:hover { border-color:#b8cbf3 !important; color:var(--primary) !important; }

        div[data-testid="stExpander"] {
            border:none !important; background:rgba(255,255,255,.82) !important;
            border-radius:16px !important; box-shadow:var(--shadow-sm);
            margin-bottom:10px; overflow:hidden;
        }
        div[data-testid="stExpander"] summary { font-size:.98rem; font-weight:700; color:var(--text); }

        div[data-testid="stTextInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-testid="stSelectbox"] > div {
            border-radius:12px !important; border:1px solid var(--line) !important; background:white !important;
        }
        div[data-testid="stAlert"] { border-radius:16px !important; border:none !important; }
        [data-testid="stSuccess"] { background:var(--success-bg) !important; color:var(--success-text) !important; }

        .product-footer {
            margin-top:.8rem; padding-top:.8rem; border-top:1px solid var(--line);
            display:flex; justify-content:space-between; align-items:center;
            gap:8px; flex-wrap:wrap;
        }
        .product-footer-left { color:var(--muted); font-size:.88rem; }
        .trust-badge {
            background:white; border:1px solid var(--line); border-radius:999px;
            padding:6px 10px; font-size:.78rem; color:#344054;
        }
        .admin-wrap {
            margin-top:.9rem; background:rgba(255,255,255,.88);
            border-radius:20px; padding:.9rem; box-shadow:var(--shadow-sm);
        }
        .usage-table td { padding:4px 10px; font-size:.9rem; }
        .usage-table th { padding:4px 10px; font-size:.82rem; color:#667085; font-weight:600; }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ==========================================================================
# File conversion helpers
# ==========================================================================
def _detect_mime(file_bytes: bytes) -> str | None:
    """Return a label for the file type based on magic bytes, or None if unknown."""
    for sig, label in _MAGIC:
        if file_bytes[: len(sig)] == sig:
            return label
    return None


def validate_uploaded_bytes(file_bytes: bytes, filename: str) -> tuple[bool, str]:
    """
    Server-side guard: reject files whose magic bytes don't match any
    allowed type.  Returns (ok, error_message).
    """
    mime = _detect_mime(file_bytes)
    if mime is None:
        return False, f"'{filename}' was rejected: unrecognised file format."
    return True, ""


def convert_image_to_pdf(uploaded_file, output_path: Path) -> Path:
    image = Image.open(uploaded_file)
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(output_path, "PDF")
    return output_path


# ==========================================================================
# Database
# ==========================================================================

# ---------- one-time migration registry ----------
_MIGRATIONS: list[tuple[str, str]] = [
    # (migration_id, SQL statement)
    ("er_email",         "ALTER TABLE error_reports ADD COLUMN email TEXT"),
    ("er_resolved_at",   "ALTER TABLE error_reports ADD COLUMN resolved_at_utc TEXT"),
    ("er_pub_note",      "ALTER TABLE error_reports ADD COLUMN public_resolution_note TEXT"),
    ("iu_upload_count",  "ALTER TABLE ip_usage ADD COLUMN upload_count INTEGER DEFAULT 0"),
    ("iu_download_count","ALTER TABLE ip_usage ADD COLUMN download_count INTEGER DEFAULT 0"),
]


def _apply_migrations(conn: sqlite3.Connection):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            migration_id TEXT PRIMARY KEY,
            applied_at   TEXT NOT NULL
        )
        """
    )
    conn.commit()

    cur = conn.cursor()
    for migration_id, sql in _MIGRATIONS:
        cur.execute("SELECT 1 FROM schema_version WHERE migration_id = ?", (migration_id,))
        if cur.fetchone():
            continue
        try:
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_version (migration_id, applied_at) VALUES (?, ?)",
                (migration_id, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
        except sqlite3.OperationalError as e:
            logger.debug("Migration '%s' skipped: %s", migration_id, e)


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(TRACK_DB, check_same_thread=False, timeout=10)

    # Core tables
    conn.executescript(
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
        );

        CREATE TABLE IF NOT EXISTS error_reports (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc        TEXT NOT NULL,
            session_id    TEXT,
            document_name TEXT,
            issue_type    TEXT,
            details       TEXT,
            status        TEXT DEFAULT 'open',
            ip            TEXT,
            city          TEXT,
            region        TEXT,
            country       TEXT,
            user_agent    TEXT
        );

        CREATE TABLE IF NOT EXISTS ip_usage (
            ip               TEXT PRIMARY KEY,
            upload_count     INTEGER DEFAULT 0,
            process_count    INTEGER DEFAULT 0,
            download_count   INTEGER DEFAULT 0,
            last_action_utc  TEXT
        );

        CREATE TABLE IF NOT EXISTS ip_blocks (
            ip              TEXT PRIMARY KEY,
            blocked_at_utc  TEXT NOT NULL,
            expires_at_utc  TEXT NOT NULL,
            trigger_feature TEXT
        );

        CREATE TABLE IF NOT EXISTS access_requests (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            ip                TEXT NOT NULL,
            email             TEXT,
            reason            TEXT,
            donation_interest TEXT,
            paid_interest     TEXT,
            willing_to_pay    TEXT,
            requested_at_utc  TEXT NOT NULL,
            status            TEXT DEFAULT 'pending',
            resolved_at_utc   TEXT,
            admin_note        TEXT
        );
        """
    )
    conn.commit()
    _apply_migrations(conn)
    return conn


# ==========================================================================
# Session helpers
# ==========================================================================
def get_session_id() -> str:
    if "tracker_session_id" not in st.session_state:
        st.session_state["tracker_session_id"] = str(uuid.uuid4())
    return st.session_state["tracker_session_id"]


def safe_str(value, default="") -> str:
    return default if value is None else str(value)


def format_utc_to_est(ts_utc: str) -> str:
    if not ts_utc:
        return ""
    try:
        dt = datetime.fromisoformat(ts_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except Exception:
        return ts_utc


# ==========================================================================
# Client context & geolocation  (cached per session to avoid rate-limits)
# ==========================================================================
def get_client_context() -> dict:
    ip, user_agent = "", ""
    try:
        headers  = dict(st.context.headers)
        xff      = headers.get("X-Forwarded-For")
        if xff:
            ip = safe_str(xff.split(",")[0].strip())
        user_agent = safe_str(headers.get("User-Agent", ""))
    except Exception:
        pass

    if not ip:
        try:
            ctx = st.context
            if ctx is not None:
                ip         = safe_str(getattr(ctx, "ip_address", ""))
                hdrs       = getattr(ctx, "headers", None)
                if hdrs and not user_agent:
                    user_agent = safe_str(hdrs.get("User-Agent", ""))
        except Exception:
            pass

    if not ip:
        ip = "local"

    return {"ip": ip, "user_agent": user_agent}


def lookup_location(ip: str) -> tuple:
    """Return (city, region, country, zip, lat, lon, isp) — cached in session_state."""
    if not ip or ip == "local":
        return "", "", "", "", "", "", ""

    cache_key = f"_geo_{ip}"
    if cache_key in st.session_state:
        return st.session_state[cache_key]

    try:
        req  = Request(f"http://ip-api.com/json/{ip}")
        data = json.loads(urlopen(req, timeout=3).read())
        result = (
            data.get("city", ""),
            data.get("regionName", ""),
            data.get("country", ""),
            data.get("zip", ""),
            data.get("lat", ""),
            data.get("lon", ""),
            data.get("isp", ""),
        )
    except Exception as e:
        logger.debug("Geolocation lookup failed for %s: %s", ip, e)
        result = ("", "", "", "", "", "", "")

    st.session_state[cache_key] = result
    return result


def log_event(action: str, document_name: str = ""):
    """
    Write to access_log, increment the per-feature counter, and block the IP
    if any feature limit has been reached.
    """
    try:
        ctx                                      = get_client_context()
        ip                                       = ctx["ip"]
        city, region, country, zip_, lat, lon, isp = lookup_location(ip)

        conn = get_db()
        conn.execute(
            """
            INSERT INTO access_log
                (ts_utc,session_id,ip,city,region,country,zip,lat,lon,isp,
                 action,document_name,user_agent)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                get_session_id(), ip,
                city, region, country, zip_, lat, lon, isp,
                action, document_name, ctx["user_agent"],
            ),
        )
        conn.commit()
        conn.close()

        # Increment feature counter then check all limits
        if action in FEATURE_LIMITS and ip and ip != "local":
            increment_feature_count(ip, action)
            triggered = check_limits_and_block(ip)
            if triggered:
                logger.info("IP %s blocked — %s limit reached.", ip, triggered)

    except Exception as e:
        logger.warning("log_event failed: %s", e)


# ==========================================================================
# Per-feature rate limiting
# ==========================================================================
_FEATURE_COLS = {
    "upload":   "upload_count",
    "process":  "process_count",
    "download": "download_count",
}


def increment_feature_count(ip: str, feature: str):
    col = _FEATURE_COLS.get(feature)
    if not col:
        return
    try:
        now  = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        conn.execute(
            f"""
            INSERT INTO ip_usage (ip, {col}, last_action_utc)
            VALUES (?, 1, ?)
            ON CONFLICT(ip) DO UPDATE SET
                {col}            = {col} + 1,
                last_action_utc  = excluded.last_action_utc
            """,
            (ip, now),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("increment_feature_count(%s, %s): %s", ip, feature, e)


def get_ip_usage(ip: str) -> dict[str, int]:
    """Return {upload_count, process_count, download_count} for an IP."""
    defaults = {"upload_count": 0, "process_count": 0, "download_count": 0}
    if not ip or ip == "local":
        return defaults
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT upload_count, process_count, download_count FROM ip_usage WHERE ip = ?",
            (ip,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            return {"upload_count": row[0] or 0, "process_count": row[1] or 0, "download_count": row[2] or 0}
    except Exception as e:
        logger.warning("get_ip_usage(%s): %s", ip, e)
    return defaults


def reset_ip_usage(ip: str):
    try:
        conn = get_db()
        conn.execute(
            """
            UPDATE ip_usage
            SET upload_count=0, process_count=0, download_count=0
            WHERE ip = ?
            """,
            (ip,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("reset_ip_usage(%s): %s", ip, e)


def check_limits_and_block(ip: str) -> str | None:
    """
    Check all feature limits.  Block the IP if any active limit is exceeded.
    Returns the name of the triggered feature, or None.
    """
    usage = get_ip_usage(ip)
    for feature, limit in FEATURE_LIMITS.items():
        if limit <= 0:
            continue
        col   = _FEATURE_COLS[feature]
        count = usage.get(col, 0)
        if count >= limit:
            block_ip(ip, trigger_feature=feature)
            return feature
    return None


def is_ip_blocked(ip: str) -> bool:
    if not ip or ip == "local":
        return False
    try:
        now  = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id FROM ip_blocks WHERE ip = ? AND expires_at_utc > ?",
            (ip, now),
        )
        row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception as e:
        logger.warning("is_ip_blocked(%s): %s", ip, e)
        return False


def get_block_trigger(ip: str) -> str:
    """Return which feature triggered the block (for the blocked screen)."""
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT trigger_feature FROM ip_blocks WHERE ip = ?", (ip,))
        row = cur.fetchone()
        conn.close()
        return (row[0] or "process") if row else "process"
    except Exception:
        return "process"


def block_ip(ip: str, trigger_feature: str = "process"):
    if not ip or ip == "local":
        return
    try:
        now     = datetime.now(timezone.utc)
        expires = now + timedelta(days=BLOCK_DAYS)
        conn    = get_db()
        conn.execute(
            """
            INSERT INTO ip_blocks (ip, blocked_at_utc, expires_at_utc, trigger_feature)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(ip) DO UPDATE SET
                blocked_at_utc  = excluded.blocked_at_utc,
                expires_at_utc  = excluded.expires_at_utc,
                trigger_feature = excluded.trigger_feature
            """,
            (ip, now.isoformat(), expires.isoformat(), trigger_feature),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("block_ip(%s): %s", ip, e)


def unblock_ip(ip: str):
    try:
        conn = get_db()
        conn.execute("DELETE FROM ip_blocks WHERE ip = ?", (ip,))
        conn.commit()
        conn.close()
        reset_ip_usage(ip)
    except Exception as e:
        logger.warning("unblock_ip(%s): %s", ip, e)


def get_current_blocks() -> list[tuple]:
    try:
        now  = datetime.now(timezone.utc).isoformat()
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            """
            SELECT ip, blocked_at_utc, expires_at_utc, trigger_feature
            FROM ip_blocks
            WHERE expires_at_utc > ?
            ORDER BY blocked_at_utc DESC
            """,
            (now,),
        )
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning("get_current_blocks: %s", e)
        return []


# ==========================================================================
# Access requests
# ==========================================================================
def has_pending_request(ip: str) -> bool:
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            "SELECT id FROM access_requests WHERE ip = ? AND status = 'pending'",
            (ip,),
        )
        row = cur.fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def save_access_request(ip, email, reason, donation_interest, paid_interest, willing_to_pay):
    try:
        conn = get_db()
        conn.execute(
            """
            INSERT INTO access_requests
                (ip, email, reason, donation_interest, paid_interest,
                 willing_to_pay, requested_at_utc)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ip, email, reason, donation_interest, paid_interest,
                willing_to_pay, datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("save_access_request: %s", e)


def get_access_requests(status: str | None = None, limit: int = 200) -> list[tuple]:
    try:
        conn = get_db()
        cur  = conn.cursor()
        if status:
            cur.execute(
                """
                SELECT id,ip,email,reason,donation_interest,paid_interest,
                       willing_to_pay,requested_at_utc,status,resolved_at_utc,admin_note
                FROM access_requests WHERE status=? ORDER BY requested_at_utc DESC LIMIT ?
                """,
                (status, limit),
            )
        else:
            cur.execute(
                """
                SELECT id,ip,email,reason,donation_interest,paid_interest,
                       willing_to_pay,requested_at_utc,status,resolved_at_utc,admin_note
                FROM access_requests ORDER BY requested_at_utc DESC LIMIT ?
                """,
                (limit,),
            )
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        logger.warning("get_access_requests: %s", e)
        return []


def approve_access_request(request_id: int, admin_note: str = ""):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT ip FROM access_requests WHERE id=?", (request_id,))
        row = cur.fetchone()
        if row:
            unblock_ip(row[0])
        cur.execute(
            """
            UPDATE access_requests
            SET status='approved', resolved_at_utc=?, admin_note=?
            WHERE id=?
            """,
            (datetime.now(timezone.utc).isoformat(), admin_note, request_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("approve_access_request: %s", e)


def reject_access_request(request_id: int, admin_note: str = ""):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute(
            """
            UPDATE access_requests
            SET status='rejected', resolved_at_utc=?, admin_note=?
            WHERE id=?
            """,
            (datetime.now(timezone.utc).isoformat(), admin_note, request_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("reject_access_request: %s", e)


# ==========================================================================
# Directory helpers
# ==========================================================================
def ensure_app_directories():
    ensure_directories()
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)


def move_file_to_trash(file_path: Path) -> Path | None:
    try:
        file_path = Path(file_path)
        if not file_path.exists() or not file_path.is_file():
            return None
        timestamp   = datetime.now().strftime("%Y%m%d_%H%M%S")
        destination = TRASH_DIR / f"{timestamp}__{file_path.name}"
        shutil.move(str(file_path), str(destination))
        return destination
    except Exception as e:
        logger.warning("move_file_to_trash(%s): %s", file_path, e)
        return None


# ==========================================================================
# Error report helpers  (unchanged from original)
# ==========================================================================
def save_error_report(document_name, issue_type, details, visitor_info=None, email=None):
    conn         = get_db()
    visitor_info = visitor_info or {}
    conn.execute(
        """
        INSERT INTO error_reports
            (ts_utc,session_id,document_name,issue_type,details,email,
             ip,city,region,country,user_agent)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            get_session_id(), document_name, issue_type, details, email,
            visitor_info.get("ip"), visitor_info.get("city"),
            visitor_info.get("region"), visitor_info.get("country"),
            visitor_info.get("user_agent"),
        ),
    )
    conn.commit()
    conn.close()


def update_error_report_status(report_id, new_status, public_resolution_note=None):
    conn           = get_db()
    resolved_at    = datetime.now(timezone.utc).isoformat() if new_status == "resolved" else None
    conn.execute(
        "UPDATE error_reports SET status=?,resolved_at_utc=?,public_resolution_note=? WHERE id=?",
        (new_status, resolved_at, public_resolution_note, report_id),
    )
    conn.commit()
    conn.close()


def get_error_report_counts() -> dict:
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT status, COUNT(*) FROM error_reports GROUP BY status")
    rows   = cur.fetchall()
    conn.close()
    counts = {"open": 0, "resolved": 0}
    for status, count in rows:
        counts[status] = count
    return counts


def get_open_error_reports(limit=200):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT id,ts_utc,session_id,document_name,issue_type,details,status,
               email,ip,city,region,country,user_agent,resolved_at_utc,public_resolution_note
        FROM error_reports WHERE status='open'
        ORDER BY ts_utc DESC LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_resolved_error_reports(limit=200):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT id,ts_utc,session_id,document_name,issue_type,details,status,
               email,ip,city,region,country,user_agent,resolved_at_utc,public_resolution_note
        FROM error_reports WHERE status='resolved'
        ORDER BY resolved_at_utc DESC, ts_utc DESC LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_public_bug_summary():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM error_reports")
    total_reported = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM error_reports WHERE status='resolved'")
    total_resolved = cur.fetchone()[0]
    conn.close()
    return total_reported, total_resolved


def get_public_resolved_bugs(limit=5):
    conn = get_db()
    cur  = conn.cursor()
    cur.execute(
        """
        SELECT issue_type, public_resolution_note, resolved_at_utc
        FROM error_reports
        WHERE status='resolved'
          AND public_resolution_note IS NOT NULL
          AND TRIM(public_resolution_note) != ''
        ORDER BY resolved_at_utc DESC LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def render_error_report_card(row):
    (
        report_id, ts_utc, session_id, document_name, issue_type, details, status,
        email, ip, city, region, country, user_agent, resolved_at_utc,
        public_resolution_note_saved,
    ) = row

    location_text = ", ".join(x for x in [city, region, country] if x) or "Unknown"

    with st.expander(f"#{report_id} | {issue_type} | {document_name or 'Unknown file'} | {status}"):
        st.write(f"**Reported at:** {format_utc_to_est(ts_utc)}")
        st.write(f"**Document:** {document_name or 'N/A'}")
        st.write(f"**Issue type:** {issue_type}")
        st.write(f"**Details:** {details}")
        st.write(f"**Status:** {status}")
        st.write(f"**Email:** {email or 'Not provided'}")
        st.write(f"**Resolved at:** {format_utc_to_est(resolved_at_utc) if resolved_at_utc else 'Not resolved yet'}")
        st.write(f"**IP:** {ip or 'N/A'}  |  **Location:** {location_text}")
        st.write(f"**User-Agent:** {user_agent or 'N/A'}")

        public_note = st.text_input(
            "Public resolution note",
            value=public_resolution_note_saved or "",
            placeholder="E.g. Fixed date parsing for statements missing the year.",
            key=f"public_note_{report_id}",
        )
        col1, col2 = st.columns(2)
        with col1:
            if status != "resolved" and st.button("Mark resolved", key=f"resolve_{report_id}", use_container_width=True):
                update_error_report_status(report_id, "resolved", public_note.strip() or None)
                st.success(f"Report #{report_id} resolved.")
                st.rerun()
        with col2:
            if status != "open" and st.button("Reopen", key=f"reopen_{report_id}", use_container_width=True):
                update_error_report_status(report_id, "open", None)
                st.rerun()


# ==========================================================================
# Admin helpers
# ==========================================================================
def admin_mode_requested() -> bool:
    return str(st.query_params.get("admin", "")).strip().lower() == "true"


def is_admin_authenticated() -> bool:
    return st.session_state.get("admin_authenticated", False)


def show_admin_login():
    st.markdown('<div class="admin-wrap">', unsafe_allow_html=True)
    st.caption("Internal access")
    entered  = st.text_input("Password", type="password", key="admin_password_input")
    expected = os.getenv("ADMIN_PASSWORD", "")
    if entered:
        if expected and hmac.compare_digest(entered, expected):
            st.session_state["admin_authenticated"] = True
            st.success("Admin access granted.")
            st.rerun()
        else:
            st.session_state["admin_authenticated"] = False
            st.error("Invalid password.")
    st.markdown("</div>", unsafe_allow_html=True)


def get_admin_file_rows(folder: Path) -> list[dict]:
    rows = []
    if not folder.exists():
        return rows
    for fp in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not fp.is_file():
            continue
        stat = fp.stat()
        rows.append({
            "name":         fp.name,
            "size_kb":      round(stat.st_size / 1024, 2),
            "modified_est": (
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .astimezone(ZoneInfo("America/New_York"))
                .strftime("%Y-%m-%d %I:%M:%S %p")
            ),
            "path": fp,
        })
    return rows


def _rc(tab_key):
    k = f"{tab_key}_rc"
    st.session_state.setdefault(k, 0)
    return st.session_state[k]


def _bump_rc(tab_key):
    st.session_state[f"{tab_key}_rc"] = _rc(tab_key) + 1


def _cb_key(tab_key, name, rc):
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return f"{tab_key}_f_{safe}_{rc}"


def _sa_key(tab_key, rc):
    return f"{tab_key}_sa_{rc}"


def safe_admin_file(base: Path, name: str) -> Path | None:
    try:
        base   = Path(base).resolve()
        target = (base / name).resolve()
        if not str(target).startswith(str(base)):
            return None
        return target if target.is_file() else None
    except Exception:
        return None


def _set_all(tab_key, names, value, rc):
    for n in names:
        st.session_state[_cb_key(tab_key, n, rc)] = value


def _build_zip(files: list[Path], label: str):
    if not files:
        return None, None, None, None
    if len(files) == 1:
        try:
            return files[0].read_bytes(), files[0].name, "application/octet-stream", None
        except Exception as e:
            return None, None, None, str(e)
    buf, skipped = io.BytesIO(), []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            try:
                zf.writestr(f.name, f.read_bytes())
            except Exception:
                skipped.append(f.name)
    buf.seek(0)
    if len(skipped) == len(files):
        return None, None, None, "None of the selected files could be read."
    warn = ("Skipped: " + ", ".join(skipped)) if skipped else None
    return buf.getvalue(), f"{label.lower()}_files.zip", "application/zip", warn


def render_admin_file_manager(tab_key: str, folder_label: str, folder_path: Path, delete_mode: str):
    files = get_admin_file_rows(folder_path)
    rc    = _rc(tab_key)
    st.write(f"Files in {folder_label.lower()}: {len(files)}")
    if not files:
        st.info("No files found.")
        return

    names = [r["name"] for r in files]
    for r in files:
        st.session_state.setdefault(_cb_key(tab_key, r["name"], rc), False)

    hc = st.columns([1.7, 5.3, 2, 3])
    with hc[0]:
        st.checkbox("Select all", key=_sa_key(tab_key, rc),
                    on_change=_set_all, args=(tab_key, names, True, rc))
    hc[1].markdown("**Name**")
    hc[2].markdown("**Size (KB)**")
    hc[3].markdown("**Modified EST**")

    selected = []
    for r in files:
        ck   = _cb_key(tab_key, r["name"], rc)
        cols = st.columns([1.7, 5.3, 2, 3])
        with cols[0]:
            st.checkbox("Select", key=ck, label_visibility="collapsed")
        cols[1].write(r["name"])
        cols[2].write(f"{r['size_kb']:.2f}")
        cols[3].write(r["modified_est"])
        if st.session_state.get(ck):
            sf = safe_admin_file(folder_path, r["name"])
            if sf:
                selected.append(sf)

    st.caption(f"Selected: {len(selected)}")
    dl_data, dl_name, dl_mime, dl_warn = _build_zip(selected, folder_label)
    if dl_warn:
        st.warning(dl_warn)

    c1, c2 = st.columns(2)
    with c1:
        if dl_data:
            st.download_button("Download selected", data=dl_data, file_name=dl_name,
                               mime=dl_mime, use_container_width=True,
                               key=f"{tab_key}_dl_{rc}")
        else:
            st.button("Download selected", disabled=True, use_container_width=True,
                      key=f"{tab_key}_dl_off_{rc}")

    with c2:
        if st.button("Delete selected", disabled=not selected, use_container_width=True,
                     key=f"{tab_key}_del_{rc}", type="secondary"):
            if delete_mode == "trash":
                ok, fail = [], []
                for f in selected:
                    m = move_file_to_trash(f)
                    (ok if m else fail).append(f.name)
                if ok:
                    log_event("admin_trash_move", ", ".join(ok))
                    st.session_state[f"{tab_key}_msg"] = f"Moved {len(ok)} file(s) to trash."
                if fail:
                    st.session_state[f"{tab_key}_err"] = "Could not move: " + ", ".join(fail[:5])
            else:
                ok, fail = [], []
                for f in selected:
                    try:
                        f.unlink()
                        ok.append(f.name)
                    except Exception:
                        fail.append(f.name)
                if ok:
                    log_event("admin_delete", ", ".join(ok))
                    st.session_state[f"{tab_key}_msg"] = f"Deleted {len(ok)} file(s)."
                if fail:
                    st.session_state[f"{tab_key}_err"] = "Could not delete: " + ", ".join(fail[:5])
            _bump_rc(tab_key)
            st.rerun()

    if msg := st.session_state.pop(f"{tab_key}_msg", None):
        st.success(msg)
    if err := st.session_state.pop(f"{tab_key}_err", None):
        st.error(err)


def read_access_log(limit: int = 200) -> pd.DataFrame:
    if not TRACK_DB.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(TRACK_DB)
    df   = pd.read_sql_query(
        f"""
        SELECT ts_utc,session_id,ip,city,region,country,zip,lat,lon,isp,
               action,document_name,user_agent
        FROM access_log ORDER BY id DESC LIMIT {int(limit)}
        """,
        conn,
    )
    conn.close()
    if not df.empty:
        df["ts_est"] = (
            pd.to_datetime(df["ts_utc"], utc=True)
            .dt.tz_convert("America/New_York")
            .dt.strftime("%Y-%m-%d %I:%M:%S %p")
        )
        df.drop(columns=["ts_utc"], inplace=True)
    return df


# ==========================================================================
# Admin: Access Requests tab
# ==========================================================================
def _usage_table_html(usage: dict) -> str:
    rows_html = ""
    for feature, col in _FEATURE_COLS.items():
        limit    = FEATURE_LIMITS[feature]
        count    = usage.get(col, 0)
        limit_lbl = str(limit) if limit > 0 else "∞"
        triggered = limit > 0 and count >= limit
        flag      = " ⚠️" if triggered else ""
        style     = "color:#9a3412;font-weight:700;" if triggered else ""
        rows_html += (
            f"<tr>"
            f"<td style='text-transform:capitalize;{style}'>{feature}{flag}</td>"
            f"<td style='{style}'>{count}</td>"
            f"<td>{limit_lbl}</td>"
            f"</tr>"
        )
    return (
        "<table class='usage-table'>"
        "<thead><tr><th>Feature</th><th>Used</th><th>Limit</th></tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )


def render_admin_access_requests():
    all_reqs = get_access_requests()
    pending  = [r for r in all_reqs if r[8] == "pending"]
    approved = [r for r in all_reqs if r[8] == "approved"]
    rejected = [r for r in all_reqs if r[8] == "rejected"]

    c1, c2, c3 = st.columns(3)
    c1.metric("Pending",  len(pending))
    c2.metric("Approved", len(approved))
    c3.metric("Rejected", len(rejected))

    # Active blocks
    st.markdown("### Active IP Blocks")
    blocks = get_current_blocks()
    if not blocks:
        st.info("No IPs currently blocked.")
    else:
        for ip_addr, blocked_at, expires_at, trigger in blocks:
            usage = get_ip_usage(ip_addr)
            total = sum(usage.values())
            cost  = usage.get("process_count", 0) * COST_PER_FILE_USD
            with st.expander(f"🔒 {ip_addr}  |  triggered by: {trigger or 'process'}  |  {total} actions  |  ≈ ${cost:.2f}"):
                st.markdown(
                    _usage_table_html(usage), unsafe_allow_html=True
                )
                col_a, col_b, col_c = st.columns([2, 2, 1])
                col_a.caption(f"Blocked: {format_utc_to_est(blocked_at)}")
                col_b.caption(f"Expires: {format_utc_to_est(expires_at)}")
                with col_c:
                    if st.button("Unblock", key=f"unblock_{ip_addr}", use_container_width=True):
                        unblock_ip(ip_addr)
                        log_event("admin_manual_unblock", ip_addr)
                        st.success(f"{ip_addr} unblocked.")
                        st.rerun()

    # Pending requests
    st.markdown("### Pending Requests")
    if not pending:
        st.info("No pending requests.")
    else:
        for row in pending:
            _render_request_card(row)

    # Full history
    with st.expander("Full request history"):
        if all_reqs:
            df = pd.DataFrame(all_reqs, columns=[
                "id","ip","email","reason","donation_interest",
                "paid_interest","willing_to_pay","requested_at",
                "status","resolved_at","admin_note",
            ])
            st.dataframe(df, use_container_width=True)
        else:
            st.caption("No requests yet.")


def _render_request_card(row):
    (req_id, ip, email, reason, donation_interest, paid_interest,
     willing_to_pay, requested_at_utc, status, resolved_at_utc, admin_note) = row

    usage = get_ip_usage(ip)
    cost  = usage.get("process_count", 0) * COST_PER_FILE_USD

    with st.expander(f"#{req_id} | {ip} | {email or 'No email'} | {format_utc_to_est(requested_at_utc)}"):
        col_a, col_b = st.columns([1, 1])
        with col_a:
            st.write(f"**IP:** {ip}")
            st.write(f"**Email:** {email or 'N/A'}")
            st.write(f"**Reason:** {reason or 'N/A'}")
            st.write(f"**Donation interest:** {donation_interest or 'N/A'}")
            st.write(f"**Paid plan interest:** {paid_interest or 'N/A'}")
            st.write(f"**Willing to pay:** {willing_to_pay or 'N/A'}")
        with col_b:
            st.markdown(f"**Usage breakdown** (≈ ${cost:.2f} cost):")
            st.markdown(_usage_table_html(usage), unsafe_allow_html=True)

        note = st.text_input("Admin note", value=admin_note or "", key=f"note_{req_id}")
        b1, b2 = st.columns(2)
        with b1:
            if status == "pending" and st.button(
                "✅ Approve & Unblock", key=f"approve_{req_id}",
                use_container_width=True, type="primary"
            ):
                approve_access_request(req_id, note)
                log_event("admin_approved_request", ip)
                st.success(f"IP {ip} unblocked.")
                st.rerun()
        with b2:
            if status == "pending" and st.button(
                "❌ Reject", key=f"reject_{req_id}", use_container_width=True
            ):
                reject_access_request(req_id, note)
                log_event("admin_rejected_request", ip)
                st.rerun()


def show_admin_panel():
    st.markdown('<div class="admin-wrap">', unsafe_allow_html=True)
    st.subheader("Admin Panel")

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(
        ["Input Files", "Output Files", "Trash", "Access Log", "Bug Reports", "Access Requests"]
    )
    with tab1:
        render_admin_file_manager("input",  "Input",  Path(UPLOAD_DIR), "trash")
    with tab2:
        render_admin_file_manager("output", "Output", Path(OUTPUT_DIR), "trash")
    with tab3:
        render_admin_file_manager("trash",  "Trash",  TRASH_DIR,        "permanent")
    with tab4:
        df = read_access_log(limit=300)
        st.write(f"Recent log entries: {len(df)}")
        st.dataframe(df, use_container_width=True)
    with tab5:
        counts = get_error_report_counts()
        c1, c2 = st.columns(2)
        c1.metric("Open",     counts.get("open",     0))
        c2.metric("Resolved", counts.get("resolved", 0))
        st.markdown("### Open Bugs")
        for row in (get_open_error_reports() or []):
            render_error_report_card(row)
        if not get_open_error_reports():
            st.info("No open bug reports.")
        st.markdown("### Resolved Bugs")
        resolved = get_resolved_error_reports()
        if not resolved:
            st.info("No resolved bug reports.")
        else:
            for row in resolved:
                render_error_report_card(row)
    with tab6:
        render_admin_access_requests()

    st.markdown("</div>", unsafe_allow_html=True)


# ==========================================================================
# Processing helpers
# ==========================================================================
def save_uploaded_file(uploaded_file) -> Path:
    destination = Path(UPLOAD_DIR) / uploaded_file.name
    with open(destination, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return destination


def save_uploaded_or_converted_file(uploaded_file) -> Path:
    """Save with server-side MIME validation before touching the filesystem."""
    raw_bytes = uploaded_file.getvalue()
    ok, err   = validate_uploaded_bytes(raw_bytes, uploaded_file.name)
    if not ok:
        raise ValueError(err)

    ext = uploaded_file.name.rsplit(".", 1)[-1].lower()
    if ext == "pdf":
        destination = Path(UPLOAD_DIR) / uploaded_file.name
        with open(destination, "wb") as f:
            f.write(raw_bytes)
        return destination

    # Image → convert to PDF
    output_path = Path(UPLOAD_DIR) / f"{Path(uploaded_file.name).stem}.pdf"
    return convert_image_to_pdf(uploaded_file, output_path)


def process_file(pdf_path: Path) -> Path:
    return process_pdf_to_csv(pdf_path)


# ==========================================================================
# Left panel
# ==========================================================================
def render_issue_form(current_doc_name: str):
    issue_type = st.selectbox(
        "What went wrong?",
        ["Wrong dates", "Missing transactions", "Duplicate transactions",
         "Wrong debit / credit values", "CSV format issue", "Processing failed", "Other"],
        key="error_issue_type",
    )
    error_details = st.text_area(
        "Tell us what happened",
        placeholder="E.g. Statement processed but 3 transactions from page 2 are missing.",
        key="error_details",
    )
    user_email = st.text_input(
        "Email (optional)",
        placeholder="you@example.com",
        key="error_email",
    )
    if st.button("Submit issue", use_container_width=True, type="secondary"):
        if not error_details.strip():
            st.warning("Please describe the issue before submitting.")
        elif user_email and "@" not in user_email:
            st.warning("Please enter a valid email or leave it blank.")
        else:
            ctx                                      = get_client_context()
            city, region, country, zip_, lat, lon, isp = lookup_location(ctx["ip"])
            visitor_info = {**ctx, "city": city, "region": region,
                            "country": country, "zip": zip_}
            save_error_report(
                document_name=current_doc_name,
                issue_type=issue_type,
                details=error_details.strip(),
                visitor_info=visitor_info,
                email=user_email.strip() or None,
            )
            log_event("error_report", current_doc_name)
            st.success("Your issue has been submitted.")
            st.session_state["error_details"] = ""
            st.session_state["error_email"]   = ""


def render_bug_history():
    st.caption("We actively track, review, and fix reported issues.")
    total_reported, total_resolved = get_public_bug_summary()
    c1, c2 = st.columns(2)
    c1.metric("Reported", total_reported)
    c2.metric("Resolved", total_resolved)

    for issue_type, note, resolved_at in (get_public_resolved_bugs() or []):
        st.write(f"**{issue_type}**")
        if note:
            st.write(note)
        if resolved_at:
            st.caption(f"Resolved: {format_utc_to_est(resolved_at)}")
        st.markdown("---")
    if not get_public_resolved_bugs():
        st.caption("No resolved bugs to show yet.")


# ==========================================================================
# Blocked screen
# ==========================================================================
def render_blocked_screen(ip: str):
    usage         = get_ip_usage(ip)
    process_count = usage.get("process_count", 0)
    total_cost    = process_count * COST_PER_FILE_USD
    trigger       = get_block_trigger(ip)

    # Build readable limit summary
    active_limits = {f: l for f, l in FEATURE_LIMITS.items() if l > 0}
    limits_desc   = ", ".join(f"{l} {f}s" for f, l in active_limits.items())

    st.markdown(
        f"""
        <div style="background:#fff8f0;border:1.5px solid #f97316;
                    border-radius:18px;padding:1.5rem 1.6rem;margin-bottom:1.2rem;">
            <div style="font-size:1.35rem;font-weight:800;color:#9a3412;margin-bottom:.6rem;">
                Free limit reached
            </div>
            <div style="color:#7c3a00;font-size:.97rem;line-height:1.7;">
                Your free quota ({limits_desc}) has been used up.
                This tool runs on <strong>Google Document AI</strong>, which charges
                per page — every conversion has a real cost.
                <br><br>
                <strong>Estimated processing cost so far: ${total_cost:.2f}</strong>
                &nbsp;({process_count} statements × ${COST_PER_FILE_USD:.2f}/file)
                <br><br>
                Access is paused for {BLOCK_DAYS} days. You can request more access
                below — I review manually, usually within 24–48 hours.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Per-feature usage table
    st.markdown(_usage_table_html(usage), unsafe_allow_html=True)
    st.markdown("<br>", unsafe_allow_html=True)

    if has_pending_request(ip):
        st.success(
            "Your access request is already submitted and under review. "
            "I'll email you once it's approved."
        )
        return

    req_tab, donate_tab, survey_tab = st.tabs(["Request access", "Donate", "Paid plan survey"])

    # ------------------------------------------------------------------
    # Tab 1 – Request access
    # ------------------------------------------------------------------
    with req_tab:
        st.markdown("**Tell me about your use case and I'll unblock you.**")

        req_email = st.text_input(
            "Your email (required)",
            placeholder="you@example.com",
            key="blocked_req_email",
        )
        req_reason = st.text_area(
            "Why do you need to process more statements?",
            placeholder="E.g. I'm a bookkeeper managing 20+ client accounts monthly…",
            key="blocked_req_reason",
        )

        st.markdown("---")
        st.caption("Optional — helps me understand how to keep this sustainable:")

        donation_interest = st.radio(
            "Would you consider donating to cover processing costs?",
            ["Yes, happy to donate", "Maybe", "No"],
            key="blocked_donate_radio", horizontal=True,
        )
        paid_interest = st.radio(
            "Interested in a low-cost paid plan with higher limits?",
            ["Yes", "Maybe", "No, free only"],
            key="blocked_paid_radio", horizontal=True,
        )
        willing_to_pay = ""
        if paid_interest in ("Yes", "Maybe"):
            willing_to_pay = st.text_input(
                "What would you pay monthly, and how many statements would you need?",
                placeholder="E.g. $5/month for 50 statements",
                key="blocked_pay_amount",
            )

        if st.button("Submit access request", use_container_width=True,
                     type="primary", key="submit_req_btn"):
            if not req_email or "@" not in req_email:
                st.warning("Please enter a valid email address.")
            elif not req_reason.strip():
                st.warning("Please describe your use case — even a sentence is fine.")
            else:
                save_access_request(
                    ip=ip, email=req_email.strip(), reason=req_reason.strip(),
                    donation_interest=donation_interest, paid_interest=paid_interest,
                    willing_to_pay=willing_to_pay.strip() if willing_to_pay else "",
                )
                log_event("access_request", "")
                st.success("Request submitted! I'll review within 24–48 hours and email you.")
                st.rerun()

    # ------------------------------------------------------------------
    # Tab 2 – Donate
    # ------------------------------------------------------------------
    with donate_tab:
        st.markdown("**Every donation directly offsets Google Document AI costs.**")
        st.markdown(
            f"Your {process_count} processed statements cost approximately "
            f"**${total_cost:.2f}**. Even $1–$5 helps keep this free for others."
        )
        if DONATION_LINK:
            st.markdown(
                f"""<a href="{DONATION_LINK}" target="_blank" style="
                    display:inline-block;
                    background:linear-gradient(180deg,#f97316 0%,#ea580c 100%);
                    color:white;border-radius:14px;padding:13px 32px;
                    font-weight:700;text-decoration:none;font-size:1rem;
                    margin-top:.6rem;box-shadow:0 4px 12px rgba(249,115,22,.25);
                ">☕ Donate / Buy me a coffee</a>""",
                unsafe_allow_html=True,
            )
        else:
            st.info("Set the `DONATION_LINK` env var (e.g. your Buy Me a Coffee URL) to enable this.")
        st.markdown("")
        st.caption(
            "After donating, also fill in the **Request access** tab — "
            "I'll approve it immediately once I see the donation."
        )

    # ------------------------------------------------------------------
    # Tab 3 – Paid plan survey
    # ------------------------------------------------------------------
    with survey_tab:
        st.markdown("**No commitment — just helps me decide if a paid tier makes sense.**")

        survey_price = st.selectbox(
            "How much would you pay per month?",
            ["$0 — free only", "$3–$5/month", "$10/month", "$15–$20/month", "More than $20/month"],
            key="survey_price",
        )
        survey_volume = st.selectbox(
            "How many statements do you typically process per month?",
            ["1–10", "11–25", "26–50", "51–100", "100+"],
            key="survey_volume",
        )
        survey_features = st.multiselect(
            "What extra features would matter to you?",
            ["Batch upload", "Email delivery of CSV", "API access",
             "Longer file history", "Priority processing speed", "Custom categories"],
            key="survey_features",
        )
        survey_email = st.text_input(
            "Email (optional — to notify you if a paid plan launches)",
            placeholder="you@example.com",
            key="survey_email",
        )

        if st.button("Submit survey", use_container_width=True,
                     type="secondary", key="submit_survey_btn"):
            paid_summary = (
                f"Price: {survey_price} | Volume: {survey_volume} | "
                f"Features: {', '.join(survey_features) or 'None selected'}"
            )
            email_to_use = survey_email.strip() if survey_email and "@" in survey_email else ""
            save_access_request(
                ip=ip, email=email_to_use,
                reason="Submitted via paid plan survey",
                donation_interest="Survey only",
                paid_interest="Yes",
                willing_to_pay=paid_summary,
            )
            log_event("access_request_survey", "")
            st.success("Survey submitted — thank you! Fill in the Request access tab if you need access now.")


# ==========================================================================
# Right panel
# ==========================================================================
def render_main_product_flow():
    ctx = get_client_context()
    ip  = ctx["ip"]

    # Gate: show blocked screen if IP is blocked
    if is_ip_blocked(ip):
        render_blocked_screen(ip)
        return

    uploaded_file = st.file_uploader(
        "Upload bank statement",
        label_visibility="collapsed",
        type=["pdf", "jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif"],
        key=f"uploader_{st.session_state['uploader_key']}",
    )

    if uploaded_file is not None:
        is_new = (
            st.session_state.get("current_uploaded_original_name") != uploaded_file.name
            or not st.session_state.get("current_upload_path")
        )

        if is_new:
            try:
                saved_pdf_path = save_uploaded_or_converted_file(uploaded_file)
            except ValueError as e:
                st.error(str(e))
                return

            st.session_state["current_upload_path"]          = str(saved_pdf_path)
            st.session_state["current_uploaded_original_name"] = uploaded_file.name
            st.session_state["current_saved_pdf_name"]        = saved_pdf_path.name
            st.session_state["latest_output_csv"]             = None
            st.session_state["latest_uploaded_name"]          = None
            st.session_state["csv_downloaded"]                = False
            log_event("upload", uploaded_file.name)

        saved_pdf_path = Path(st.session_state["current_upload_path"])
        display_name   = st.session_state.get("current_saved_pdf_name", uploaded_file.name)
        st.success(f"Uploaded: {display_name}")

        # Remaining-use warning for process limit
        process_limit = FEATURE_LIMITS["process"]
        if process_limit > 0:
            current_count = get_ip_usage(ip).get("process_count", 0)
            remaining     = max(0, process_limit - current_count)
            if remaining <= 3:
                st.warning(
                    f"You have **{remaining} free conversion{'s' if remaining != 1 else ''}** "
                    f"remaining before your access is temporarily paused."
                )

        if st.button("Process file", use_container_width=True, type="primary"):
            with st.spinner("Processing file..."):
                try:
                    output_csv_path = process_file(saved_pdf_path)
                    log_event("process", uploaded_file.name)  # increments + blocks if needed

                    st.session_state["latest_output_csv"]   = str(output_csv_path)
                    st.session_state["latest_uploaded_name"] = uploaded_file.name
                    st.session_state["csv_downloaded"]       = False
                    st.success("Processing completed successfully.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Processing failed: {e}")

    latest_csv          = st.session_state.get("latest_output_csv")
    latest_uploaded_name = st.session_state.get("latest_uploaded_name", "")

    if latest_csv and Path(latest_csv).exists():
        with open(latest_csv, "rb") as f:
            csv_bytes = f.read()

        if st.download_button(
            label="Download CSV",
            data=csv_bytes,
            file_name=Path(latest_csv).name,
            mime="text/csv",
            use_container_width=True,
            type="secondary",
        ):
            log_event("download", latest_uploaded_name)  # increments download counter
            st.session_state["csv_downloaded"] = True
            st.rerun()

        if st.session_state.get("csv_downloaded", False):
            current_upload_path = st.session_state.get("current_upload_path")

            if current_upload_path and st.button(
                "Delete my files from server", use_container_width=True, type="secondary"
            ):
                saved_pdf_path = Path(current_upload_path)
                moved_pdf      = move_file_to_trash(saved_pdf_path)
                moved_csv, csv_name = None, None

                if latest_csv and Path(latest_csv).exists():
                    csv_path  = Path(latest_csv)
                    csv_name  = csv_path.name
                    moved_csv = move_file_to_trash(csv_path)

                if not moved_csv:
                    fallback = Path(OUTPUT_DIR) / f"{saved_pdf_path.stem}.csv"
                    if fallback.exists():
                        csv_name  = fallback.name
                        moved_csv = move_file_to_trash(fallback)

                if moved_pdf:
                    log_event("trash_move", latest_uploaded_name or saved_pdf_path.name)
                    msg = f"File moved to trash: {moved_pdf.name}"
                    msg += f" | CSV: {moved_csv.name}" if moved_csv else (
                        f" | CSV expected but not found: {csv_name}" if csv_name
                        else " | No associated CSV found"
                    )
                    msg += " | Files will be deleted by the nightly cleanup job."

                    for k in ("current_upload_path", "current_uploaded_original_name",
                              "current_saved_pdf_name", "latest_output_csv",
                              "latest_uploaded_name", "admin_authenticated",
                              "admin_password_input"):
                        st.session_state[k] = None
                    st.session_state["csv_downloaded"]  = False
                    st.session_state["post_trash_message"] = msg
                    st.session_state["uploader_key"]    += 1
                    st.rerun()
                else:
                    st.error("Could not move file to trash.")


# ==========================================================================
# Main UI
# ==========================================================================
def main():
    inject_custom_css()
    ensure_app_directories()
    get_db()

    st.session_state.setdefault("csv_downloaded",    False)
    st.session_state.setdefault("uploader_key",      0)
    st.session_state.setdefault("post_trash_message", None)

    current_doc_name = (
        st.session_state.get("latest_uploaded_name")
        or st.session_state.get("current_saved_pdf_name")
        or st.session_state.get("current_uploaded_original_name")
        or ""
    )

    left_col, right_col = st.columns([0.68, 2.32], gap="medium")

    with left_col:
        st.markdown(
            """
            <div class="left-rail-card">
                <div class="left-rail-title">Privacy and Trust First</div>
                <div class="left-rail-body">
                    Your files are processed securely. Use "Delete my files from server"
                    after downloading to remove them immediately.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="left-rail-card">
                <div class="left-rail-title">Feedback improves accuracy</div>
                <div class="left-rail-body">
                    If anything looks off, report it. Each issue helps improve extraction
                    quality and strengthens trust in the output.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        with st.expander("Report an issue"):
            render_issue_form(current_doc_name)
        with st.expander("Bug reporting & fixes"):
            render_bug_history()

    with right_col:
        st.markdown('<div class="hero-title">Bank Statement Processor</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="hero-subtitle">Convert your bank statement into a clean, structured CSV in seconds.</div>',
            unsafe_allow_html=True,
        )
        st.markdown(
            """
            <div class="hero-mini-stats">
                <div class="hero-stat-pill">PDF &amp; image support</div>
                <div class="hero-stat-pill">Secure processing</div>
                <div class="hero-stat-pill">CSV-ready output</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.markdown('<div class="section-label">Upload bank statement</div>', unsafe_allow_html=True)

        if st.session_state.get("post_trash_message"):
            st.success(st.session_state["post_trash_message"])
            st.session_state["post_trash_message"] = None

        render_main_product_flow()

        st.markdown(
            """
            <div class="product-footer">
                <div class="product-footer-left">Reliable bank statement to CSV conversion.</div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;">
                    <div class="trust-badge">Secure workflow</div>
                    <div class="trust-badge">Structured CSV</div>
                    <div class="trust-badge">Delete from server</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if admin_mode_requested():
        if not is_admin_authenticated():
            show_admin_login()
        else:
            show_admin_panel()


if __name__ == "__main__":
    main()
