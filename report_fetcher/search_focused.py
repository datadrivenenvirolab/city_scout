# search_focused.py
# Focused 3-query Stage 1 search strategy.
# Replaces the OR-chunked multi-term approach in search.py with three semantically
# distinct, short queries per city, merged via round-robin into the final link list.
#
# search.py is untouched. To revert, change the import in pipeline_stage1.py back to:
#   from .search import process_companies_for_reports

from __future__ import annotations

import itertools
import json
import logging
import math
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from ddgs import DDGS
from tqdm import tqdm

from .prompt_loader import load_yaml
from .search import _validate_pdf_links
from .search_vars import _COUNTRY_TO_LANG, _GOOGLE_DOMAIN
from . import new_functions
from .config import (
    CITY_NAME_COLUMN,
    COLUMNS,
    COUNTRY_NAME_COLUMN,
    MAX_PAGE_RESULTS,
    MAX_PDF_RESULTS,
    RESULTS_PER_QUERY,
    SCALESERP_API_KEY,
    SEARCH_CACHE_FILE,
    SEARCH_FOR_AFTER,
    SEARCH_FOR_AFTER_COLUMN,
    SUBREGION_NAME_COLUMN,
    REGION_NAME_COLUMN,
    USE_AFTER_YEAR_FILTER,
    USE_DDGS,
    USE_SEARCH_CACHE,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# English focused query term groups (hardcoded; not in the YAML)
# ---------------------------------------------------------------------------
_EN_Q1 = '"climate action plan" OR "climate strategy" OR "climate plan"'
_EN_Q2 = '"net zero" OR "carbon neutral" OR "CAAP" OR "SEAP"'
_EN_Q3 = '"climate adaptation" OR "energy action plan" OR "climate change mitigation"'


# ---------------------------------------------------------------------------
# Term loading
# ---------------------------------------------------------------------------

def _load_focused_terms() -> Dict[str, Dict[str, str]]:
    """
    Load native-language focused terms from focused_climate_terms.yaml
    and inject hardcoded English terms under the 'en' key.
    """
    raw = load_yaml("focused_climate_terms.yaml")
    terms: Dict[str, Dict[str, str]] = dict(raw.get("focused_terms", {}))
    terms["en"] = {"q1": _EN_Q1, "q2": _EN_Q2, "q3": _EN_Q3}
    return terms


_FOCUSED_TERMS: Optional[Dict[str, Dict[str, str]]] = None


def _get_focused_terms() -> Dict[str, Dict[str, str]]:
    global _FOCUSED_TERMS
    if _FOCUSED_TERMS is None:
        _FOCUSED_TERMS = _load_focused_terms()
    return _FOCUSED_TERMS


# ---------------------------------------------------------------------------
# Language helpers
# ---------------------------------------------------------------------------

def _lang_for_country(country: str | None) -> str:
    if not country:
        return "en"
    return _COUNTRY_TO_LANG.get(str(country).strip().lower(), "en")


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def _build_base(search_dict: dict, use_after_year: bool) -> str:
    """
    Build the location/filter base string from a row dict.
    City is quoted; MinYear becomes after:<year> when enabled; others are plain.
    """
    parts: List[str] = []
    for key, val in search_dict.items():
        if not val or str(val).strip() == "":
            continue
        if key == CITY_NAME_COLUMN:
            parts.append(f'"{val}"')
        elif key == SEARCH_FOR_AFTER_COLUMN:
            if use_after_year:
                try:
                    year = int(val)
                    if year > SEARCH_FOR_AFTER:
                        parts.append(f"after:{year}")
                except (ValueError, TypeError):
                    pass
            # always skip MinYear from base when filter is off
        else:
            parts.append(str(val))
    return " ".join(parts)


def _build_focused_queries(
    search_dict: dict,
    lang: str,
    file_type_filter: str,
) -> List[str]:
    """
    Return 3 fully-formed query strings for a city row.

    Native-language mode (lang != 'en'):
      Q1 → native slot 1  (plan/strategy names in local language)
      Q2 → native slot 2  (net-zero / neutrality targets in local language)
      Q3 → English Q1     (catches EU docs, translations, international summaries)

    English mode:
      Q1 → English Q1
      Q2 → English Q2
      Q3 → English Q3
    """
    terms = _get_focused_terms()
    en_terms = terms["en"]
    lang_terms = terms.get(lang, en_terms)

    if lang != "en":
        slots = [lang_terms["q1"], lang_terms["q2"], en_terms["q1"]]
    else:
        slots = [en_terms["q1"], en_terms["q2"], en_terms["q3"]]

    base = _build_base(search_dict, USE_AFTER_YEAR_FILTER)
    return [f"{base} {file_type_filter} {slot}".strip() for slot in slots]


# ---------------------------------------------------------------------------
# Single pre-built query executor (with caching, DDGS / ScaleSERP support)
# ---------------------------------------------------------------------------

def _call_search_api(
    query: str,
    api_key: str,
    google_domain: str,
    num_results: int,
) -> Dict[str, Any]:
    """
    Execute a single pre-built query string against ScaleSERP or DDGS.
    Handles result caching identically to perform_search in search.py.
    Returns a dict with an 'organic_results' key (empty dict on failure).
    """
    base_url = "https://api.scaleserp.com/search"

    try:
        with open(SEARCH_CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    if USE_DDGS:
        cache_key = str(["DDGS_focused", query, num_results])
        if USE_SEARCH_CACHE and cache_key in cache:
            logger.info("Cache hit (DDGS): %s", query[:80])
            return cache[cache_key]
        try:
            raw = DDGS().text(
                query,
                region="wt-wt",
                safesearch="off",
                timelimit="y",
                max_results=num_results,
                backend="auto",
            )
            resp: Dict[str, Any] = {"organic_results": [
                {"link": r.get("href"), "title": r.get("title"), "snippet": r.get("body")}
                for r in (raw or [])
            ]}
        except Exception as e:
            logger.error("DDGS request failed: %s", e)
            return {}
    else:
        params = {
            "api_key": api_key,
            "q": query,
            "google_domain": google_domain,
            "num": num_results,
        }
        cache_key = str(["scaleserp_focused", params])
        if USE_SEARCH_CACHE and cache_key in cache:
            logger.info("Cache hit (ScaleSERP): %s", query[:80])
            return cache[cache_key]
        try:
            r = requests.get(base_url, params=params, timeout=25)
            r.raise_for_status()
            resp = r.json()
        except Exception as e:
            logger.error("ScaleSERP request failed: %s", e)
            return {}

    cache[cache_key] = resp
    try:
        with open(SEARCH_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        logger.warning("Failed to save search cache: %s", e)

    return resp


# ---------------------------------------------------------------------------
# Round-robin merge
# ---------------------------------------------------------------------------

def _zip_longest_merge(
    *result_lists: List[str],
    max_results: int,
) -> List[str]:
    """
    Merge result lists round-robin, deduplicating by URL.

    Round 1: list0[0], list1[0], list2[0]
    Round 2: list0[1], list1[1], list2[1]
    ...

    Stops when max_results unique URLs are collected or all lists exhausted.
    """
    merged: List[str] = []
    seen: set = set()

    for round_items in itertools.zip_longest(*result_lists, fillvalue=None):
        for url in round_items:
            if url is None:
                continue
            if url not in seen:
                seen.add(url)
                merged.append(url)
                if len(merged) >= max_results:
                    return merged

    return merged


# ---------------------------------------------------------------------------
# Primary search entry point
# ---------------------------------------------------------------------------

def find_report_links_focused(
    city_name: str,
    api_key: str,
    search_dict: dict,
    city_country: str | None,
    max_pdf_results: int = MAX_PDF_RESULTS,
    max_page_results: int = MAX_PAGE_RESULTS,
    include_country_in_search: bool = True,
) -> Tuple[List[str], List[str]]:
    """
    Run 3 focused queries (PDF pass) and 3 focused queries (page pass),
    then merge results via round-robin into (pdf_links, page_links).
    """
    native_lang = _lang_for_country(city_country)
    google_domain = _GOOGLE_DOMAIN.get(native_lang, _GOOGLE_DOMAIN["en"])

    def _run_pass(file_type_filter: str, cap: int) -> List[str]:
        queries = _build_focused_queries(search_dict, native_lang, file_type_filter)
        per_query_results: List[List[str]] = []

        for idx, query in enumerate(queries, 1):
            logger.info("Focused query %d/%d: %s", idx, len(queries), query[:120])
            resp = _call_search_api(query, api_key, google_domain, RESULTS_PER_QUERY)
            links = [
                r.get("link")
                for r in resp.get("organic_results", [])
                if r.get("link")
            ]
            logger.info("Query %d returned %d results", idx, len(links))
            per_query_results.append(links)

        return _zip_longest_merge(*per_query_results, max_results=cap)

    pdf_links = _run_pass("filetype:pdf", max_pdf_results)
    pdf_links = _validate_pdf_links(pdf_links)

    page_links = _run_pass("-filetype:pdf", max_page_results)

    logger.info(
        new_functions.print_to_logger(
            f"find_report_links_focused: {len(pdf_links)} pdf, {len(page_links)} page links"
        )
    )
    return pdf_links[:max_pdf_results], page_links[:max_page_results]


# ---------------------------------------------------------------------------
# DataFrame-level entry point (drop-in replacement for process_companies_for_reports)
# ---------------------------------------------------------------------------

def process_companies_for_reports_focused(
    df: pd.DataFrame,
    api_key: Optional[str] = None,
    *,
    include_country_in_search: bool = True,
    show_progress: bool = True,
) -> pd.DataFrame:
    """
    For each row in df, run the focused 3-query search and attach
    pdf_link1..N and page_link1..N columns to the output DataFrame.

    Drop-in replacement for search.process_companies_for_reports.
    """
    if df is None or df.empty:
        logger.info("Empty DataFrame supplied to Stage 1; returning with link columns added.")
        out = df.copy() if df is not None else pd.DataFrame()
        return _ensure_link_columns(out)

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input DataFrame is missing required columns: {missing}. "
            f"Configure CITY_NAME_COLUMN/COUNTRY_NAME_COLUMN in config.py or adjust your input."
        )

    api_key = api_key or SCALESERP_API_KEY
    if not api_key:
        raise ValueError(
            "No ScaleSERP API key provided. "
            "Pass api_key= to process_companies_for_reports_focused or set SCALESERP_API_KEY."
        )

    out_df = df.copy()
    pdf_rows: List[list] = []
    page_rows: List[list] = []

    iterator = tqdm(
        out_df.itertuples(index=False),
        total=len(out_df),
        disable=not show_progress,
        desc="Stage 1 (focused): primary search",
    )

    for row in iterator:
        try:
            row_dict = row._asdict() if hasattr(row, "_asdict") else dict(zip(out_df.columns, row))
            row_dict = {
                k: v
                for k, v in row_dict.items()
                if not (v is None or (isinstance(v, float) and math.isnan(v)))
            }

            city_name = (row_dict.get(CITY_NAME_COLUMN) or "").strip()
            country_name = (row_dict.get(COUNTRY_NAME_COLUMN) or "").strip()

            if not city_name:
                logger.warning("Skipping row with missing city name.")
                pdf_rows.append([None] * MAX_PDF_RESULTS)
                page_rows.append([None] * MAX_PAGE_RESULTS)
                continue

            pdf_links, page_links = find_report_links_focused(
                city_name=city_name,
                api_key=api_key,
                city_country=country_name,
                max_pdf_results=MAX_PDF_RESULTS,
                max_page_results=MAX_PAGE_RESULTS,
                include_country_in_search=include_country_in_search,
                search_dict=row_dict,
            )

            logger.info(new_functions.print_to_logger(pdf_links, page_links))

            pdf_links = list(pdf_links)[:MAX_PDF_RESULTS] if pdf_links else []
            page_links = list(page_links)[:MAX_PAGE_RESULTS] if page_links else []

        except Exception:
            logger.exception(
                "Error processing city '%s' (%s)",
                city_name if "city_name" in locals() else "unknown",
                country_name if "country_name" in locals() else "unknown",
            )
            pdf_links, page_links = [], []

        pdf_rows.append(pdf_links + [None] * (MAX_PDF_RESULTS - len(pdf_links)))
        page_rows.append(page_links + [None] * (MAX_PAGE_RESULTS - len(page_links)))

    pdf_cols = [f"pdf_link{i}" for i in range(1, MAX_PDF_RESULTS + 1)]
    page_cols = [f"page_link{i}" for i in range(1, MAX_PAGE_RESULTS + 1)]
    out_df[pdf_cols] = pd.DataFrame(pdf_rows, index=out_df.index)
    out_df[page_cols] = pd.DataFrame(page_rows, index=out_df.index)

    logger.info(new_functions.print_to_logger("PDF_ROWS", pdf_rows))
    logger.info(new_functions.print_to_logger("PAGE_ROWS", page_rows))

    return out_df


def _ensure_link_columns(df: pd.DataFrame) -> pd.DataFrame:
    for c in [f"pdf_link{i}" for i in range(1, MAX_PDF_RESULTS + 1)]:
        if c not in df.columns:
            df[c] = None
    for c in [f"page_link{i}" for i in range(1, MAX_PAGE_RESULTS + 1)]:
        if c not in df.columns:
            df[c] = None
    return df
