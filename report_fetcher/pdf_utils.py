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

from .classify import (
    is_relevant_pdf,
    pick_canonical_report_type_strict,
    pick_canonical_report_type_with_llm,
    verify_issuer,
)
from .config import PDF_DIR  # if you use it elsewhere
from .config import (  # Add these to config.py if not present:; TEMP_PDF_DIR = BASE_DIR / "tmp_pdfs"; CONFIRMED_PDF_DIR = PDF_DIR
    BASE_DIR,
    ISSUER_GATE_MODE,
    USE_LLM_TYPE_MATCHING,
)
from .fetch import download_pdf_detailed

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
    from .config import SHADOW_PDF_DIR
except Exception:
    SHADOW_PDF_DIR = BASE_DIR / "pdfs_shadow_rejected"

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


def extract_pdf_metadata_dates(
    pdf_path: str | Path,
) -> tuple[Optional[_dt.date], Optional[_dt.date]]:
    """
    Read the PDF's document-info dates and return (created_date, modified_date).
    PyMuPDF exposes them as "D:YYYYMMDDHHmmSS..." strings; anything unparsable
    (or with an implausible year) comes back as None.
    """

    def _parse(value: Optional[str]) -> Optional[_dt.date]:
        if not value:
            return None
        s = str(value).strip()
        # Strict PDF date prefix first: D:YYYYMMDD[HHmmSS...]; then any bare YYYYMMDD.
        m = re.match(r"D:(\d{4})(\d{2})(\d{2})", s) or re.search(r"(\d{4})(\d{2})(\d{2})", s)
        if not m:
            return None
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if not (1900 <= year <= 2100):
            return None
        try:
            return _dt.date(year, month, day)
        except ValueError:
            return None

    try:
        with pymupdf.open(str(pdf_path)) as doc:
            md = doc.metadata or {}
        return _parse(md.get("creationDate")), _parse(md.get("modDate"))
    except Exception as e:
        logger.debug("Could not read PDF metadata dates from %s: %s", pdf_path, e)
        return None, None


def check_and_save_pdf(
    pdf_url: str,
    city_name_for_filename: str,
    country: str,
    *,
    client,  # OpenAI client to pass into is_relevant_pdf
    temp_dir: Path = TEMP_PDF_DIR,
    confirmed_dir: Path = CONFIRMED_PDF_DIR,
    min_year: Optional[int] = MIN_ACCEPTABLE_REPORT_YEAR,
    page_url: Optional[str] = None,
    shadow_dir: Path = SHADOW_PDF_DIR,
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
            "ai_year_raw", "final_year", "year_source",
            "publish_date", "year_start", "year_end",
            "metadata_created_date", "metadata_modified_date",
            "language", "llm_response", "save_date" (present only when status == "saved")
          }
    """
    meta = {
        # The URL the PDF bytes actually came from (updated after download when
        # the input link was an HTML page hosting the PDF).
        "url": pdf_url,
        # The page the PDF was resolved from, when it differs from "url"; ""
        # for direct PDF links.
        "source_page_url": "",
        # Short reason the download failed (only set on download_failed);
        # surfaced in the failed-rows report for diagnostics.
        "download_error": "",
        "city": (
            str(city_name_for_filename) if pd.notna(city_name_for_filename) else "UnknownCity"
        ),
        "country": country,
        "ai_report_type": None,
        "canonical_type": None,
        "ai_year_raw": None,
        "final_year": "unknown year",
        "year_source": "",
        "publish_date": "",
        "year_start": "",
        "year_end": "",
        "metadata_created_date": "",
        "metadata_modified_date": "",
        "language": "unknown",  # <- added
        "ai_reasoning": None,
        "llm_type": None,
        "llm_response": None,
        # Issuer gate results (set only when the gate runs; see ISSUER_GATE_MODE)
        "gate_verdict": "",
        "gate_doc_class": "",
        "gate_issuer": "",
        "gate_reason": "",
        # True when the gate would have rejected but shadow mode kept the file;
        # such files are written to SHADOW_PDF_DIR instead of CONFIRMED_PDF_DIR.
        "gate_quarantined": False,
        "saved_dir": "",
        # "save_date" will be set only when actually saved
    }

    city_name_str = str(meta["city"])
    temp_dir = Path(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    confirmed_dir = Path(confirmed_dir)
    confirmed_dir.mkdir(parents=True, exist_ok=True)
    shadow_dir = Path(shadow_dir)

    # Build a temp filename that is a hash of the URL
    url_hash = hashlib.md5(pdf_url.encode("utf-8")).hexdigest()
    filename = f"{url_hash}.pdf"
    temp_path = temp_dir / filename

    logger.info("Processing PDF: %s", pdf_url)

    ok, resolved_url, dl_error = download_pdf_detailed(
        pdf_url, save_path=temp_path, page_url=page_url
    )
    if ok and resolved_url and resolved_url != pdf_url:
        # The input link was a page; record the real PDF URL as primary and
        # keep the page for provenance.
        meta["url"] = resolved_url
        meta["source_page_url"] = pdf_url
        logger.info("Resolved page -> PDF: %s -> %s", pdf_url, resolved_url)
    if not ok:
        meta["download_error"] = dl_error or ""
        logger.warning("Download failed or not a PDF: %s (%s)", pdf_url, dl_error or "unknown")
        # if temp_path.exists():
        #     try:
        #         temp_path.unlink()
        #     except Exception as cleanup_e:
        #         logger.debug("Cleanup error for %s: %s", temp_path, cleanup_e)
        return ("download_failed", None, meta)

    # Extract text (first 15 pages)
    text = extract_text_from_pdf(temp_path, max_pages=15)

    # Document-info dates from the PDF itself (independent of the LLM call).
    metadata_created, metadata_modified = extract_pdf_metadata_dates(temp_path)
    meta["metadata_created_date"] = metadata_created.isoformat() if metadata_created else ""
    meta["metadata_modified_date"] = metadata_modified.isoformat() if metadata_modified else ""

    # Call LLM to determine relevance, type, year (+ language if available)
    result = is_relevant_pdf(
        text=text,
        city_name=city_name_str,
        country=country,
        client=client,
    )
    try:
        final_rel, report_type_from_ai, year_from_ai, lang_code, reasoning_from_ai, raw_resp, llm_type_from_ai, year_start_from_ai, publish_date_from_ai = result
    except ValueError:
        logger.warning("is_relevant_pdf returned unexpected tuple length %d; using defensive fallback.", len(result))
        publish_date_from_ai = ""
        year_start_from_ai = ""
        if len(result) == 8:
            final_rel, report_type_from_ai, year_from_ai, lang_code, reasoning_from_ai, raw_resp, llm_type_from_ai, year_start_from_ai = result
        elif len(result) == 7:
            final_rel, report_type_from_ai, year_from_ai, lang_code, reasoning_from_ai, raw_resp, llm_type_from_ai = result
        elif len(result) == 6:
            final_rel, report_type_from_ai, year_from_ai, lang_code, reasoning_from_ai, raw_resp = result
            llm_type_from_ai = ""
        elif len(result) == 5:
            final_rel, report_type_from_ai, year_from_ai, lang_code, raw_resp = result
            reasoning_from_ai = ""
            llm_type_from_ai = ""
        elif len(result) == 4:
            final_rel, report_type_from_ai, year_from_ai, lang_code = result
            reasoning_from_ai = ""
            raw_resp = ""
            llm_type_from_ai = ""
        elif len(result) == 3:
            final_rel, report_type_from_ai, year_from_ai = result
            lang_code = "unknown"
            reasoning_from_ai = ""
            raw_resp = ""
            llm_type_from_ai = ""
        else:
            raise

    meta["ai_report_type"] = (report_type_from_ai or "").strip()
    meta["ai_year_raw"] = year_from_ai
    # Earlier year when the report covers a range (already validated < Year by
    # classify.py); "" for single-year reports. Informational only — gating
    # stays on the later year so future-horizon plans remain acceptable.
    meta["year_start"] = (year_start_from_ai or "").strip()
    # Explicit publication date stated in the document (YYYY[-MM[-DD]]); ""
    # when the model found none. Never derived from the period/title range.
    meta["publish_date"] = (publish_date_from_ai or "").strip()
    meta["language"] = (lang_code or "unknown").lower()
    meta["ai_reasoning"] = reasoning_from_ai
    meta["llm_type"] = (llm_type_from_ai or "").strip()
    meta["llm_response"] = raw_resp

    # --- Year handling ---
    # A document is too old only when EVERY year the LLM gave us is below the
    # cutoff. Two signals matter and either one can rescue a document:
    #   * period year   — for ranges the LATER year, so future-horizon plans
    #                     ("Plan 2015-2030") stay acceptable;
    #   * publication date — rescues backward-looking documents, above all GHG
    #                     inventories, whose period year is the DATA year. A
    #                     Curitiba inventory published July 2025 reporting on
    #                     2017-2020 was being dropped on its 2020 coverage year.
    current_year = _dt.datetime.now().year
    min_ok = min_year if min_year is not None else (current_year - 2)

    period_year_raw: Optional[int] = None
    if year_from_ai and year_from_ai != "unknown year":
        cleaned = re.sub(r"\D+$", "", year_from_ai)
        try:
            period_year_raw = int(cleaned)
        except ValueError:
            logger.debug("Could not parse AI year '%s'.", cleaned)

    publication_year: Optional[int] = None
    if meta["publish_date"]:
        try:
            publication_year = int(str(meta["publish_date"])[:4])
        except (TypeError, ValueError):
            logger.debug("Could not parse publication date %r.", meta["publish_date"])

    llm_years = [y for y in (period_year_raw, publication_year) if y is not None]
    too_old = bool(llm_years) and max(llm_years) < min_ok
    if too_old:
        logger.info(
            "Newest LLM year %s (period=%s, published=%s) before minimum acceptable year %s.",
            max(llm_years), period_year_raw, publication_year, min_ok,
        )

    # Only a period year that clears the gate feeds the naming/fallback chain.
    period_year = period_year_raw if (period_year_raw is not None and period_year_raw >= min_ok) else None

    # Upper year of a coverage range, stored only when a range was detected
    # (mirrors year_start); for single-year reports it stays "".
    meta["year_end"] = str(period_year) if (period_year and meta["year_start"]) else ""

    def _usable_year(y: int) -> bool:
        # Naming must never use a future year (metadata clock skew etc.).
        return 1900 <= y <= current_year

    # Publication-year resolution for naming/storage, in priority order:
    # AI publication date -> metadata created -> metadata modified -> period
    # year (for ranges: the upper year) -> unknown.
    final_year = "unknown year"
    year_source = ""
    if meta["publish_date"]:
        final_year = meta["publish_date"][:4]
        year_source = "ai_publication_date"
    elif metadata_created and _usable_year(metadata_created.year):
        final_year = str(metadata_created.year)
        year_source = "metadata_created"
    elif metadata_modified and _usable_year(metadata_modified.year):
        final_year = str(metadata_modified.year)
        year_source = "metadata_modified"
    elif period_year:
        final_year = str(period_year)
        year_source = "period_year"
    meta["final_year"] = final_year
    meta["year_source"] = year_source
    logger.debug("Resolved report year %s (source: %s)", final_year, year_source or "none")

    # When the LLM supplied no year at all, the age check above had nothing to
    # decide on and the document was saved on a PDF-metadata year alone — that
    # is how a 2015 brochure landed in a run gated at 2023. Fall back to the
    # metadata year, which is the only remaining signal.
    if not llm_years and year_source:
        try:
            if int(final_year) < min_ok:
                too_old = True
                logger.info(
                    "Resolved year %s (source: %s) before minimum acceptable year %s.",
                    final_year,
                    year_source,
                    min_ok,
                )
        except (TypeError, ValueError):
            logger.debug("Could not parse resolved year %r for the age gate.", final_year)

    # Canonical type resolution — cascade or legacy strict-only based on config flag
    if USE_LLM_TYPE_MATCHING:
        canonical = pick_canonical_report_type_with_llm(meta["llm_type"], meta["ai_report_type"])
    else:
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
        pass  # fall through to type_mismatch below (gate only runs on saveable docs)
    elif ISSUER_GATE_MODE in ("shadow", "enforce"):
        # Focused issuer/document-class check on documents about to be saved.
        gate = verify_issuer(text=text, city_name=city_name_str, country=country, client=client)
        meta["gate_verdict"] = gate["verdict"]
        meta["gate_doc_class"] = gate["doc_class"]
        meta["gate_issuer"] = gate["issuer"]
        meta["gate_reason"] = gate["reason"]
        if gate["verdict"] == "reject":
            if ISSUER_GATE_MODE == "enforce":
                logger.info(
                    "[ISSUER GATE] Rejected (%s, issuer: %s): %s",
                    gate["doc_class"], gate["issuer"], pdf_url,
                )
                return ("issuer_rejected", None, meta)
            logger.info(
                "[ISSUER GATE shadow] Would reject (%s, issuer: %s) — quarantining to %s: %s",
                gate["doc_class"], gate["issuer"], shadow_dir.name, pdf_url,
            )
            meta["gate_quarantined"] = True

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
    raw_report_type = meta["ai_report_type"] or ""
    safe_report_type = re.sub(r"[^\w\-_. ]", "", raw_report_type)
    # Range-covering reports are named with their coverage range ("2015-2030")
    # — the range identifies the plan better than a publish year, and the end
    # year alone can't read as a publication date. Everything else carries the
    # resolved publication year (see the priority chain above).
    year_segment = (
        f"{meta['year_start']}-{meta['year_end']}" if meta["year_end"] else final_year
    )
    final_year_for_filename = year_segment.replace(" ", "_")

    # Shadow-mode gate rejections are still kept for review, but land in the
    # quarantine folder so pdfs/ holds only documents nothing objected to.
    target_dir = shadow_dir if meta.get("gate_quarantined") else confirmed_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    meta["saved_dir"] = target_dir.name

    confirmed_filename = (
        f"{safe_city}_{safe_report_type}_{final_year_for_filename}.pdf"
    )
    confirmed_path = target_dir / confirmed_filename

    # Resolve collisions
    counter = 1
    while confirmed_path.exists():
        confirmed_filename = (
            f"{safe_city}_{safe_report_type}_{final_year_for_filename}_{counter}.pdf"
        )
        confirmed_path = target_dir / confirmed_filename
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
                safe_report_type,
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
