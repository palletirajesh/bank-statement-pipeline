from pathlib import Path
from datetime import datetime
import json
import os
import re
import tempfile

import pandas as pd
from google.cloud import documentai
from google.protobuf.json_format import MessageToDict
from pypdf import PdfReader, PdfWriter


MAX_PAGES_PER_REQUEST = 15

LOCAL_KEY = Path("document-key.json")
if "GOOGLE_APPLICATION_CREDENTIALS" not in os.environ and LOCAL_KEY.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(LOCAL_KEY.resolve())

PROJECT_ID = "418651416236"
LOCATION = "us"
PROCESSOR_ID = "47cfa52a67c0c7a0"

UPLOAD_DIR = Path("input")
OUTPUT_DIR = Path("output")
PROCESSED_DIR = Path("processed")
JSON_DIR = Path("json_output")

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "aprl": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def ensure_directories():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)


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
        except Exception:
            pass


def process_document(file_path: Path):
    client = documentai.DocumentProcessorServiceClient()
    name = client.processor_path(PROJECT_ID, LOCATION, PROCESSOR_ID)

    with open(file_path, "rb") as f:
        file_bytes = f.read()

    raw_document = documentai.RawDocument(
        content=file_bytes,
        mime_type="application/pdf",
    )

    request = documentai.ProcessRequest(
        name=name,
        raw_document=raw_document,
    )

    result = client.process_document(request=request)
    return result.document


def process_document_with_chunking(file_path: Path):
    page_count = get_pdf_page_count(file_path)

    if page_count <= MAX_PAGES_PER_REQUEST:
        return process_document(file_path)

    chunk_paths = split_pdf_into_chunks(file_path, MAX_PAGES_PER_REQUEST)
    try:
        docs = [process_document(chunk_path) for chunk_path in chunk_paths]
        return docs
    finally:
        cleanup_temp_files(chunk_paths)


def save_document_ai_json(document_or_documents, pdf_path: Path):
    ensure_directories()

    if isinstance(document_or_documents, list):
        json_payload = []
        for idx, doc in enumerate(document_or_documents, start=1):
            json_payload.append({
                "chunk_number": idx,
                "document": MessageToDict(doc._pb),
            })
    else:
        json_payload = MessageToDict(document_or_documents._pb)

    json_file = JSON_DIR / f"{pdf_path.stem}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(json_payload, f, indent=2, ensure_ascii=False)

    return json_file


def get_all_entities(document_or_documents):
    if isinstance(document_or_documents, list):
        entities = []
        for doc in document_or_documents:
            entities.extend(doc.entities)
        return entities
    return document_or_documents.entities


def collect_full_text(document_or_documents) -> str:
    if isinstance(document_or_documents, list):
        return "\n".join((doc.text or "") for doc in document_or_documents if doc)
    return document_or_documents.text or ""


def get_page_text_map(document_or_documents) -> dict[int, str]:
    page_text_map: dict[int, str] = {}
    docs = document_or_documents if isinstance(document_or_documents, list) else [document_or_documents]

    global_page_number = 1
    for doc in docs:
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
                    end_index = int(seg.end_index)
                    token_text_parts.append(full_text[start_index:end_index])

                token_text = "".join(token_text_parts).strip()
                if token_text:
                    page_parts.append(token_text)

            page_text_map[global_page_number] = " ".join(page_parts)
            global_page_number += 1

    return page_text_map


def get_entity_page_number(entity) -> int | None:
    page_refs = []

    page_anchor = getattr(entity, "page_anchor", None)
    if page_anchor and getattr(page_anchor, "page_refs", None):
        page_refs.extend(page_anchor.page_refs)

    if not page_refs:
        for prop in entity.properties:
            prop_page_anchor = getattr(prop, "page_anchor", None)
            if prop_page_anchor and getattr(prop_page_anchor, "page_refs", None):
                page_refs.extend(prop_page_anchor.page_refs)

    if not page_refs:
        return None

    first_ref = page_refs[0]
    page_num = getattr(first_ref, "page", None)
    if page_num is None:
        return None

    return int(page_num) + 1


def get_normalized_month_day(prop):
    try:
        nv = getattr(prop, "normalized_value", None)
        if not nv:
            return None

        date_val = getattr(nv, "date_value", None)
        if not date_val:
            return None

        month = getattr(date_val, "month", 0)
        day = getattr(date_val, "day", 0)
        year = getattr(date_val, "year", 0)

        if month and day:
            return {
                "month": int(month),
                "day": int(day),
                "year": int(year) if year else None,
                "has_year": bool(year),
            }
    except Exception:
        pass

    return None


def extract_leading_date_text(date_text: str) -> str:
    if not date_text:
        return ""

    text = str(date_text).strip()
    compact = re.sub(r"\s+", " ", text)

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

    m = re.fullmatch(
        r"(?P<month_name>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2})(?:,?\s+(?P<year>\d{2,4}))?",
        compact,
        flags=re.IGNORECASE,
    )
    if m:
        month = MONTH_MAP.get(m.group("month_name").lower())
        if month:
            year = m.group("year")
            parsed_year = None
            has_year = False
            if year:
                parsed_year = int(year)
                if parsed_year < 100:
                    parsed_year += 2000
                has_year = True
            return {
                "month": month,
                "day": int(m.group("day")),
                "year": parsed_year,
                "has_year": has_year,
            }

    m = re.fullmatch(
        r"(?P<month_name>[A-Za-z]{3,9})(?P<day>\d{1,2})(?:[/-]?(?P<year>\d{2,4}))?",
        compact,
        flags=re.IGNORECASE,
    )
    if m:
        month = MONTH_MAP.get(m.group("month_name").lower())
        if month:
            year = m.group("year")
            parsed_year = None
            has_year = False
            if year:
                parsed_year = int(year)
                if parsed_year < 100:
                    parsed_year += 2000
                has_year = True
            return {
                "month": month,
                "day": int(m.group("day")),
                "year": parsed_year,
                "has_year": has_year,
            }

    m = re.fullmatch(
        r"(?P<month>\d{1,2})[/-](?P<day>\d{1,2})(?:[/-](?P<year>\d{2,4}))?",
        compact,
    )
    if m:
        year = m.group("year")
        parsed_year = None
        has_year = False
        if year:
            parsed_year = int(year)
            if parsed_year < 100:
                parsed_year += 2000
            has_year = True

        return {
            "month": int(m.group("month")),
            "day": int(m.group("day")),
            "year": parsed_year,
            "has_year": has_year,
        }

    return None

def parse_statement_side(side_text: str):
    parsed = parse_date_components(side_text)
    if not parsed:
        return None

    return {
        "month": parsed["month"],
        "day": parsed["day"],
        "year": parsed["year"],
    }


def build_statement_datetimes(start_info, end_info):
    if not start_info or not end_info:
        return None, None

    start_year = start_info["year"]
    end_year = end_info["year"]

    if start_year is None and end_year is not None:
        start_year = end_year if start_info["month"] <= end_info["month"] else end_year - 1

    if end_year is None and start_year is not None:
        end_year = start_year if end_info["month"] >= start_info["month"] else start_year + 1

    if start_year is None or end_year is None:
        return None, None

    try:
        start_dt = datetime(start_year, start_info["month"], start_info["day"])
        end_dt = datetime(end_year, end_info["month"], end_info["day"])
        return start_dt, end_dt
    except Exception:
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
        r"\b([A-Za-z]{3,9}\s+\d{1,2}(?:/\d{2,4}|,?\s+\d{2,4})?)\s*[-–]\s*([A-Za-z]{3,9}\s+\d{1,2}(?:/\d{2,4}|,?\s+\d{2,4})?)\b",
        r"\b(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\s*(?:to|[-–])\s*(\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, flat_text, flags=re.IGNORECASE):
            left = match.group(1).strip(" :-")
            right = match.group(2).strip(" :-")

            start_info = parse_statement_side(left)
            end_info = parse_statement_side(right)

            start_dt, end_dt = build_statement_datetimes(start_info, end_info)
            if start_dt and end_dt:
                return {"start": start_dt, "end": end_dt}

    return None


def infer_transaction_year(month: int, day: int, statement_period: dict | None):
    if not statement_period:
        return None

    start_dt = statement_period["start"]
    end_dt = statement_period["end"]

    candidate_years = {start_dt.year - 1, start_dt.year, end_dt.year, end_dt.year + 1}

    valid_candidates = []
    for year in sorted(candidate_years):
        try:
            candidate = datetime(year, month, day)
            if start_dt <= candidate <= end_dt:
                valid_candidates.append(candidate)
        except ValueError:
            continue

    if valid_candidates:
        return valid_candidates[0].year

    if month > end_dt.month:
        return start_dt.year
    return end_dt.year


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
        dt = datetime(year, parsed["month"], parsed["day"])
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return ""


def is_year_plausible_for_statement(year: int | None, month: int, day: int, statement_period: dict | None) -> bool:
    if year is None:
        return False
    if not statement_period:
        return 2000 <= year <= 2100

    try:
        candidate = datetime(year, month, day)
    except Exception:
        return False

    start_dt = statement_period["start"]
    end_dt = statement_period["end"]

    if start_dt <= candidate <= end_dt:
        return True

    return abs(candidate.year - end_dt.year) <= 1


def choose_best_date_parts(date_text: str, normalized_date_parts, statement_period: dict | None = None):
    text_parts = parse_date_components(date_text) if date_text else None

    if text_parts and text_parts.get("has_year"):
        if is_year_plausible_for_statement(
            text_parts["year"],
            text_parts["month"],
            text_parts["day"],
            statement_period,
        ):
            return {
                "month": text_parts["month"],
                "day": text_parts["day"],
                "year": text_parts["year"],
            }

        inferred_year = infer_transaction_year(
            text_parts["month"],
            text_parts["day"],
            statement_period,
        )
        return {
            "month": text_parts["month"],
            "day": text_parts["day"],
            "year": inferred_year,
        }

    if text_parts:
        inferred_year = infer_transaction_year(
            text_parts["month"],
            text_parts["day"],
            statement_period,
        )
        return {
            "month": text_parts["month"],
            "day": text_parts["day"],
            "year": inferred_year,
        }

    if normalized_date_parts:
        chosen_year = normalized_date_parts.get("year")
        if not is_year_plausible_for_statement(
            chosen_year,
            normalized_date_parts["month"],
            normalized_date_parts["day"],
            statement_period,
        ):
            chosen_year = infer_transaction_year(
                normalized_date_parts["month"],
                normalized_date_parts["day"],
                statement_period,
            )

        return {
            "month": normalized_date_parts["month"],
            "day": normalized_date_parts["day"],
            "year": chosen_year,
        }

    return None

def get_statement_period_for_entity(entity, page_text_map: dict[int, str], document_level_period):
    page_number = get_entity_page_number(entity)

    if page_number is not None:
        page_text = page_text_map.get(page_number, "")
        if page_text:
            page_period = extract_statement_period(page_text)
            if page_period:
                return page_period

    return document_level_period


def extract_transactions(document_or_documents):
    rows = []

    full_text = collect_full_text(document_or_documents)
    document_level_period = extract_statement_period(full_text)
    page_text_map = get_page_text_map(document_or_documents)
    entities = get_all_entities(document_or_documents)

    for entity in entities:
        if entity.type_ != "Transactions":
            continue

        row = {
            "date": "",
            "description": "",
            "debit": "",
            "credit": "",
            "balance": "",
        }

        normalized_date_parts = None
        statement_period = get_statement_period_for_entity(entity, page_text_map, document_level_period)

        for prop in entity.properties:
            field_name = prop.type_.strip().lower()
            field_value = (prop.mention_text or "").strip()

            if field_name == "date":
                normalized_date_parts = get_normalized_month_day(prop)

            if field_name in row:
                row[field_name] = field_value

        chosen_date_parts = choose_best_date_parts(
            row["date"],
            normalized_date_parts,
            statement_period,
        )

        if chosen_date_parts and chosen_date_parts.get("year") is not None:
            try:
                row["date"] = datetime(
                    chosen_date_parts["year"],
                    chosen_date_parts["month"],
                    chosen_date_parts["day"],
                ).strftime("%m/%d/%Y")
            except Exception:
                row["date"] = normalize_transaction_date(row["date"], statement_period)
        else:
            row["date"] = normalize_transaction_date(row["date"], statement_period)

        rows.append(row)

    cleaned_rows = []
    last_date = ""

    for row in rows:
        non_empty_count = sum(1 for v in row.values() if str(v).strip())
        if non_empty_count == 0:
            continue

        if not row["date"] and last_date and (
            row["description"] or row["debit"] or row["credit"] or row["balance"]
        ):
            row["date"] = last_date

        if row["date"]:
            last_date = row["date"]

        cleaned_rows.append(row)

    return cleaned_rows


def process_pdf_to_csv(pdf_path: Path) -> Path:
    ensure_directories()

    pdf_path = Path(pdf_path)
    document_or_documents = process_document_with_chunking(pdf_path)
    save_document_ai_json(document_or_documents, pdf_path)

    rows = extract_transactions(document_or_documents)
    if not rows:
        raise ValueError("No transaction rows found.")

    df = pd.DataFrame(rows, columns=["date", "description", "debit", "credit", "balance"])
    output_file = OUTPUT_DIR / f"{pdf_path.stem}.csv"
    df.to_csv(output_file, index=False)
    return output_file


def main():
    ensure_directories()

    pdf_files = list(UPLOAD_DIR.glob("*.pdf"))
    if not pdf_files:
        print("No PDF files found in input folder.")
        return

    for pdf_file in pdf_files:
        print(f"\nProcessing: {pdf_file.name}")
        try:
            output_file = process_pdf_to_csv(pdf_file)
            print(f"CSV created: {output_file}")
        except Exception as e:
            print(f"Failed: {pdf_file.name} -> {e}")


if __name__ == "__main__":
    main()
