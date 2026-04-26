"""
main.py – Bank Statement Pipeline (Document AI → CSV)

Changes from original
─────────────────────
1. GCP credentials moved to env vars (GCP_PROJECT_ID, GCP_LOCATION, GCP_PROCESSOR_ID).
   Hard-coded values kept as fallbacks so nothing breaks without a redeploy.

2. process_document_with_chunking() now ALWAYS returns list[Document].
   Every downstream function therefore works on a plain list — no more
   isinstance(x, list) branches anywhere.

3. Chunk page-offset bug fixed.
   The old get_entity_page_number() returned a 0-based index within the
   chunk, so entities in chunk 2+ mapped to the *wrong* page in
   get_page_text_map().  The new get_entity_global_page_number(entity,
   doc_page_offset) adds the cumulative page offset of all prior chunks,
   giving a correct 1-based global page number.
   extract_transactions() now tracks that offset as it walks each chunk.

4. Silent bare-except replaced with logger.warning / logger.debug
   throughout.  A module-level logger lets the caller control verbosity
   without editing this file.
"""

from pathlib import Path
from datetime import datetime
import json
import logging
import os
import re
import tempfile

import pandas as pd
from google.cloud import documentai
from google.protobuf.json_format import MessageToDict
from pypdf import PdfReader, PdfWriter


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAX_PAGES_PER_REQUEST = 15

# Local key file fallback for development
LOCAL_KEY = Path("document-key.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and LOCAL_KEY.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(LOCAL_KEY.resolve())

# GCP — set these as env vars in production; original values kept as fallbacks
PROJECT_ID   = os.getenv("GCP_PROJECT_ID",   "418651416236")
LOCATION     = os.getenv("GCP_LOCATION",     "us")
PROCESSOR_ID = os.getenv("GCP_PROCESSOR_ID", "47cfa52a67c0c7a0")

UPLOAD_DIR    = Path("input")
OUTPUT_DIR    = Path("output")
PROCESSED_DIR = Path("processed")
JSON_DIR      = Path("json_output")

MONTH_MAP = {
    "jan": 1,  "january": 1,
    "feb": 2,  "february": 2,
    "mar": 3,  "march": 3,
    "apr": 4,  "aprl": 4,  "april": 4,
    "may": 5,
    "jun": 6,  "june": 6,
    "jul": 7,  "july": 7,
    "aug": 8,  "august": 8,
    "sep": 9,  "sept": 9,  "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------
def ensure_directories():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# PDF helpers
# ---------------------------------------------------------------------------
def get_pdf_page_count(file_path: Path) -> int:
    reader = PdfReader(str(file_path))
    return len(reader.pages)


def split_pdf_into_chunks(file_path: Path, chunk_size: int = MAX_PAGES_PER_REQUEST) -> list[Path]:
    reader = PdfReader(str(file_path))
    temp_paths: list[Path] = []

    for start in range(0, len(reader.pages), chunk_size):
        writer = PdfWriter()
        for i in range(start, min(start + chunk_size, len(reader.pages))):
            writer.add_page(reader.pages[i])

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        tmp_path = Path(tmp.name)
        tmp.close()

        with open(tmp_path, "wb") as f:
            writer.write(f)

        temp_paths.append(tmp_path)

    return temp_paths


def cleanup_temp_files(paths: list[Path]):
    for path in paths:
        try:
            if path.exists():
                path.unlink()
        except Exception as e:
            logger.warning("Could not delete temp file %s: %s", path, e)


# ---------------------------------------------------------------------------
# Document AI
# ---------------------------------------------------------------------------
def process_document(file_path: Path):
    client = documentai.DocumentProcessorServiceClient()
    name   = client.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    raw_document = documentai.RawDocument(content=file_bytes, mime_type="application/pdf")
    request      = documentai.ProcessRequest(name=name, raw_document=raw_document)
    result       = client.process_document(request=request)
    return result.document


def process_document_with_chunking(file_path: Path) -> list:
    """
    Always returns list[Document] — one element for short PDFs, multiple for
    chunked ones.  Callers never need to branch on the return type.
    """
    page_count = get_pdf_page_count(file_path)

    if page_count <= MAX_PAGES_PER_REQUEST:
        return [process_document(file_path)]

    chunk_paths = split_pdf_into_chunks(file_path, MAX_PAGES_PER_REQUEST)
    try:
        return [process_document(cp) for cp in chunk_paths]
    finally:
        cleanup_temp_files(chunk_paths)


# ---------------------------------------------------------------------------
# JSON persistence
# ---------------------------------------------------------------------------
def save_document_ai_json(documents: list, pdf_path: Path) -> Path:
    """Persist raw Document AI response(s) for debugging / audit."""
    ensure_directories()

    if len(documents) == 1:
        json_payload = MessageToDict(documents[0]._pb)
    else:
        json_payload = [
            {"chunk_number": idx, "document": MessageToDict(doc._pb)}
            for idx, doc in enumerate(documents, start=1)
        ]

    json_file = JSON_DIR / f"{pdf_path.stem}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, indent=2, ensure_ascii=False)

    return json_file


# ---------------------------------------------------------------------------
# Document helpers – all accept list[Document]
# ---------------------------------------------------------------------------
def get_all_entities(documents: list):
    entities = []
    for doc in documents:
        entities.extend(doc.entities)
    return entities


def collect_full_text(documents: list) -> str:
    return "\n".join((doc.text or "") for doc in documents if doc)


def get_page_text_map(documents: list) -> dict[int, str]:
    """
    Build a global 1-based page-number → text map across all chunks.
    Each chunk's pages are numbered from 0 internally; we add the running
    offset so that the keys match what get_entity_global_page_number returns.
    """
    page_text_map: dict[int, str] = {}
    global_page_number = 1

    for doc in documents:
        full_text = doc.text or ""
        for page in doc.pages:
            page_parts = []
            for token in page.tokens:
                anchor = token.layout.text_anchor
                if not anchor.text_segments:
                    continue

                token_text_parts = []
                for seg in anchor.text_segments:
                    start_index = int(seg.start_index) if seg.start_index else 0
                    end_index   = int(seg.end_index)
                    token_text_parts.append(full_text[start_index:end_index])

                token_text = "".join(token_text_parts).strip()
                if token_text:
                    page_parts.append(token_text)

            page_text_map[global_page_number] = " ".join(page_parts)
            global_page_number += 1

    return page_text_map


def get_entity_global_page_number(entity, doc_page_offset: int) -> int | None:
    """
    Return the *global* 1-based page number for an entity.

    doc_page_offset  — total pages in all preceding chunks.
    A page-0 entity in chunk 1 (offset 15) → global page 16.

    The old get_entity_page_number() ignored the offset, so entities in
    any chunk after the first mapped to wrong pages in get_page_text_map.
    """
    page_refs = []

    page_anchor = getattr(entity, "page_anchor", None)
    if page_anchor and getattr(page_anchor, "page_refs", None):
        page_refs.extend(page_anchor.page_refs)

    # Fall back to property-level anchors
    if not page_refs:
        for prop in entity.properties:
            prop_page_anchor = getattr(prop, "page_anchor", None)
            if prop_page_anchor and getattr(prop_page_anchor, "page_refs", None):
                page_refs.extend(prop_page_anchor.page_refs)

    if not page_refs:
        return None

    first_ref = page_refs[0]
    page_num  = getattr(first_ref, "page", None)
    if page_num is None:
        return None

    # page_num is 0-based within the chunk
    return int(page_num) + 1 + doc_page_offset


# ---------------------------------------------------------------------------
# Date parsing helpers
# ---------------------------------------------------------------------------
def get_normalized_month_day(prop):
    try:
        nv = getattr(prop, "normalized_value", None)
        if not nv:
            return None

        date_val = getattr(nv, "date_value", None)
        if not date_val:
            return None

        month = getattr(date_val, "month", 0)
        day   = getattr(date_val, "day",   0)
        year  = getattr(date_val, "year",  0)

        if month and day:
            return {
                "month":    int(month),
                "day":      int(day),
                "year":     int(year) if year else None,
                "has_year": bool(year),
            }
    except Exception as e:
        logger.debug("get_normalized_month_day: %s", e)

    return None


def extract_leading_date_text(date_text: str) -> str:
    if not date_text:
        return ""

    compact = re.sub(r"\s+", " ", str(date_text).strip())

    patterns = [
        r"^(?:Mon|Tue|Wed|Thu|Thur|Thurs|Fri|Sat|Sun),?\s+[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}",
        r"^(?:Mon|Tue|Wed|Thu|Thur|Thurs|Fri|Sat|Sun),?\s+[A-Za-z]{3,9}\.?\s+\d{1,2}",
        r"^[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}",
        r"^[A-Za-z]{3,9}\.?\s+\d{1,2}",
        r"^\d{1,2}[/-]\d{1,2}[/-]\d{2,4}",
        r"^\d{1,2}[/-]\d{1,2}",
    ]

    for pattern in patterns:
        m = re.search(pattern, compact, flags=re.IGNORECASE)
        if m:
            return m.group(0).strip()

    return compact


def parse_date_components(date_text: str):
    if not date_text or not str(date_text).strip():
        return None

    compact = extract_leading_date_text(date_text)
    compact = re.sub(r"\s+", " ", compact)
    compact = re.sub(r"^(mon|tue|wed|thu|thur|thurs|fri|sat|sun),?\s+", "", compact, flags=re.IGNORECASE)
    compact = compact.strip(" ,.-")

    # "January 15, 2024" / "Jan 15" / "Jan. 15 2024"
    m = re.fullmatch(
        r"(?P<month_name>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{2,4}))?",
        compact,
        flags=re.IGNORECASE,
    )
    if m:
        month = MONTH_MAP.get(m.group("month_name").lower())
        if month:
            raw_year = m.group("year")
            parsed_year, has_year = None, False
            if raw_year:
                parsed_year = int(raw_year)
                if parsed_year < 100:
                    parsed_year += 2000
                has_year = True
            return {"month": month, "day": int(m.group("day")), "year": parsed_year, "has_year": has_year}

    # "Jan15" / "Jan15/2024"
    m = re.fullmatch(
        r"(?P<month_name>[A-Za-z]{3,9})(?P<day>\d{1,2})(?:[/-]?(?P<year>\d{2,4}))?",
        compact,
        flags=re.IGNORECASE,
    )
    if m:
        month = MONTH_MAP.get(m.group("month_name").lower())
        if month:
            raw_year = m.group("year")
            parsed_year, has_year = None, False
            if raw_year:
                parsed_year = int(raw_year)
                if parsed_year < 100:
                    parsed_year += 2000
                has_year = True
            return {"month": month, "day": int(m.group("day")), "year": parsed_year, "has_year": has_year}

    # "01/15/2024" / "1-15" / "01/15"
    m = re.fullmatch(
        r"(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?:[/-](?P<year>\d{2,4}))?",
        compact,
    )
    if m:
        raw_year = m.group("year")
        parsed_year, has_year = None, False
        if raw_year:
            parsed_year = int(raw_year)
            if parsed_year < 100:
                parsed_year += 2000
            has_year = True
        return {"month": int(m.group("month")), "day": int(m.group("day")), "year": parsed_year, "has_year": has_year}

    return None


def parse_statement_side(side_text: str):
    parsed = parse_date_components(side_text)
    if not parsed:
        return None
    return {"month": parsed["month"], "day": parsed["day"], "year": parsed["year"]}


def build_statement_datetimes(start_info, end_info):
    if not start_info or not end_info:
        return None, None

    start_year = start_info["year"]
    end_year   = end_info["year"]

    if start_year is None and end_year is not None:
        start_year = end_year if start_info["month"] <= end_info["month"] else end_year - 1

    if end_year is None and start_year is not None:
        end_year = start_year if end_info["month"] >= start_info["month"] else start_year + 1

    if start_year is None or end_year is None:
        return None, None

    try:
        start_dt = datetime(start_year, start_info["month"], start_info["day"])
        end_dt   = datetime(end_year,   end_info["month"],   end_info["day"])
        return start_dt, end_dt
    except Exception as e:
        logger.debug("build_statement_datetimes failed: %s", e)
        return None, None


def extract_statement_period(text: str):
    if not text:
        return None

    flat_text = re.sub(r"[ \t]+", " ", text)
    flat_text = re.sub(r"\n+", "\n", flat_text)

    patterns = [
        r"statement\s*from\s*-?\s*to[:\s]*([A-Za-z0-9/,\- ]+?)\s*(?:to|[-–])\s*([A-Za-z0-9/,\- ]+?)(?:\n|$)",
        r"statement\s*from[:\s]*([A-Za-z0-9/,\- ]+?)\s*(?:to|[-–])\s*([A-Za-z0-9/,\- ]+?)(?:\n|$)",
        r"statement\s*period[:\s]*([A-Za-z0-9/,\- ]+?)\s*(?:to|[-–])\s*([A-Za-z0-9/,\- ]+?)(?:\n|$)",
        r"period\s*(?:of|:)\s*([A-Za-z0-9/,\- ]+?)\s*(?:to|[-–])\s*([A-Za-z0-9/,\- ]+?)(?:\n|$)",
        r"\b([A-Za-z]{3,9}\s+\d{1,2}(?:/\d{2,4}|,?\s+\d{2,4})?)\s*[-–]\s*([A-Za-z]{3,9}\s+\d{1,2}(?:/\d{2,4}|,?\s+\d{2,4})?)\b",
        r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\s*(?:to|[-–])\s*(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, flat_text, flags=re.IGNORECASE):
            left  = match.group(1).strip(" :-")
            right = match.group(2).strip(" :-")

            start_info = parse_statement_side(left)
            end_info   = parse_statement_side(right)

            start_dt, end_dt = build_statement_datetimes(start_info, end_info)
            if start_dt and end_dt:
                return {"start": start_dt, "end": end_dt}

    return None


def infer_transaction_year(month: int, day: int, statement_period: dict | None):
    if not statement_period:
        return None

    start_dt = statement_period["start"]
    end_dt   = statement_period["end"]

    valid_candidates = []
    for year in sorted({start_dt.year - 1, start_dt.year, end_dt.year, end_dt.year + 1}):
        try:
            candidate = datetime(year, month, day)
            if start_dt <= candidate <= end_dt:
                valid_candidates.append(candidate)
        except ValueError:
            continue

    if valid_candidates:
        return valid_candidates[0].year

    return start_dt.year if month > end_dt.month else end_dt.year


def normalize_transaction_date(date_text: str, statement_period: dict | None = None) -> str:
    if not date_text or not str(date_text).strip():
        return ""

    parsed = parse_date_components(date_text)
    if not parsed:
        return ""

    year = parsed["year"]
    if year is None:
        year = infer_transaction_year(parsed["month"], parsed["day"], statement_period)

    if year is None:
        return ""

    try:
        return datetime(year, parsed["month"], parsed["day"]).strftime("%m/%d/%Y")
    except Exception as e:
        logger.debug("normalize_transaction_date failed for %r: %s", date_text, e)
        return ""


def is_year_plausible_for_statement(
    year: int | None, month: int, day: int, statement_period: dict | None
) -> bool:
    if year is None:
        return False
    if not statement_period:
        return 2000 <= year <= 2100

    try:
        candidate = datetime(year, month, day)
    except Exception:
        return False

    start_dt = statement_period["start"]
    end_dt   = statement_period["end"]
    return (start_dt <= candidate <= end_dt) or abs(candidate.year - end_dt.year) <= 1


def choose_best_date_parts(date_text: str, normalized_date_parts, statement_period: dict | None = None):
    text_parts = parse_date_components(date_text) if date_text else None

    if text_parts and text_parts.get("has_year"):
        if is_year_plausible_for_statement(
            text_parts["year"], text_parts["month"], text_parts["day"], statement_period
        ):
            return {"month": text_parts["month"], "day": text_parts["day"], "year": text_parts["year"]}
        inferred = infer_transaction_year(text_parts["month"], text_parts["day"], statement_period)
        return {"month": text_parts["month"], "day": text_parts["day"], "year": inferred}

    if text_parts:
        inferred = infer_transaction_year(text_parts["month"], text_parts["day"], statement_period)
        return {"month": text_parts["month"], "day": text_parts["day"], "year": inferred}

    if normalized_date_parts:
        chosen_year = normalized_date_parts.get("year")
        if not is_year_plausible_for_statement(
            chosen_year, normalized_date_parts["month"], normalized_date_parts["day"], statement_period
        ):
            chosen_year = infer_transaction_year(
                normalized_date_parts["month"], normalized_date_parts["day"], statement_period
            )
        return {
            "month": normalized_date_parts["month"],
            "day":   normalized_date_parts["day"],
            "year":  chosen_year,
        }

    return None


def get_statement_period_for_page(
    page_number: int | None, page_text_map: dict[int, str], document_level_period
):
    """Look up statement period from the specific page; fall back to document level."""
    if page_number is not None:
        page_text = page_text_map.get(page_number, "")
        if page_text:
            page_period = extract_statement_period(page_text)
            if page_period:
                return page_period
    return document_level_period


# ---------------------------------------------------------------------------
# Transaction extraction
# ---------------------------------------------------------------------------
def extract_transactions(documents: list) -> list[dict]:
    """
    Walk each chunk in order, tracking the running page offset so that
    get_entity_global_page_number() returns the correct global page for
    every entity regardless of which chunk it came from.
    """
    rows: list[dict] = []

    full_text             = collect_full_text(documents)
    document_level_period = extract_statement_period(full_text)
    page_text_map         = get_page_text_map(documents)

    page_offset = 0
    for doc in documents:
        for entity in doc.entities:
            if entity.type_ != "Transactions":
                continue

            row = {"date": "", "description": "", "debit": "", "credit": "", "balance": ""}
            normalized_date_parts = None

            global_page_num  = get_entity_global_page_number(entity, page_offset)
            statement_period = get_statement_period_for_page(
                global_page_num, page_text_map, document_level_period
            )

            for prop in entity.properties:
                field_name  = prop.type_.strip().lower()
                field_value = (prop.mention_text or "").strip()

                if field_name == "date":
                    normalized_date_parts = get_normalized_month_day(prop)

                if field_name in row:
                    row[field_name] = field_value

            chosen = choose_best_date_parts(row["date"], normalized_date_parts, statement_period)

            if chosen and chosen.get("year") is not None:
                try:
                    row["date"] = datetime(
                        chosen["year"], chosen["month"], chosen["day"]
                    ).strftime("%m/%d/%Y")
                except Exception as e:
                    logger.debug("Date construction failed %s: %s", chosen, e)
                    row["date"] = normalize_transaction_date(row["date"], statement_period)
            else:
                row["date"] = normalize_transaction_date(row["date"], statement_period)

            rows.append(row)

        page_offset += len(doc.pages)

    # Post-process: drop all-empty rows; forward-fill missing dates
    cleaned: list[dict] = []
    last_date = ""
    for row in rows:
        if not any(str(v).strip() for v in row.values()):
            continue
        if not row["date"] and last_date and any(
            row[k] for k in ("description", "debit", "credit", "balance")
        ):
            row["date"] = last_date
        if row["date"]:
            last_date = row["date"]
        cleaned.append(row)

    return cleaned


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------
def process_pdf_to_csv(pdf_path: Path) -> Path:
    ensure_directories()
    pdf_path  = Path(pdf_path)
    documents = process_document_with_chunking(pdf_path)
    save_document_ai_json(documents, pdf_path)

    rows = extract_transactions(documents)
    if not rows:
        raise ValueError("No transaction rows found.")

    df          = pd.DataFrame(rows, columns=["date", "description", "debit", "credit", "balance"])
    output_file = OUTPUT_DIR / f"{pdf_path.stem}.csv"
    df.to_csv(output_file, index=False)
    return output_file


def main():
    ensure_directories()
    pdf_files = list(UPLOAD_DIR.glob("*.pdf"))
    if not pdf_files:
        print("No PDF files found in input/ folder.")
        return

    for pdf_file in pdf_files:
        print(f"\nProcessing: {pdf_file.name}")
        try:
            output_file = process_pdf_to_csv(pdf_file)
            print(f"  → CSV created: {output_file}")
        except Exception as e:
            logger.error("Failed to process %s: %s", pdf_file.name, e)


if __name__ == "__main__":
    main()
