# search_focused.py
# Focused 3-query Stage 1 search strategy.
# Replaces the OR-chunked multi-term approach in search.py with three semantically
# distinct, short queries per city, merged via round-robin into the final link list.
#
# search.py is untouched. To revert, change the import in pipeline_stage1.py back to:
#   from .search import process_companies_for_reports

from __future__ import annotations

import ast
import itertools
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from ddgs import DDGS
from tqdm import tqdm

from .prompt_loader import load_yaml
from .search import _validate_pdf_links
from .search_vars import _COUNTRY_TO_LANG, _GOOGLE_DOMAIN
from .utils import progress_label
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
    SEARCH_LANGUAGE_PRIORITY,
    STAGE2_PAGE_PROCESSING_CONFIG,
    SUBREGION_NAME_COLUMN,
    REGION_NAME_COLUMN,
    USE_AFTER_YEAR_FILTER,
    USE_DDGS,
    USE_SEARCH_CACHE,
)

logger = logging.getLogger(__name__)


def _scaleserp_cache_key(query: str, google_domain: str, num_results: int) -> str:
    """Build a stable cache key that never contains credentials."""
    return str(["scaleserp_focused", query, google_domain, num_results])


def _redact_cache_value(value: Any, secret: str) -> Any:
    """Recursively remove a secret from strings before they reach disk."""
    if not secret:
        return value
    if isinstance(value, dict):
        return {
            str(key).replace(secret, "[REDACTED]"): _redact_cache_value(item, secret)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_cache_value(item, secret) for item in value]
    if isinstance(value, str):
        return value.replace(secret, "[REDACTED]")
    return value


def _compact_search_response(response: Any, secret: str) -> Dict[str, Any]:
    """Cache only fields consumed by the pipeline, excluding API metadata URLs."""
    organic = response.get("organic_results", []) if isinstance(response, dict) else []
    if not isinstance(organic, list):
        organic = []
    return {"organic_results": _redact_cache_value(organic, secret)}


def sanitize_search_cache_file(
    cache_file: str | Path = SEARCH_CACHE_FILE,
    *,
    api_key: str = SCALESERP_API_KEY,
) -> Tuple[Dict[str, Any], bool]:
    """Load and migrate a cache so neither its keys nor values contain secrets."""
    path = Path(cache_file)
    try:
        raw_cache = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}, False

    if not isinstance(raw_cache, dict):
        return {}, False

    sanitized: Dict[str, Any] = {}
    changed = False
    for raw_key, raw_response in raw_cache.items():
        safe_key = str(raw_key)
        known_scaleserp_entry = False
        try:
            parsed = ast.literal_eval(str(raw_key))
            if (
                isinstance(parsed, list)
                and len(parsed) == 2
                and parsed[0] == "scaleserp_focused"
                and isinstance(parsed[1], dict)
            ):
                params = parsed[1]
                safe_key = _scaleserp_cache_key(
                    str(params.get("q", "")),
                    str(params.get("google_domain", "google.com")),
                    int(params.get("num", RESULTS_PER_QUERY)),
                )
                known_scaleserp_entry = True
        except (SyntaxError, ValueError, TypeError):
            pass

        if api_key:
            safe_key = safe_key.replace(api_key, "[REDACTED]")
        safe_response = (
            _compact_search_response(raw_response, api_key)
            if known_scaleserp_entry
            else _redact_cache_value(raw_response, api_key)
        )
        sanitized[safe_key] = safe_response
        if safe_key != raw_key or safe_response != raw_response:
            changed = True

    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(sanitized, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_path.replace(path)

    return sanitized, changed

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
# Country / city name normalisation
# ---------------------------------------------------------------------------

_ISO3_TO_COUNTRY: Optional[Dict[str, str]] = None


def _get_iso3_to_country() -> Dict[str, str]:
    """
    ISO3 -> English country name, inverted from iso_country_codes.json
    (which is stored name -> code). Empty dict if the file is unreadable.
    """
    global _ISO3_TO_COUNTRY
    if _ISO3_TO_COUNTRY is None:
        mapping: Dict[str, str] = {}
        for base in (Path(__file__).resolve().parent.parent, Path.cwd()):
            path = base / "iso_country_codes.json"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    mapping = {str(v).upper(): str(k) for k, v in json.load(f).items()}
                break
            except Exception:
                continue
        if not mapping:
            logger.warning("iso_country_codes.json not found; ISO3 codes stay verbatim in queries.")
        _ISO3_TO_COUNTRY = mapping
    return _ISO3_TO_COUNTRY


# Codes whose ISO long form is a poor search token (or is absent from
# iso_country_codes.json). Everything else is taken from that file and
# truncated at the first comma ("Tanzania, United Republic of" -> "Tanzania").
_COUNTRY_NAME_OVERRIDES = {
    "KOR": "South Korea",
    "PRK": "North Korea",
    "IRN": "Iran",
    "TWN": "Taiwan",
    "COD": "Democratic Republic of the Congo",
    "COG": "Republic of the Congo",
    "SYR": "Syria",
    "LBY": "Libya",
    "MKD": "North Macedonia",
    "LAO": "Laos",
    "RUS": "Russia",
    "VNM": "Vietnam",
    "MDA": "Moldova",
    "SRB": "Serbia",
    "PSE": "Palestine",
}


def _country_display(country: Any) -> str:
    """
    Render the country for a search query.

    The upload sheet stores ISO3 codes, and pasting those into the query as a
    literal token wrecks recall: `"Cape Town" ZAF` returns zero capetown.gov.za
    documents while `"Cape Town" South Africa` returns them at ranks 1-2, and
    `GBR` actively pulls Great Barrier Reef papers into UK searches. Expand to
    the English country name; leave anything that is already a name alone.
    """
    text = str(country or "").strip()
    if not (len(text) == 3 and text.isalpha()):
        return text
    code = text.upper()
    if code in _COUNTRY_NAME_OVERRIDES:
        return _COUNTRY_NAME_OVERRIDES[code]
    name = _get_iso3_to_country().get(code)
    if not name:
        return text
    # Drop the formal suffix and any stray non-breaking space from the source file.
    return name.split(",")[0].replace("\xa0", " ").strip() or text


_ALIAS_RE = re.compile(r"^(?P<primary>[^(]+?)\s*\((?P<alias>[^)]+)\)\s*$")
# Parentheticals that qualify rather than rename ("Delhi (union territory)").
_ALIAS_STOPWORDS = ("including", "union", "district", "province", "state", "region", "city of")


def _split_city_alias(city: str) -> Tuple[str, Optional[str]]:
    """
    Split "Kraków (Cracow)" into ("Kraków", "Cracow").

    The sheet quotes 61 city names with a parenthetical exonym, and quoting the
    whole cell makes an exact phrase that essentially never appears in a local
    document: `"Kraków (Cracow)"` returned 3 irrelevant results where `"Kraków"`
    puts the official bip.krakow.pl plan at rank 1.

    Returns (primary_name, alias_or_None). The alias is dropped when the
    parenthetical is a qualifier rather than an alternative name.
    """
    text = str(city or "").strip()
    m = _ALIAS_RE.match(text)
    if not m:
        return text, None
    primary = m.group("primary").strip()
    alias = m.group("alias").strip()
    if not primary:
        return text, None
    if any(alias.lower().startswith(w) for w in _ALIAS_STOPWORDS):
        return primary, None
    return primary, alias


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------

def _build_base(search_dict: dict, use_after_year: bool, city_override: Optional[str] = None) -> str:
    """
    Build the location/filter base string from a row dict.
    City is quoted; Country is expanded from ISO3 to its English name and left
    unquoted; MinYear becomes after:<year> when enabled; others are plain.

    city_override replaces the row's City with a normalised form (alias split).
    """
    parts: List[str] = []
    for key, val in search_dict.items():
        if not val or str(val).strip() == "":
            continue
        if key == CITY_NAME_COLUMN:
            parts.append(f'"{city_override or val}"')
        elif key == COUNTRY_NAME_COLUMN:
            country = _country_display(val)
            if country:
                parts.append(country)
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

    When the city carries a parenthetical exonym ("Kraków (Cracow)"), the native
    slots use the local name and the English slot uses the exonym, since that is
    the form each corpus actually spells.
    """
    terms = _get_focused_terms()
    en_terms = terms["en"]
    lang_terms = terms.get(lang, en_terms)

    if lang != "en":
        slots = [lang_terms["q1"], lang_terms["q2"], en_terms["q1"]]
    else:
        slots = [en_terms["q1"], en_terms["q2"], en_terms["q3"]]

    primary, alias = _split_city_alias(str(search_dict.get(CITY_NAME_COLUMN, "")))
    base_primary = _build_base(search_dict, USE_AFTER_YEAR_FILTER, city_override=primary)
    base_english = (
        _build_base(search_dict, USE_AFTER_YEAR_FILTER, city_override=alias)
        if alias
        else base_primary
    )

    # Only the last slot is the English-language query in native mode; in
    # English mode every slot is, so the exonym applies throughout.
    bases = [base_primary, base_primary, base_english] if lang != "en" else [base_english] * 3
    return [
        f"{base} {file_type_filter} {slot}".strip() for base, slot in zip(bases, slots)
    ]


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

    cache, _ = sanitize_search_cache_file(SEARCH_CACHE_FILE, api_key=api_key)

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
        cache_key = _scaleserp_cache_key(query, google_domain, num_results)
        if USE_SEARCH_CACHE and cache_key in cache:
            logger.info("Cache hit (ScaleSERP): %s", query[:80])
            return cache[cache_key]
        try:
            r = requests.get(base_url, params=params, timeout=25)
            r.raise_for_status()
            resp = _compact_search_response(r.json(), api_key)
        except Exception as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status is not None:
                logger.error("ScaleSERP request failed (HTTP %s)", status)
            else:
                logger.error("ScaleSERP request failed (%s)", type(e).__name__)
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
    fallback: bool = False,
) -> Tuple[List[str], List[str]]:
    """
    Run 3 focused queries (PDF pass) and 3 focused queries (page pass),
    then merge results via round-robin into (pdf_links, page_links).

    When fallback=True, flip the language (native -> English, unless
    SEARCH_LANGUAGE_PRIORITY == "english"), which also switches the Google
    domain. Otherwise identical to the primary: the same 3-query system, just
    run on a different domain.
    """
    native_lang = _lang_for_country(city_country)
    if fallback:
        lang = native_lang if SEARCH_LANGUAGE_PRIORITY == "english" else "en"
    else:
        lang = native_lang
    google_domain = _GOOGLE_DOMAIN.get(lang, _GOOGLE_DOMAIN["en"])

    def _run_pass(file_type_filter: str, cap: int) -> List[str]:
        queries = _build_focused_queries(search_dict, lang, file_type_filter)
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

    # The page pass costs 3 SERP calls per entity and Stage 2 discards its
    # results entirely under the "skip" policy, so don't run it at all then.
    if STAGE2_PAGE_PROCESSING_CONFIG == "skip":
        logger.info("Page pass skipped (STAGE2_PAGE_PROCESSING_CONFIG='skip'): saved 3 queries.")
        page_links: List[str] = []
    else:
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
    progress_offset: int = 0,
    progress_total: Optional[int] = None,
    fallback: bool = False,
) -> pd.DataFrame:
    """
    For each row in df, run the focused search and attach pdf_link1..N and
    page_link1..N columns to the output DataFrame. When fallback=True, use the
    2-query English fallback strategy instead of the 3-query primary.

    Drop-in replacement for search.process_companies_for_reports.

    progress_offset/progress_total: scan-wide progress context from the
    service layer (entities done in earlier batches / queue size at scan
    start); log labels fall back to batch-local numbering when absent.
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
        desc=f"Stage 1 (focused): {'fallback' if fallback else 'primary'} search",
    )

    for row_index, row in enumerate(iterator, start=1):
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

            logger.info(
                "Stage 1 search %s: %s (%s)",
                progress_label(row_index, len(out_df), progress_offset, progress_total),
                city_name,
                country_name,
            )

            pdf_links, page_links = find_report_links_focused(
                city_name=city_name,
                api_key=api_key,
                city_country=country_name,
                max_pdf_results=MAX_PDF_RESULTS,
                max_page_results=MAX_PAGE_RESULTS,
                include_country_in_search=include_country_in_search,
                search_dict=row_dict,
                fallback=fallback,
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


def process_companies_for_fallback_reports_focused(
    df: pd.DataFrame,
    scaleserp_api_key: Optional[str] = None,
    *,
    include_country_in_search: bool = True,
    show_progress: bool = True,
    progress_offset: int = 0,
    progress_total: Optional[int] = None,
) -> pd.DataFrame:
    """
    Focused-structure drop-in replacement for
    search.process_companies_for_fallback_reports. Runs the 2-query English
    fallback (see find_report_links_focused_fallback) for each row.
    """
    return process_companies_for_reports_focused(
        df,
        api_key=scaleserp_api_key,
        include_country_in_search=include_country_in_search,
        show_progress=show_progress,
        progress_offset=progress_offset,
        progress_total=progress_total,
        fallback=True,
    )


def _ensure_link_columns(df: pd.DataFrame) -> pd.DataFrame:
    for c in [f"pdf_link{i}" for i in range(1, MAX_PDF_RESULTS + 1)]:
        if c not in df.columns:
            df[c] = None
    for c in [f"page_link{i}" for i in range(1, MAX_PAGE_RESULTS + 1)]:
        if c not in df.columns:
            df[c] = None
    return df
