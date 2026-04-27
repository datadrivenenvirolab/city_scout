# pdf_utils.py
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse

import pymupdf  # PyMuPDF
import pandas as pd

from .classify import is_relevant_pdf, pick_canonical_report_type_strict
from .config import PDF_DIR  # if you use it elsewhere
from .config import (  # Add these to config.py if not present:; TEMP_PDF_DIR = BASE_DIR / "tmp_pdfs"; CONFIRMED_PDF_DIR = PDF_DIR
    BASE_DIR,
)
from .fetch import download_pdf

# Optional imports from config; provide sane fallbacks if missing
try:
    from .config import TEMP_PDF_DIR
except Exception:
    TEMP_PDF_DIR = BASE_DIR / "tmp_pdfs"

try:
    from .config import CONFIRMED_PDF_DIR
except Exception:
    CONFIRMED_PDF_DIR = PDF_DIR

try:
    from .config import MIN_ACCEPTABLE_REPORT_YEAR
except Exception:
    MIN_ACCEPTABLE_REPORT_YEAR = _dt.datetime.now().year - 2

logger = logging.getLogger(__name__)


def extract_text_from_pdf(pdf_path: str | Path, max_pages: int = 10) -> str:
    """
    Extract text from the first `max_pages` pages of a PDF using PyMuPDF.
    Returns raw text (preserves casing). Caller may lowercase if needed for matching.
    """
    pdf_path = Path(pdf_path)
    text_chunks: list[str] = []
    try:
        with pymupdf.open(pdf_path) as doc:
            n = min(max_pages, len(doc))
            for i in range(n):
                page = doc[i]
                # "text" is default; explicit to be clear
                text_chunks.append(page.get_text("text"))
        return "\n".join(text_chunks)
    except Exception as e:
        logger.error("Error reading PDF %s: %s", pdf_path, e, exc_info=True)
        return ""


def check_and_save_pdf(
    pdf_url: str,
    city_name_for_filename: str,
    country: str,
    *,
    client,  # OpenAI client to pass into is_relevant_pdf
    temp_dir: Path = TEMP_PDF_DIR,
    confirmed_dir: Path = CONFIRMED_PDF_DIR,
    min_year: Optional[int] = MIN_ACCEPTABLE_REPORT_YEAR,
) -> Tuple[str, Optional[str], dict]:
    """
    Download a PDF, classify relevance/type/year (and language), and save it if acceptable.

    Returns:
        (status, saved_path, meta)
        - status in {"saved","download_failed","too_old","irrelevant","classification_failed","type_mismatch"}
        - saved_path: str path if saved, else None
        - meta: {
            "url", "city", "country",
            "ai_report_type", "canonical_type",
            "ai_year_raw", "final_year",
            "language", "llm_response", "save_date" (present only when status == "saved")
          }
    """
    meta = {
        "url": pdf_url,
        "city": (
            str(city_name_for_filename) if pd.notna(city_name_for_filename) else "UnknownCity"
        ),
        "country": country,
        "ai_report_type": None,
        "canonical_type": None,
        "ai_year_raw": None,
        "final_year": "unknown year",
        "language": "unknown",  # <- added
        "ai_reasoning": None,
        "llm_response": None,
        # "save_date" will be set only when actually saved
    }

    city_name_str = str(meta["city"])
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    confirmed_dir = Path(confirmed_dir)
    confirmed_dir.mkdir(parents=True, exist_ok=True)

    # Build a temp filename that is a hash of the URL
    url_hash = hashlib.md5(pdf_url.encode("utf-8")).hexdigest()
    filename = f"{url_hash}.pdf"
    temp_path = temp_dir / filename

    logger.info("Processing PDF: %s", pdf_url)

    ok = download_pdf(pdf_url, save_path=temp_path)
    if not ok:
        logger.warning("Download failed or not a PDF: %s", pdf_url)
        # if temp_path.exists():
        #     try:
        #         temp_path.unlink()
        #     except Exception as cleanup_e:
        #         logger.debug("Cleanup error for %s: %s", temp_path, cleanup_e)
        return ("download_failed", None, meta)

    # Extract text (first 15 pages)
    text = extract_text_from_pdf(temp_path, max_pages=15)

    # Call LLM to determine relevance, type, year (+ language if available)
    result = is_relevant_pdf(
        text=text,
        city_name=city_name_str,
        country=country,
        client=client,
    )
    try:
        final_rel, report_type_from_ai, year_from_ai, lang_code, reasoning_from_ai, raw_resp = result
    except Exception:
        if len(result) == 6:
            final_rel, report_type_from_ai, year_from_ai, lang_code, reasoning_from_ai, raw_resp = result
        elif len(result) == 5:
            final_rel, report_type_from_ai, year_from_ai, lang_code, raw_resp = result
            reasoning_from_ai = ""
        elif len(result) == 4:
            final_rel, report_type_from_ai, year_from_ai, lang_code = result
            reasoning_from_ai = ""
            raw_resp = ""
        elif len(result) == 3:
            final_rel, report_type_from_ai, year_from_ai = result
            lang_code = "unknown"
            reasoning_from_ai = ""
            raw_resp = ""
        else:
            raise Exception("Unexpected result format from is_relevant_pdf")

    meta["ai_report_type"] = (report_type_from_ai or "").strip()
    meta["ai_year_raw"] = year_from_ai
    meta["language"] = (lang_code or "unknown").lower()
    meta["ai_reasoning"] = reasoning_from_ai
    meta["llm_response"] = raw_resp

    # --- Year handling ---
    final_year = "unknown year"
    too_old = False
    if year_from_ai and year_from_ai != "unknown year":
        cleaned = re.sub(r"\D+$", "", year_from_ai)
        try:
            y = int(cleaned)
            current_year = _dt.datetime.now().year
            min_ok = min_year if min_year is not None else (current_year - 2)
            if min_ok <= y <= current_year:
                final_year = str(y)
                logger.debug("Using AI year: %s", final_year)
            else:
                too_old = True
                logger.info("AI year %s outside acceptable range (%s-%s).", y, min_ok, current_year)
        except ValueError:
            logger.debug("Could not parse AI year '%s'.", cleaned)
    meta["final_year"] = final_year

    # Canonical type (strict, no upgrades)
    canonical = pick_canonical_report_type_strict(meta["ai_report_type"])
    meta["canonical_type"] = canonical

    # --- Decide outcomes ---
    if not final_rel:
        # irrelevant
        # try:
        #     if temp_path.exists():
        #         temp_path.unlink()
        # except Exception:
        #     logger.debug("Failed to remove temp file %s", temp_path, exc_info=True)
        logger.info("Irrelevant by AI: %s", pdf_url)
        return ("irrelevant", None, meta)

    if too_old:
        # try:
        #     if temp_path.exists():
        #         temp_path.unlink()
        # except Exception:
        #     logger.debug("Failed to remove temp file %s", temp_path, exc_info=True)
        logger.info("Report too old: %s", pdf_url)
        return ("too_old", None, meta)
    logger.info(canonical)
    if not canonical:
        # relevant but type not in accepted list
        # try:
        #     if temp_path.exists():
        #         temp_path.unlink()
        # except Exception:
        #     logger.debug("Failed to remove temp file %s", temp_path, exc_info=True)
        logger.info(
            "[TYPE MISMATCH] Relevant but AI type '%s' not accepted.", meta["ai_report_type"]
        )
        return ("type_mismatch", None, meta)

    # Save to confirmed location with sanitized filename
    safe_city = re.sub(r"[^\w\-_. ]", "", city_name_str)
    final_report_type_for_filename = canonical
    final_year_for_filename = final_year.replace(" ", "_")

    confirmed_filename = (
        f"{safe_city}_{final_report_type_for_filename}_{final_year_for_filename}.pdf"
    )
    confirmed_path = confirmed_dir / confirmed_filename

    # Resolve collisions
    counter = 1
    while confirmed_path.exists():
        confirmed_filename = (
            f"{safe_city}_{final_report_type_for_filename}_{final_year_for_filename}_{counter}.pdf"
        )
        confirmed_path = confirmed_dir / confirmed_filename
        counter += 1

    # Move temp -> confirmed (works across filesystems)
    if temp_path.exists():
        try:
            shutil.move(str(temp_path), str(confirmed_path))
            # Record save date for stage2_results.csv
            meta["save_date"] = _dt.date.today().isoformat()  # YYYY-MM-DD
            logger.info(
                "Saved PDF: %s (Type: %s, Year: %s)",
                confirmed_path,
                final_report_type_for_filename,
                final_year,
            )
            return ("saved", str(confirmed_path), meta)
        except Exception as e:
            logger.error("Failed to move %s -> %s: %s", temp_path, confirmed_path, e, exc_info=True)
            # leave temp file for debugging
            return ("classification_failed", None, meta)
    else:
        logger.error("Temp file missing before save: %s", temp_path)
        return ("classification_failed", None, meta)
