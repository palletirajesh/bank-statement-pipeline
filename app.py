from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os
import uuid
import hmac
import json
import sqlite3
import shutil
import io
import zipfile
from urllib.request import Request, urlopen

import pandas as pd
import streamlit as st
from PIL import Image

from main import process_pdf_to_csv, UPLOAD_DIR, OUTPUT_DIR, ensure_directories

st.set_page_config(
    page_title="Bank Statement Processor",
    layout="wide",
)

TRACK_DB = Path("access_log.db")
TRASH_DIR = Path("trash")


# =========================
# Styling
# =========================
def inject_custom_css():
    st.markdown(
        """
        <style>
        [data-testid="stHeader"] {
            visibility: hidden;
            height: 0%;
        }

        footer {
            visibility: hidden;
        }

        [data-testid="collapsedControl"] {
            display: none;
        }

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
            --shadow-xs: 0 1px 2px rgba(16, 24, 40, 0.03);
            --shadow-sm: 0 8px 24px rgba(15, 23, 42, 0.05);
        }

        .stApp {
            background: linear-gradient(180deg, #f8fbff 0%, #f4f7fb 100%);
        }

        .block-container {
            max-width: 100% !important;
            padding-top: 0.2rem !important;
            padding-bottom: 1rem !important;
            padding-left: 0.9rem !important;
            padding-right: 0.9rem !important;
        }

        section.main > div {
            max-width: 100% !important;
            padding-top: 0rem !important;
        }

        .left-rail-card {
            background: var(--surface);
            border-radius: 18px;
            padding: 0.95rem;
            box-shadow: var(--shadow-sm);
            margin-bottom: 12px;
        }

        .left-rail-title {
            font-size: 0.95rem;
            font-weight: 700;
            color: var(--text);
            margin-bottom: 0.3rem;
        }

        .left-rail-body {
            font-size: 0.95rem;
            line-height: 1.6;
            color: #475467;
        }

        .hero-title {
            font-size: 2.5rem;
            line-height: 1.02;
            letter-spacing: -0.04em;
            font-weight: 800;
            color: var(--text);
            margin: 0;
        }

        .hero-subtitle {
            font-size: 0.98rem;
            color: var(--muted);
            margin-top: 0.45rem;
            margin-bottom: 0.8rem;
            max-width: 760px;
            line-height: 1.55;
        }

        .hero-mini-stats {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-bottom: 0.8rem;
        }

        .hero-stat-pill {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 0.8rem;
            color: #344054;
        }

        .section-label {
            font-size: 0.92rem;
            font-weight: 700;
            color: #344054;
            margin-bottom: 0.35rem;
        }

        div[data-testid="stFileUploader"] {
            background: #fbfdff;
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 0.4rem;
        }

        div[data-testid="stFileUploader"] section {
            border-radius: 14px !important;
        }

        .stButton > button,
        .stDownloadButton > button {
            height: 44px !important;
            border-radius: 14px !important;
            font-weight: 700 !important;
            box-shadow: var(--shadow-xs) !important;
        }

        button[kind="primary"] {
            background: linear-gradient(180deg, #2563eb 0%, #1d4ed8 100%) !important;
            color: white !important;
            border: 1px solid #1d4ed8 !important;
        }

        button[kind="primary"]:hover {
            background: linear-gradient(180deg, #1d4ed8 0%, #1e40af 100%) !important;
            border-color: #1e40af !important;
        }

        button[kind="secondary"] {
            background: #ffffff !important;
            color: var(--text) !important;
            border: 1px solid #d7e0ee !important;
        }

        button[kind="secondary"]:hover {
            border-color: #b8cbf3 !important;
            color: var(--primary) !important;
        }

        div[data-testid="stExpander"] {
            border: none !important;
            background: rgba(255,255,255,0.82) !important;
            border-radius: 16px !important;
            box-shadow: var(--shadow-sm);
            margin-bottom: 10px;
            overflow: hidden;
        }

        div[data-testid="stExpander"] summary {
            font-size: 0.98rem;
            font-weight: 700;
            color: var(--text);
        }

        div[data-testid="stTextInput"] input,
        div[data-testid="stTextArea"] textarea,
        div[data-testid="stSelectbox"] > div {
            border-radius: 12px !important;
            border: 1px solid var(--line) !important;
            background: white !important;
        }

        div[data-testid="stAlert"] {
            border-radius: 16px !important;
            border: none !important;
        }

        [data-testid="stSuccess"] {
            background: var(--success-bg) !important;
            color: var(--success-text) !important;
        }

        .product-footer {
            margin-top: 0.8rem;
            padding-top: 0.8rem;
            border-top: 1px solid var(--line);
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 8px;
            flex-wrap: wrap;
        }

        .product-footer-left {
            color: var(--muted);
            font-size: 0.88rem;
        }

        .trust-badge {
            background: white;
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 6px 10px;
            font-size: 0.78rem;
            color: #344054;
        }

        .admin-wrap {
            margin-top: 0.9rem;
            background: rgba(255,255,255,0.88);
            border-radius: 20px;
            padding: 0.9rem;
            box-shadow: var(--shadow-sm);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# =========================
# File conversion helpers
# =========================
def convert_image_to_pdf(uploaded_file, output_path: Path) -> Path:
    image = Image.open(uploaded_file)
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(output_path, "PDF")
    return output_path


# =========================
# Setup helpers
# =========================
def get_db():
    conn = sqlite3.connect(TRACK_DB, check_same_thread=False)

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS access_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            session_id TEXT,
            ip TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            action TEXT NOT NULL,
            document_name TEXT,
            user_agent TEXT,
            zip TEXT,
            lat TEXT,
            lon TEXT,
            isp TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS error_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            session_id TEXT,
            document_name TEXT,
            issue_type TEXT,
            details TEXT,
            status TEXT DEFAULT 'open',
            ip TEXT,
            city TEXT,
            region TEXT,
            country TEXT,
            user_agent TEXT
        )
        """
    )

    try:
        conn.execute("ALTER TABLE error_reports ADD COLUMN email TEXT")
    except Exception:
        pass

    try:
        conn.execute("ALTER TABLE error_reports ADD COLUMN resolved_at_utc TEXT")
    except Exception:
        pass

    try:
        conn.execute("ALTER TABLE error_reports ADD COLUMN public_resolution_note TEXT")
    except Exception:
        pass

    conn.commit()
    return conn


def get_session_id():
    if "tracker_session_id" not in st.session_state:
        st.session_state["tracker_session_id"] = str(uuid.uuid4())
    return st.session_state["tracker_session_id"]


def safe_str(value, default=""):
    if value is None:
        return default
    return str(value)


def format_utc_to_est(ts_utc):
    if not ts_utc:
        return ""
    try:
        dt = datetime.fromisoformat(ts_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except Exception:
        return ts_utc


def get_client_context():
    ip = ""
    user_agent = ""

    try:
        headers = dict(st.context.headers)
        xff = headers.get("X-Forwarded-For")
        if xff:
            ip = safe_str(xff.split(",")[0].strip())
        user_agent = safe_str(headers.get("User-Agent", ""))
    except Exception:
        pass

    if not ip:
        try:
            ctx = st.context
            if ctx is not None:
                ip = safe_str(getattr(ctx, "ip_address", ""))
                headers = getattr(ctx, "headers", None)
                if headers and not user_agent:
                    user_agent = safe_str(headers.get("User-Agent", ""))
        except Exception:
            pass

    if not ip:
        ip = "local"

    return {
        "ip": ip,
        "city": "",
        "region": "",
        "country": "",
        "zip": "",
        "lat": "",
        "lon": "",
        "isp": "",
        "user_agent": user_agent,
    }


def lookup_location(ip: str):
    if not ip or ip == "local":
        return "", "", "", "", "", "", ""

    try:
        req = Request(f"http://ip-api.com/json/{ip}")
        data = json.loads(urlopen(req).read())
        return (
            data.get("city", ""),
            data.get("regionName", ""),
            data.get("country", ""),
            data.get("zip", ""),
            data.get("lat", ""),
            data.get("lon", ""),
            data.get("isp", ""),
        )
    except Exception:
        return "", "", "", "", "", "", ""


def log_event(action: str, document_name: str = ""):
    try:
        ctx = get_client_context()
        city, region, country, zip_code, lat, lon, isp = lookup_location(ctx["ip"])

        conn = get_db()
        conn.execute(
            """
            INSERT INTO access_log (
                ts_utc, session_id, ip, city, region, country, zip, lat, lon, isp, action, document_name, user_agent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                get_session_id(),
                ctx["ip"],
                city,
                region,
                country,
                zip_code,
                lat,
                lon,
                isp,
                action,
                document_name,
                ctx["user_agent"],
            ),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def ensure_app_directories():
    ensure_directories()
    Path(UPLOAD_DIR).mkdir(parents=True, exist_ok=True)
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    TRASH_DIR.mkdir(parents=True, exist_ok=True)


def move_file_to_trash(file_path: Path):
    try:
        file_path = Path(file_path)

        if not file_path.exists() or not file_path.is_file():
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        trash_name = f"{timestamp}__{file_path.name}"
        destination = TRASH_DIR / trash_name

        shutil.move(str(file_path), str(destination))
        return destination
    except Exception:
        return None


# =========================
# Error report helpers
# =========================
def save_error_report(document_name, issue_type, details, visitor_info=None, email=None):
    conn = get_db()
    cur = conn.cursor()

    visitor_info = visitor_info or {}

    cur.execute(
        """
        INSERT INTO error_reports (
            ts_utc,
            session_id,
            document_name,
            issue_type,
            details,
            email,
            ip,
            city,
            region,
            country,
            user_agent
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            get_session_id(),
            document_name,
            issue_type,
            details,
            email,
            visitor_info.get("ip"),
            visitor_info.get("city"),
            visitor_info.get("region"),
            visitor_info.get("country"),
            visitor_info.get("user_agent"),
        ),
    )

    conn.commit()
    conn.close()


def update_error_report_status(report_id, new_status, public_resolution_note=None):
    conn = get_db()
    cur = conn.cursor()

    resolved_at_utc = None
    if new_status == "resolved":
        resolved_at_utc = datetime.now(timezone.utc).isoformat()

    cur.execute(
        """
        UPDATE error_reports
        SET status = ?, resolved_at_utc = ?, public_resolution_note = ?
        WHERE id = ?
        """,
        (new_status, resolved_at_utc, public_resolution_note, report_id),
    )

    conn.commit()
    conn.close()


def get_error_report_counts():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT status, COUNT(*)
        FROM error_reports
        GROUP BY status
        """
    )
    rows = cur.fetchall()
    conn.close()

    counts = {"open": 0, "resolved": 0}
    for status, count in rows:
        counts[status] = count
    return counts


def get_open_error_reports(limit=200):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            ts_utc,
            session_id,
            document_name,
            issue_type,
            details,
            status,
            email,
            ip,
            city,
            region,
            country,
            user_agent,
            resolved_at_utc,
            public_resolution_note
        FROM error_reports
        WHERE status = 'open'
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


def get_resolved_error_reports(limit=200):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            id,
            ts_utc,
            session_id,
            document_name,
            issue_type,
            details,
            status,
            email,
            ip,
            city,
            region,
            country,
            user_agent,
            resolved_at_utc,
            public_resolution_note
        FROM error_reports
        WHERE status = 'resolved'
        ORDER BY resolved_at_utc DESC, ts_utc DESC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


def get_public_bug_summary():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM error_reports")
    total_reported = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM error_reports WHERE status = 'resolved'")
    total_resolved = cur.fetchone()[0]

    conn.close()
    return total_reported, total_resolved


def get_public_resolved_bugs(limit=5):
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT
            issue_type,
            public_resolution_note,
            resolved_at_utc
        FROM error_reports
        WHERE status = 'resolved'
          AND public_resolution_note IS NOT NULL
          AND TRIM(public_resolution_note) != ''
        ORDER BY resolved_at_utc DESC
        LIMIT ?
        """,
        (limit,),
    )

    rows = cur.fetchall()
    conn.close()
    return rows


def render_error_report_card(row):
    (
        report_id,
        ts_utc,
        session_id,
        document_name,
        issue_type,
        details,
        status,
        email,
        ip,
        city,
        region,
        country,
        user_agent,
        resolved_at_utc,
        public_resolution_note_saved,
    ) = row

    location_text = ", ".join([x for x in [city, region, country] if x]) or "Unknown"

    with st.expander(
        f"#{report_id} | {issue_type} | {document_name or 'Unknown file'} | {status}"
    ):
        st.write(f"**Reported at:** {format_utc_to_est(ts_utc)}")
        st.write(f"**Document:** {document_name or 'N/A'}")
        st.write(f"**Issue Type:** {issue_type}")
        st.write(f"**Details:** {details}")
        st.write(f"**Status:** {status}")
        st.write(f"**Email:** {email or 'Not provided'}")
        st.write(
            f"**Resolved at:** {format_utc_to_est(resolved_at_utc) if resolved_at_utc else 'Not resolved yet'}"
        )
        st.write(f"**Session ID:** {session_id or 'N/A'}")
        st.write(f"**IP:** {ip or 'N/A'}")
        st.write(f"**Location:** {location_text}")
        st.write(f"**User Agent:** {user_agent or 'N/A'}")

        public_note = st.text_input(
            "Public resolution note",
            value=public_resolution_note_saved or "",
            placeholder="Example: Fixed date parsing for statements missing the year.",
            key=f"public_note_{report_id}",
        )

        col1, col2 = st.columns(2)

        with col1:
            if status != "resolved":
                if st.button("Mark Resolved", key=f"resolve_report_{report_id}", use_container_width=True):
                    update_error_report_status(
                        report_id,
                        "resolved",
                        public_resolution_note=public_note.strip() if public_note.strip() else None,
                    )
                    st.success(f"Report #{report_id} marked as resolved.")
                    st.rerun()

        with col2:
            if status != "open":
                if st.button("Reopen", key=f"reopen_report_{report_id}", use_container_width=True):
                    update_error_report_status(
                        report_id,
                        "open",
                        public_resolution_note=None,
                    )
                    st.success(f"Report #{report_id} reopened.")
                    st.rerun()


# =========================
# Admin helpers
# =========================
def admin_mode_requested() -> bool:
    admin_value = st.query_params.get("admin", "")
    return str(admin_value).strip().lower() == "true"


def is_admin_authenticated() -> bool:
    return st.session_state.get("admin_authenticated", False)


def show_admin_login():
    st.markdown('<div class="admin-wrap">', unsafe_allow_html=True)
    st.caption("Internal access")

    entered_password = st.text_input(
        "Password",
        type="password",
        key="admin_password_input",
    )

    actual_password = os.getenv("ADMIN_PASSWORD", "")

    if entered_password:
        if actual_password and hmac.compare_digest(entered_password, actual_password):
            st.session_state["admin_authenticated"] = True
            st.success("Admin access granted.")
            st.rerun()
        else:
            st.session_state["admin_authenticated"] = False
            st.error("Invalid password.")
    st.markdown("</div>", unsafe_allow_html=True)



def list_files(folder: Path):
    rows = []
    if not folder.exists():
        return pd.DataFrame(columns=["name", "size_kb", "modified_est"])

    for file_path in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if file_path.is_file():
            stat = file_path.stat()
            modified_est = (
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .astimezone(ZoneInfo("America/New_York"))
                .strftime("%Y-%m-%d %I:%M:%S %p")
            )

            rows.append(
                {
                    "name": file_path.name,
                    "size_kb": round(stat.st_size / 1024, 2),
                    "modified_est": modified_est,
                }
            )

    return pd.DataFrame(rows)


def get_admin_file_rows(folder: Path):
    rows = []
    if not folder.exists():
        return rows

    for file_path in sorted(folder.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if file_path.is_file():
            stat = file_path.stat()
            modified_est = (
                datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                .astimezone(ZoneInfo("America/New_York"))
                .strftime("%Y-%m-%d %I:%M:%S %p")
            )
            rows.append(
                {
                    "name": file_path.name,
                    "size_kb": round(stat.st_size / 1024, 2),
                    "modified_est": modified_est,
                    "path": file_path,
                }
            )
    return rows


def get_admin_reset_counter(tab_key: str) -> int:
    state_key = f"{tab_key}_admin_reset_counter"
    if state_key not in st.session_state:
        st.session_state[state_key] = 0
    return st.session_state[state_key]


def bump_admin_reset_counter(tab_key: str):
    state_key = f"{tab_key}_admin_reset_counter"
    st.session_state[state_key] = get_admin_reset_counter(tab_key) + 1


def get_admin_checkbox_key(tab_key: str, file_name: str, reset_counter: int) -> str:
    safe_name = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in file_name)
    return f"{tab_key}_file_{safe_name}_{reset_counter}"


def get_admin_select_all_key(tab_key: str, reset_counter: int) -> str:
    return f"{tab_key}_select_all_{reset_counter}"


def safe_admin_file(base_folder: Path, file_name: str):
    try:
        base_folder = Path(base_folder).resolve()
        target = (base_folder / file_name).resolve()
        if not str(target).startswith(str(base_folder)):
            return None
        if not target.exists() or not target.is_file():
            return None
        return target
    except Exception:
        return None


def set_all_admin_checkboxes(tab_key: str, file_names: list[str], value: bool, reset_counter: int):
    for file_name in file_names:
        checkbox_key = get_admin_checkbox_key(tab_key, file_name, reset_counter)
        st.session_state[checkbox_key] = value


def move_admin_selected_files_to_trash(selected_files: list[Path]):
    moved_files = []
    failed_files = []

    for file_path in selected_files:
        moved = move_file_to_trash(file_path)
        if moved:
            moved_files.append(moved.name)
        else:
            failed_files.append(file_path.name)

    return moved_files, failed_files


def permanently_delete_admin_selected_files(selected_files: list[Path]):
    deleted_files = []
    failed_files = []

    for file_path in selected_files:
        try:
            file_path.unlink()
            deleted_files.append(file_path.name)
        except Exception:
            failed_files.append(file_path.name)

    return deleted_files, failed_files


def build_admin_download_payload(selected_files: list[Path], folder_label: str):
    if not selected_files:
        return None, None, None, None

    if len(selected_files) == 1:
        safe_file = selected_files[0]
        try:
            return safe_file.read_bytes(), safe_file.name, "application/octet-stream", None
        except PermissionError:
            return None, None, None, f"File is open or locked: {safe_file.name}"
        except Exception as e:
            return None, None, None, f"Could not read {safe_file.name}: {e}"

    zip_buffer = io.BytesIO()
    skipped_files = []

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for safe_file in selected_files:
            try:
                zf.writestr(safe_file.name, safe_file.read_bytes())
            except Exception:
                skipped_files.append(safe_file.name)

    zip_buffer.seek(0)

    if skipped_files and len(skipped_files) == len(selected_files):
        return None, None, None, "None of the selected files could be prepared for download."

    warning_message = None
    if skipped_files:
        warning_message = "Skipped file(s): " + ", ".join(skipped_files)

    zip_name = f"{folder_label.lower()}_files.zip"
    return zip_buffer.getvalue(), zip_name, "application/zip", warning_message


def render_admin_file_manager(tab_key: str, folder_label: str, folder_path: Path, delete_mode: str):
    files = get_admin_file_rows(folder_path)
    reset_counter = get_admin_reset_counter(tab_key)

    st.write(f"Files in {folder_label.lower()}: {len(files)}")

    if not files:
        st.info("No files found.")
        return

    file_names = [row["name"] for row in files]
    select_all_key = get_admin_select_all_key(tab_key, reset_counter)

    for row in files:
        checkbox_key = get_admin_checkbox_key(tab_key, row["name"], reset_counter)
        if checkbox_key not in st.session_state:
            st.session_state[checkbox_key] = False

    header_cols = st.columns([1.7, 5.3, 2, 3])
    with header_cols[0]:
        st.checkbox(
            "Select all",
            key=select_all_key,
            on_change=set_all_admin_checkboxes,
            args=(tab_key, file_names, True, reset_counter),
        )
    header_cols[1].markdown("**Name**")
    header_cols[2].markdown("**Size (KB)**")
    header_cols[3].markdown("**Modified EST**")

    st.markdown("<div style='height: 6px;'></div>", unsafe_allow_html=True)

    selected_files = []

    for row in files:
        checkbox_key = get_admin_checkbox_key(tab_key, row["name"], reset_counter)
        cols = st.columns([1.7, 5.3, 2, 3])

        with cols[0]:
            st.checkbox(
                "Select",
                key=checkbox_key,
                label_visibility="collapsed",
            )

        with cols[1]:
            st.write(row["name"])

        with cols[2]:
            st.write(f"{row['size_kb']:.2f}")

        with cols[3]:
            st.write(row["modified_est"])

        if st.session_state.get(checkbox_key, False):
            safe_file = safe_admin_file(folder_path, row["name"])
            if safe_file:
                selected_files.append(safe_file)

    st.markdown("<div style='height: 12px;'></div>", unsafe_allow_html=True)
    st.caption(f"Selected files: {len(selected_files)}")

    download_data, download_name, download_mime, download_warning = build_admin_download_payload(
        selected_files,
        folder_label,
    )

    if download_warning:
        st.warning(download_warning)

    action_col1, action_col2 = st.columns(2)

    with action_col1:
        if download_data:
            st.download_button(
                "Download selected file(s)",
                data=download_data,
                file_name=download_name,
                mime=download_mime,
                use_container_width=True,
                key=f"{tab_key}_download_action_{reset_counter}",
            )
        else:
            st.button(
                "Download selected file(s)",
                disabled=True,
                use_container_width=True,
                key=f"{tab_key}_download_disabled_{reset_counter}",
            )

    with action_col2:
        delete_label = "Delete selected file(s)"
        if st.button(
            delete_label,
            disabled=not selected_files,
            use_container_width=True,
            key=f"{tab_key}_delete_action_{reset_counter}",
            type="secondary",
        ):
            if delete_mode == "trash":
                changed_files, failed_files = move_admin_selected_files_to_trash(selected_files)
                if changed_files:
                    log_event("admin_trash_move", ", ".join(changed_files))
                    st.session_state[f"{tab_key}_admin_message"] = (
                        f"Moved {len(changed_files)} file(s) to trash: " + ", ".join(changed_files[:5])
                    )
                if failed_files:
                    st.session_state[f"{tab_key}_admin_error"] = (
                        "Could not move: " + ", ".join(failed_files[:5])
                    )
            else:
                changed_files, failed_files = permanently_delete_admin_selected_files(selected_files)
                if changed_files:
                    log_event("admin_delete", ", ".join(changed_files))
                    st.session_state[f"{tab_key}_admin_message"] = (
                        f"Permanently deleted {len(changed_files)} file(s): " + ", ".join(changed_files[:5])
                    )
                if failed_files:
                    st.session_state[f"{tab_key}_admin_error"] = (
                        "Could not delete: " + ", ".join(failed_files[:5])
                    )

            bump_admin_reset_counter(tab_key)
            st.rerun()

    success_message = st.session_state.pop(f"{tab_key}_admin_message", None)
    error_message = st.session_state.pop(f"{tab_key}_admin_error", None)

    if success_message:
        st.success(success_message)
    if error_message:
        st.error(error_message)


def read_access_log(limit: int = 200):
    if not TRACK_DB.exists():
        return pd.DataFrame()

    conn = sqlite3.connect(TRACK_DB)
    df = pd.read_sql_query(
        f"""
        SELECT
        ts_utc, session_id, ip, city, region, country, zip, lat, lon, isp, action, document_name, user_agent
        FROM access_log
        ORDER BY id DESC
        LIMIT {int(limit)}
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
        df = df.drop(columns=["ts_utc"])

    return df


def show_admin_panel():
    st.markdown('<div class="admin-wrap">', unsafe_allow_html=True)
    st.subheader("Admin Panel")

    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["Input Files", "Output Files", "Trash", "Access Log", "Bug Reports"]
    )

    with tab1:
        render_admin_file_manager("input", "Input", Path(UPLOAD_DIR), delete_mode="trash")

    with tab2:
        render_admin_file_manager("output", "Output", Path(OUTPUT_DIR), delete_mode="trash")

    with tab3:
        render_admin_file_manager("trash", "Trash", TRASH_DIR, delete_mode="permanent")

    with tab4:
        log_df = read_access_log(limit=300)
        st.write(f"Recent log entries: {len(log_df)}")
        st.dataframe(log_df, use_container_width=True)

    with tab5:
        counts = get_error_report_counts()

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Open Reports", counts.get("open", 0))
        with col2:
            st.metric("Resolved Reports", counts.get("resolved", 0))

        open_rows = get_open_error_reports()
        resolved_rows = get_resolved_error_reports()

        st.markdown("### Open Bugs")
        if not open_rows:
            st.info("No open bug reports.")
        else:
            for row in open_rows:
                render_error_report_card(row)

        st.markdown("### Resolved Bugs")
        if not resolved_rows:
            st.info("No resolved bug reports.")
        else:
            for row in resolved_rows:
                render_error_report_card(row)
    st.markdown("</div>", unsafe_allow_html=True)

# =========================
# Processing helpers
# =========================
def save_uploaded_file(uploaded_file) -> Path:
    destination = Path(UPLOAD_DIR) / uploaded_file.name
    with open(destination, "wb") as f:
        f.write(uploaded_file.getbuffer())
    return destination


def save_uploaded_or_converted_file(uploaded_file) -> Path:
    ext = uploaded_file.name.split(".")[-1].lower()

    if ext == "pdf":
        return save_uploaded_file(uploaded_file)

    output_path = Path(UPLOAD_DIR) / f"{Path(uploaded_file.name).stem}.pdf"
    return convert_image_to_pdf(uploaded_file, output_path)


def process_file(pdf_path: Path) -> Path:
    return process_pdf_to_csv(pdf_path)


# =========================
# Left panel helpers
# =========================
def render_issue_form(current_doc_name: str):
    issue_type = st.selectbox(
        "What went wrong?",
        [
            "Wrong dates",
            "Missing transactions",
            "Duplicate transactions",
            "Wrong debit / credit values",
            "CSV format issue",
            "Processing failed",
            "Other",
        ],
        key="error_issue_type",
    )

    error_details = st.text_area(
        "Tell us what happened",
        placeholder="Example: Statement processed, but 3 transactions from page 2 are missing.",
        key="error_details",
    )

    user_email = st.text_input(
        "Email (optional, if you'd like us to follow up)",
        placeholder="you@example.com",
        key="error_email",
    )

    if st.button("Submit issue", use_container_width=True, type="secondary"):
        if not error_details.strip():
            st.warning("Please describe the issue before submitting.")
        elif user_email and "@" not in user_email:
            st.warning("Please enter a valid email or leave it blank.")
        else:
            visitor_info = get_client_context()
            city, region, country, zip_code, lat, lon, isp = lookup_location(visitor_info["ip"])
            visitor_info.update(
                {
                    "city": city,
                    "region": region,
                    "country": country,
                    "zip": zip_code,
                    "lat": lat,
                    "lon": lon,
                    "isp": isp,
                }
            )

            save_error_report(
                document_name=current_doc_name,
                issue_type=issue_type,
                details=error_details.strip(),
                visitor_info=visitor_info,
                email=user_email.strip() if user_email else None,
            )

            log_event("error_report", current_doc_name)
            st.success("Your issue has been submitted.")
            st.session_state["error_details"] = ""
            st.session_state["error_email"] = ""


def render_bug_history():
    st.caption("We actively track, review, and fix reported issues.")

    total_reported, total_resolved = get_public_bug_summary()
    public_resolved_rows = get_public_resolved_bugs()

    col1, col2 = st.columns(2)
    with col1:
        st.metric("Reported", total_reported)
    with col2:
        st.metric("Resolved", total_resolved)

    if public_resolved_rows:
        st.markdown("**Recent fixes**")
        for issue_type, public_resolution_note, resolved_at_utc in public_resolved_rows:
            st.write(f"**{issue_type}**")
            if public_resolution_note:
                st.write(public_resolution_note)
            if resolved_at_utc:
                st.caption(f"Resolved on: {format_utc_to_est(resolved_at_utc)}")
            st.markdown("---")
    else:
        st.caption("No resolved bugs to show yet.")


# =========================
# Right panel helpers
# =========================
def render_main_product_flow():
    uploaded_file = st.file_uploader(
        "Upload bank statement",
        label_visibility="collapsed",
        type=["pdf", "jpg", "jpeg", "png", "webp", "bmp", "tiff", "tif"],
        key=f"uploader_{st.session_state['uploader_key']}",
    )

    if uploaded_file is not None:
        is_new_upload = (
            st.session_state.get("current_uploaded_original_name") != uploaded_file.name
            or not st.session_state.get("current_upload_path")
        )

        if is_new_upload:
            saved_pdf_path = save_uploaded_or_converted_file(uploaded_file)
            st.session_state["current_upload_path"] = str(saved_pdf_path)
            st.session_state["current_uploaded_original_name"] = uploaded_file.name
            st.session_state["current_saved_pdf_name"] = saved_pdf_path.name
            st.session_state["latest_output_csv"] = None
            st.session_state["latest_uploaded_name"] = None
            st.session_state["csv_downloaded"] = False
            log_event("upload", uploaded_file.name)

        saved_pdf_path = Path(st.session_state["current_upload_path"])
        display_name = st.session_state.get("current_saved_pdf_name", uploaded_file.name)

        st.success(f"Uploaded: {display_name}")

        if st.button("Process file", use_container_width=True, type="primary"):
            with st.spinner("Processing file..."):
                try:
                    output_csv_path = process_file(saved_pdf_path)
                    log_event("process", uploaded_file.name)

                    st.session_state["latest_output_csv"] = str(output_csv_path)
                    st.session_state["latest_uploaded_name"] = uploaded_file.name
                    st.session_state["csv_downloaded"] = False
                    st.success("Processing completed successfully.")
                    st.rerun()
                except Exception as e:
                    st.error(f"Processing failed: {e}")

    latest_csv = st.session_state.get("latest_output_csv")
    latest_uploaded_name = st.session_state.get("latest_uploaded_name", "")

    if latest_csv and Path(latest_csv).exists():
        with open(latest_csv, "rb") as f:
            csv_bytes = f.read()

        download_name = Path(latest_csv).name

        if st.download_button(
            label="Download CSV",
            data=csv_bytes,
            file_name=download_name,
            mime="text/csv",
            use_container_width=True,
            type="secondary",
        ):
            log_event("download", latest_uploaded_name)
            st.session_state["csv_downloaded"] = True
            st.rerun()

        if st.session_state.get("csv_downloaded", False):
            current_upload_path = st.session_state.get("current_upload_path")

            if current_upload_path and st.button(
                "Delete my files from server",
                use_container_width=True,
                type="secondary",
            ):
                saved_pdf_path = Path(current_upload_path)
                moved_pdf = move_file_to_trash(saved_pdf_path)

                moved_csv = None
                csv_name = None

                if latest_csv and Path(latest_csv).exists():
                    csv_path = Path(latest_csv)
                    csv_name = csv_path.name
                    moved_csv = move_file_to_trash(csv_path)

                if not moved_csv:
                    fallback_csv = Path(OUTPUT_DIR) / f"{saved_pdf_path.stem}.csv"
                    if fallback_csv.exists():
                        csv_name = fallback_csv.name
                        moved_csv = move_file_to_trash(fallback_csv)

                if moved_pdf:
                    log_event("trash_move", latest_uploaded_name or saved_pdf_path.name)

                    message = f"File moved to trash: {moved_pdf.name}"

                    if moved_csv:
                        message += f" | Associated CSV moved to trash: {moved_csv.name}"
                    elif csv_name:
                        message += f" | Associated CSV was expected but could not be moved: {csv_name}"
                    else:
                        message += " | No associated CSV found"

                    message += " | These files will be deleted from the server by the nightly cleanup job."

                    st.session_state["post_trash_message"] = message
                    st.session_state["current_upload_path"] = None
                    st.session_state["current_uploaded_original_name"] = None
                    st.session_state["current_saved_pdf_name"] = None
                    st.session_state["latest_output_csv"] = None
                    st.session_state["latest_uploaded_name"] = None
                    st.session_state["csv_downloaded"] = False
                    st.session_state["admin_authenticated"] = False
                    st.session_state["admin_password_input"] = ""
                    st.session_state["uploader_key"] += 1
                    st.rerun()
                else:
                    st.error("Could not move file to trash.")


# =========================
# Main UI
# =========================
def main():
    inject_custom_css()
    ensure_app_directories()
    get_db()

    if "csv_downloaded" not in st.session_state:
        st.session_state["csv_downloaded"] = False
    if "uploader_key" not in st.session_state:
        st.session_state["uploader_key"] = 0
    if "post_trash_message" not in st.session_state:
        st.session_state["post_trash_message"] = None

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
                    Your files are processed securely.You can remove your files from the server
                    using the option "Delete my files from server" post your download
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
            '''
            <div class="hero-mini-stats">
                <div class="hero-stat-pill">PDF & image support</div>
                <div class="hero-stat-pill">Secure processing</div>
                <div class="hero-stat-pill">CSV-ready output</div>
            </div>
            ''',
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
                <div style="display:flex; gap:8px; flex-wrap:wrap;">
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