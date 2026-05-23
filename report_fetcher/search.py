# Whats new: Implementing search caching for some time to reduce api calls when testing.

# search.py
# from report_fetcher.config import SEARCH_FOR_AFTER
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import re
from pprint import pprint, pformat
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

from .search_vars import _CORE_TERMS, _COUNTRY_TO_LANG, _GOOGLE_DOMAIN, _CORE_TERMS_LISTS
from . import new_functions
import pandas as pd
import requests
from ddgs import DDGS
from tqdm import tqdm

from .config import (  # CITY_NAME_COLUMN,; COUNTRY_COLUMN,
    # ASSOCIATION_NAME_COLUMN,
    CITY_NAME_COLUMN,
    COLUMNS,
    COUNTRY_NAME_COLUMN,
    MAX_PAGE_RESULTS,
    MAX_PDF_RESULTS,
    REGION_NAME_COLUMN,
    SCALESERP_API_KEY,
    SEARCH_CACHE_FILE,
    SEARCH_LANGUAGE_PRIORITY,
    STAGE1_SCORING_ENABLED,
    SUBREGION_NAME_COLUMN,
    USE_DDGS,
    USE_SEARCH_CACHE,
    MAX_QUERY_WORDS,
    SEARCH_FOR_AFTER,
    SEARCH_FOR_AFTER_COLUMN,
    NEGATIVE_KEYWORDS,
)

# for column in COLUMNS:
#     # from .config __import__(column)

logger = logging.getLogger(__name__)
noisy_loggers = ["duckduckgo_search", "ddgs", "primp", "ddgs.ddgs"]
for noisy in noisy_loggers:
    logging.getLogger(noisy).disabled = True


def _lang_for_country(country: str | None) -> str:
    if not country:
        return "en"
    key = str(country).strip().lower()
    return _COUNTRY_TO_LANG.get(key, "en")


# --- core API call -----------------------------------------------------------


# def perform_search(
#     city_name: str | None,  # only used in logs
#     api_key: str,
#     google_domain: str,
#     core_terms: str,
#     city_country: str | None,  # only used in logs.
#     include_country_in_search: bool,
#     num_results: int,
#     file_type_filter: str | None,
#     search_dict: dict,
#     include_region_names: bool = False,
# ) -> Dict[str, Any]:
#     """
#     Call ScaleSERP and return JSON dict ({} on failure).
#     """
#     use_ddgs = USE_DDGS
#     use_cache = USE_SEARCH_CACHE
#     # use_ddgs = not use_ddgs
#     base_url = "https://api.scaleserp.com/search"
#     # query_parts = [city_name]
#     # if city_country and include_country_in_search:
#     #     query_parts.append(city_country)
#     # query_parts.append(core_terms)

#     query_parts = []
#     regions_dict = None

#     if include_region_names:
#         regions_dict = new_functions.get_region_types_by_country(search_dict["Country"])

#     # print("Regions Dict")
#     # print(search_dict["Country"], regions_dict)

#     # raise "LookHere"

#     if regions_dict:
#         search_dict_keys = search_dict.keys()
#         # for key in search_dict_keys:
#         for key in search_dict.keys():
#             region_name = regions_dict.get(key, None)
#             if region_name:
#                 search_dict[region_name] = search_dict.pop(key)

#     # print(search_dict)

#     # logger.info()
#     logger.info(new_functions.print_to_logger("Searching for ", search_dict))

#     # query_parts.append(new_functions.get_address(search_dict))

#     # logger.info("Recieved region info from Nominatim " + str(query_parts))
#     # print(query_parts)

#     # input()

#     for key in search_dict.keys():
#         val = search_dict.get(key)
#         # print(type(val), val)
#         if val and str(val).strip() != "":
#             if key in ["City"]:
#                 query_parts.append('"' + str(val) + '"')
#             else:
#                 query_parts.append(str(val))

#             # query_parts.append("AND")
#             if include_region_names:
#                 query_parts.append("'" + str(key) + "'")

#     # print(query_parts)
#     # input()

#     if file_type_filter:
#         query_parts.append(file_type_filter)

#     query_parts.append(core_terms)
#     # input()

#     query = " ".join(query_parts)

#     # logger.info("Query: " + query)
#     # if file_type_filter:
#     #     query += f" {file_type_filter}"

#     params = {
#         "api_key": api_key,
#         "q": query,
#         "google_domain": google_domain,
#         "num": num_results,
#     }
#     try:
#         with open(SEARCH_CACHE_FILE, "r+", encoding="utf-8") as search_cache_file_text:
#             search_cache = json.load(search_cache_file_text)
#     except:
#         search_cache = dict()

#     if use_ddgs:
#         try:
#             del params["api_key"]
#             del params["google_domain"]
#             request_string = str(["DDGS", params])
#             if use_cache:
#                 stored_search_json = search_cache.get(request_string, None)
#                 if stored_search_json:
#                     logger.info("Using cached results")
#                     return stored_search_json
#             # r = requests.get(base_url, params=params, timeout=25)
#             # r.raise_for_status()
#             try:
#                 # logger.propagate = False
#                 results = DDGS().text(
#                     query,
#                     region="wt-wt",
#                     safesearch="off",
#                     timelimit="y",
#                     max_results=num_results,
#                     backend="auto",
#                 )
#                 # logger.propagate = True
#                 resp_json = dict()
#                 resp_json["organic_results"] = []
#                 for result in results:
#                     resp_json["organic_results"].append(
#                         {
#                             "link": result.get("href"),
#                             "title": result.get("title"),
#                             "snippet": result.get("body"),
#                         }
#                     )

#                 # pprint(resp_json)
#                 # input()

#                 search_cache[request_string] = resp_json
#                 with open(SEARCH_CACHE_FILE, "w+", encoding="utf-8") as search_cache_file_text:
#                     json.dump(search_cache, search_cache_file_text, indent=2)
#                 return resp_json
#             except ValueError:
#                 logger.warning("DDG non-JSON response for %s: %s", city_name, str(result))
#                 return {}
#         except requests.RequestException as e:
#             logger.error("DDG request failed for %s (%s): %s", city_name, google_domain, e)
#             return {}
#     else:
#         try:
#             request_string = str([base_url, params, "google"])
#             if use_cache:
#                 stored_search_json = search_cache.get(request_string, None)
#                 if stored_search_json:
#                     logger.info("Using cached results")
#                     return stored_search_json
#             r = requests.get(base_url, params=params, timeout=25)
#             r.raise_for_status()
#             try:
#                 resp_json = r.json()
#                 search_cache[request_string] = resp_json
#                 with open(SEARCH_CACHE_FILE, "w+", encoding="utf-8") as search_cache_file_text:
#                     json.dump(search_cache, search_cache_file_text, indent=2)
#                 return r.json()
#             except ValueError:
#                 logger.warning("ScaleSERP non-JSON response for %s: %s", city_name, r.text[:300])
#                 return {}
#         except requests.RequestException as e:
#             logger.error("ScaleSERP request failed for %s (%s): %s", city_name, google_domain, e)
#             return {}


def count_words(text: str) -> int:
    """Count words in a string, treating quoted phrases as single units."""
    # Remove quotes and count resulting words
    return len(text.replace('"', "").split())


def build_queries_with_limit(
    base_parts: List[str],
    search_terms: List[str],
    max_words: int = 32,
    # min_search_year: int = SEARCH_FOR_AFTER
) -> List[str]:
    """
    Build multiple queries, each up to max_words in length.

    Args:
        base_parts: List of base query components (location, filters, etc.)
        search_terms: List of search keywords/phrases to distribute across queries
        max_words: Maximum word count per query (default: 32)

    Returns:
        List of query strings, each within word limit
    """
    queries = []
    base_query = " ".join(base_parts)
    # if min_search_year and min_search_year > 1900:
    #     base_query = base_parts + " after:" + str(min_search_year)
    base_word_count = count_words(base_query)

    if base_word_count >= max_words:
        # If base alone exceeds limit, return it as-is
        return [base_query]

    available_words = max_words - base_word_count
    current_terms = []
    current_word_count = 0

    for term in search_terms:
        # Wrap multi-word terms in quotes
        if " " in term and not (term.startswith('"') and term.endswith('"')):
            formatted_term = f'"{term}"'
        else:
            formatted_term = term

        term_word_count = count_words(formatted_term)

        # Check if adding this term (plus OR) would exceed limit
        # Account for " OR " = 1 word between terms
        additional_words = term_word_count
        if current_terms:  # Add 1 for "OR" if not first term
            additional_words += 1

        if current_word_count + additional_words <= available_words:
            current_terms.append(formatted_term)
            current_word_count += additional_words
        else:
            # Finalize current query
            if current_terms:
                terms_query = " OR ".join(current_terms)
                full_query = f"{base_query} {terms_query}".strip()
                queries.append(full_query)

            # Start new query with current term
            current_terms = [formatted_term]
            current_word_count = term_word_count

    # Add remaining terms
    if current_terms:
        terms_query = " OR ".join(current_terms)
        full_query = f"{base_query} {terms_query}".strip()
        queries.append(full_query)

    return queries if queries else [base_query]


# def perform_search(
#     city_name: str | None,
#     api_key: str,
#     google_domain: str,
#     search_terms_list: List[str],  # Changed from core_terms string
#     city_country: str | None,
#     include_country_in_search: bool,
#     num_results: int,
#     file_type_filter: str | None,
#     search_dict: dict,
#     include_region_names: bool = False,
#     use_ddgs: bool = True,
#     use_cache: bool = True,
#     max_words_per_query: int = 32,
#     search_cache_file: str = "search_cache.json"
# ) -> List[Dict[str, Any]]:
#     """
#     Perform multiple searches with search terms distributed across queries.

#     Returns:
#         List of result dictionaries, one per query executed
#     """
#     base_url = "https://api.scaleserp.com/search"

#     # Build base query parts (location, filters, etc.)
#     query_parts = []
#     regions_dict = None

#     if include_region_names:
#         regions_dict = new_functions.get_region_types_by_country(search_dict["Country"])

#     if regions_dict:
#         for key in list(search_dict.keys()):
#             region_name = regions_dict.get(key, None)
#             if region_name:
#                 search_dict[region_name] = search_dict.pop(key)

#     logger.info(new_functions.print_to_logger("Searching for ", search_dict))

#     # Build base query from search_dict
#     for key, val in search_dict.items():
#         if val and str(val).strip():
#             if key in ["City"]:
#                 query_parts.append(f'"{val}"')
#             else:
#                 query_parts.append(str(val))

#             if include_region_names:
#                 query_parts.append(f"'{key}'")

#     if file_type_filter:
#         query_parts.append(file_type_filter)

#     # Build multiple queries from search terms
#     queries = build_queries_with_limit(query_parts, search_terms_list, max_words_per_query)

#     logger.info(pformat(queries))
#     # exit()

#     logger.info(f"Built {len(queries)} queries from {len(search_terms_list)} search terms")

#     # Load cache
#     try:
#         with open(search_cache_file, "r", encoding="utf-8") as f:
#             search_cache = json.load(f)
#     except:
#         search_cache = {}

#     all_results = []

#     for idx, query in enumerate(queries):
#         logger.info(f"Executing query {idx + 1}/{len(queries)}: {query[:100]}...")

#         params = {
#             "api_key": api_key,
#             "q": query,
#             "google_domain": google_domain,
#             "num": num_results,
#         }

#         if use_ddgs:
#             try:
#                 del params["api_key"]
#                 del params["google_domain"]
#                 request_string = str(["DDGS", params])

#                 if use_cache and request_string in search_cache:
#                     logger.info("Using cached results")
#                     all_results.append(search_cache[request_string])
#                     continue

#                 results = DDGS().text(
#                     query,
#                     region="wt-wt",
#                     safesearch="off",
#                     timelimit="y",
#                     max_results=num_results,
#                     backend="auto",
#                 )

#                 resp_json = {"organic_results": []}
#                 for result in results:
#                     resp_json["organic_results"].append({
#                         "link": result.get("href"),
#                         "title": result.get("title"),
#                         "snippet": result.get("body"),
#                     })

#                 search_cache[request_string] = resp_json
#                 all_results.append(resp_json)

#             except Exception as e:
#                 logger.error(f"DDG request failed for query {idx + 1}: {e}")
#                 all_results.append({})
#         else:
#             try:
#                 request_string = str([base_url, params, "google"])

#                 if use_cache and request_string in search_cache:
#                     logger.info("Using cached results")
#                     all_results.append(search_cache[request_string])
#                     continue

#                 r = requests.get(base_url, params=params, timeout=25)
#                 r.raise_for_status()
#                 resp_json = r.json()

#                 search_cache[request_string] = resp_json
#                 all_results.append(resp_json)

#             except Exception as e:
#                 logger.error(f"ScaleSERP request failed for query {idx + 1}: {e}")
#                 all_results.append({})

#     # Save cache
#     try:
#         with open(search_cache_file, "w", encoding="utf-8") as f:
#             json.dump(search_cache, f, indent=2)
#     except Exception as e:
#         logger.error(f"Failed to save cache: {e}")

#     pprint(all_results)
#     exit()

#     return all_results


def perform_search(
    city_name: str | None,
    api_key: str,
    google_domain: str,
    search_terms_list: List[str],  # Changed from core_terms string
    city_country: str | None,
    include_country_in_search: bool,
    num_results: int,
    file_type_filter: str | None,
    search_dict: dict,
    include_region_names: bool = False,
    # use_ddgs: bool = True,
    # use_cache: bool = True,
    # max_words_per_query: int = 32,
    # search_cache_file: str = "search_cache.json"
    use_ddgs=USE_DDGS,
    use_cache=USE_SEARCH_CACHE,
    search_cache_file=SEARCH_CACHE_FILE,
    max_words_per_query=MAX_QUERY_WORDS,  # or use a global constant
) -> Dict[str, Any]:
    """
    Perform multiple searches with search terms distributed across queries.
    Returns merged results in the same format as original perform_search function.

    Returns:
        Dict with 'organic_results' key containing all results from all queries
    """
    base_url = "https://api.scaleserp.com/search"

    # Build base query parts (location, filters, etc.)
    query_parts = []
    regions_dict = None

    if include_region_names:
        regions_dict = new_functions.get_region_types_by_country(search_dict["Country"])

    if regions_dict:
        for key in list(search_dict.keys()):
            region_name = regions_dict.get(key, None)
            if region_name:
                search_dict[region_name] = search_dict.pop(key)

    logger.info(new_functions.print_to_logger("Searching for ", search_dict))

    # Build base query from search_dict
    for key, val in search_dict.items():
        if val and str(val).strip():
            if key in ["City"]:
                query_parts.append(f'"{val}"')
            elif key in ["MinYear"]:
                query_parts.append(f"after:{str(val)}")
            else:
                query_parts.append(str(val))

            if include_region_names:
                query_parts.append(f"'{key}'")

    if file_type_filter:
        query_parts.append(file_type_filter)

    for kw in NEGATIVE_KEYWORDS:
        if " " in kw:
            query_parts.append(f'-"{kw}"')
        else:
            query_parts.append(f"-{kw}")

    # Build multiple queries from search terms
    queries = build_queries_with_limit(query_parts, search_terms_list, max_words_per_query)

    logger.info(f"Built {len(queries)} queries from {len(search_terms_list)} search terms")

    # Load cache
    try:
        with open(search_cache_file, "r", encoding="utf-8") as f:
            search_cache = json.load(f)
    except:
        search_cache = {}

    # Merged results - same format as original function
    merged_results = {"organic_results": []}
    seen_links = set()  # Deduplicate results

    for idx, query in enumerate(queries):
        logger.info(f"Executing query {idx + 1}/{len(queries)}: {query[:100]}...")

        params = {
            "api_key": api_key,
            "q": query,
            "google_domain": google_domain,
            "num": num_results,
        }

        if use_ddgs:
            try:
                del params["api_key"]
                del params["google_domain"]
                request_string = str(["DDGS", params])

                if use_cache and request_string in search_cache:
                    logger.info("Using cached results")
                    resp_json = search_cache[request_string]
                else:
                    results = DDGS().text(
                        query,
                        region="wt-wt",
                        safesearch="off",
                        timelimit="y",
                        max_results=num_results,
                        backend="auto",
                    )

                    resp_json = {"organic_results": []}
                    for result in results:
                        resp_json["organic_results"].append(
                            {
                                "link": result.get("href"),
                                "title": result.get("title"),
                                "snippet": result.get("body"),
                            }
                        )

                    search_cache[request_string] = resp_json

                # Merge results, avoiding duplicates
                for result in resp_json.get("organic_results", []):
                    link = result.get("link")
                    if link and link not in seen_links:
                        seen_links.add(link)
                        merged_results["organic_results"].append(result)

            except Exception as e:
                logger.error(f"DDG request failed for query {idx + 1}: {e}")
        else:
            try:
                request_string = str([base_url, params, "google"])

                if use_cache and request_string in search_cache:
                    logger.info("Using cached results")
                    resp_json = search_cache[request_string]
                else:
                    r = requests.get(base_url, params=params, timeout=25)
                    r.raise_for_status()
                    resp_json = r.json()

                    search_cache[request_string] = resp_json

                # Merge results, avoiding duplicates
                for result in resp_json.get("organic_results", []):
                    link = result.get("link")
                    if link and link not in seen_links:
                        seen_links.add(link)
                        merged_results["organic_results"].append(result)

            except Exception as e:
                logger.error(f"ScaleSERP request failed for query {idx + 1}: {e}")

    # Save cache
    try:
        with open(search_cache_file, "w", encoding="utf-8") as f:
            json.dump(search_cache, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save cache: {e}")

    logger.info(f"Total unique results: {len(merged_results['organic_results'])}")

    # Return empty dict if no results, same as original function
    # pprint(merged_results)
    # exit()
    return merged_results if merged_results["organic_results"] else {}


# --- search entrypoints ------------------------------------------------------


def _check_url_is_pdf(url: str, sess: requests.Session, timeout: float = 5.0) -> bool:
    """
    Returns True if the URL points to an actual PDF.
    Fast path: URL ends in .pdf — no request needed.
    Slow path: Range GET for first 4 bytes, check %PDF magic number.
    On any error, returns True (fail-safe: keep in pdf_links).
    """
    if url.lower().split("?")[0].endswith(".pdf"):
        return True
    try:
        r = sess.get(url, headers={"Range": "bytes=0-3"}, timeout=timeout, stream=True)
        chunk = r.raw.read(4)
        r.close()
        return chunk == b"%PDF"
    except Exception:
        return True


def _validate_pdf_links(pdf_links: List[str]) -> List[str]:
    """Filters pdf_links down to URLs confirmed as actual PDFs."""
    sess = requests.Session()
    validated = []
    for url in pdf_links:
        if _check_url_is_pdf(url, sess):
            validated.append(url)
        else:
            logger.debug("Dropped non-PDF from pdf_links: %s", url)
    return validated


def find_report_links_categorized(
    city_name: str,
    api_key: str,
    search_dict: dict,
    city_country: str | None,
    max_pdf_results: int = MAX_PDF_RESULTS,
    max_page_results: int = MAX_PAGE_RESULTS,
    include_country_in_search: bool = True,
) -> Tuple[List[str], List[str]]:
    """
    Primary search pass for PDF and non-PDF links using language preference.
    Returns (pdf_links, page_links).
    """
    # Find an equivalent?
    native_lang = _lang_for_country(city_country)

    if SEARCH_LANGUAGE_PRIORITY == "english":
        primary_lang = "en"
    else:
        primary_lang = native_lang

    google_domain = _GOOGLE_DOMAIN[primary_lang]
    core_terms = _CORE_TERMS[primary_lang]
    core_terms_list = _CORE_TERMS_LISTS[primary_lang]

    # PDFs
    pdf_json = perform_search(
        city_name,
        api_key,
        google_domain,
        # core_terms,
        core_terms_list,
        city_country,
        include_country_in_search,
        max_pdf_results,
        "filetype:pdf",
        search_dict,
    )

    # logger.info(new_functions.print_to_logger("PDF JSON:" , pdf_json))
    # exit()

    if STAGE1_SCORING_ENABLED:
        pdf_links = score_results(pdf_json, city_name, city_country)
    else:
        pdf_links = [r.get("link") for r in pdf_json.get("organic_results", []) if r.get("link")]

    # logger.info(new_functions.print_to_logger("PDF LINKS", pdf_links))
    # exit()

    # Pages (non-PDF)
    page_json = perform_search(
        city_name,
        api_key,
        google_domain,
        # core_terms,
        core_terms_list,
        city_country,
        include_country_in_search,
        max_page_results,
        "-filetype:pdf",
        search_dict,
    )
    if STAGE1_SCORING_ENABLED:
        page_links = score_results(page_json, city_name, city_country)
    else:
        page_links = [r.get("link") for r in page_json.get("organic_results", []) if r.get("link")]

    pdf_links = _validate_pdf_links(pdf_links)

    # print("\n\n\nOPS")
    # logger.info(page_links)
    # logger.info(pdf_links)
    # # exit()
    return pdf_links[:max_pdf_results], page_links[:max_page_results]


def find_report_links_fallback(
    city_name: str,
    api_key: str,
    search_dict: dict,
    city_country: str | None,
    max_pdf_results: int = MAX_PDF_RESULTS,
    max_page_results: int = MAX_PAGE_RESULTS,
    include_country_in_search: bool = True,
) -> Tuple[List[str], List[str]]:
    """
    Fallback search: flip the language preference (if primary was EN, use native; else EN).
    """
    # Need to find an equivalent way to judge language by country, without knowing the column.
    native_lang = _lang_for_country(city_country)

    if SEARCH_LANGUAGE_PRIORITY == "english":
        fallback_lang = native_lang
    else:
        fallback_lang = "en"

    google_domain = _GOOGLE_DOMAIN[fallback_lang]
    core_terms = _CORE_TERMS[fallback_lang]
    core_terms_list = _CORE_TERMS_LISTS[fallback_lang]

    # PDFs
    pdf_json = perform_search(
        city_name,
        api_key,
        google_domain,
        # core_terms,
        core_terms_list,
        city_country,
        include_country_in_search,
        max_pdf_results,
        "filetype:pdf",
        search_dict,
    )
    pdf_links = (
        score_results(pdf_json, city_name, city_country)
        if STAGE1_SCORING_ENABLED
        else [r.get("link") for r in pdf_json.get("organic_results", []) if r.get("link")]
    )

    # Pages
    page_json = perform_search(
        city_name,
        api_key,
        google_domain,
        # core_terms,
        core_terms_list,
        city_country,
        include_country_in_search,
        max_page_results,
        "-filetype:pdf",
        search_dict,
    )

    logger.info(pformat(page_json))
    logger.info(len(page_json))
    # exit()
    page_links = (
        score_results(page_json, city_name, city_country)
        if STAGE1_SCORING_ENABLED
        else [r.get("link") for r in page_json.get("organic_results", []) if r.get("link")]
    )

    # print()

    return pdf_links[:max_pdf_results], page_links[:max_page_results]


def _ensure_link_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Make sure pdf_link* and page_link* columns exist (filled with None)."""
    pdf_cols = [f"pdf_link{i}" for i in range(1, MAX_PDF_RESULTS + 1)]
    page_cols = [f"page_link{i}" for i in range(1, MAX_PAGE_RESULTS + 1)]
    for c in pdf_cols + page_cols:
        if c not in df.columns:
            df[c] = None
    return df


def process_companies_for_reports(
    df: pd.DataFrame,
    api_key: Optional[str] = None,
    *,
    include_country_in_search: bool = True,
    show_progress: bool = True,
) -> pd.DataFrame:
    """
    For each row, run the primary search (language decided in search.py) and add
    columns pdf_link1..N and page_link1..N.

    - Uses CITY_NAME_COLUMN and COUNTRY_COLUMN from config.
    - Does not mutate the input DataFrame.
    """
    if df is None or df.empty:
        logger.info("Empty DataFrame supplied to Stage 1; returning with link columns added.")
        out = df.copy() if df is not None else pd.DataFrame()
        return _ensure_link_columns(out)

    # Validate required columns
    # missing = [c for c in (CITY_NAME_COLUMN, COUNTRY_COLUMN) if c not in df.columns]
    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Input DataFrame is missing required columns: {missing}. "
            f"Configure CITY_NAME_COLUMN/COUNTRY_COLUMN in config.py or adjust your input."
        )

    api_key = api_key or SCALESERP_API_KEY
    if not api_key:
        raise ValueError(
            "No ScaleSERP API key provided. "
            "Pass `api_key=` to process_companies_for_reports or set SCALESERP_API_KEY in config.py / env."
        )

    out_df = df.copy()
    pdf_rows: List[list] = []
    page_rows: List[list] = []

    iterator = tqdm(
        out_df.itertuples(index=False),
        total=len(out_df),
        disable=not show_progress,
        desc="Stage 1: primary search",
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
            region_name = (row_dict.get(REGION_NAME_COLUMN) or "").strip()
            subregion_name = (row_dict.get(SUBREGION_NAME_COLUMN) or "").strip()
            country_name = (row_dict.get(COUNTRY_NAME_COLUMN) or "").strip()

            if not city_name:
                logger.warning("Skipping row with missing city name.")
                pdf_rows.append([None] * MAX_PDF_RESULTS)
                page_rows.append([None] * MAX_PAGE_RESULTS)
                continue

            pdf_links, page_links = find_report_links_categorized(
                city_name=city_name,
                api_key=api_key,
                city_country=country_name,
                max_pdf_results=MAX_PDF_RESULTS,
                max_page_results=MAX_PAGE_RESULTS,
                include_country_in_search=include_country_in_search,
                search_dict=row_dict,
            )

            logger.info(new_functions.print_to_logger(pdf_links, page_links))

            # Defensive: ensure list type & cap
            pdf_links = list(pdf_links)[:MAX_PDF_RESULTS] if pdf_links else []
            page_links = list(page_links)[:MAX_PAGE_RESULTS] if page_links else []

        except Exception as e:
            logger.exception(
                "Error processing city '%s' (%s): %s",
                city_name if "city_name" in locals() else "unknown",
                country_name if "country_name" in locals() else "unknown",
                e,
            )
            pdf_links, page_links = [], []

        # Pad to fixed length
        pdf_rows.append(pdf_links + [None] * (MAX_PDF_RESULTS - len(pdf_links)))
        page_rows.append(page_links + [None] * (MAX_PAGE_RESULTS - len(page_links)))

    # assign columns
    pdf_cols = [f"pdf_link{i}" for i in range(1, MAX_PDF_RESULTS + 1)]
    page_cols = [f"page_link{i}" for i in range(1, MAX_PAGE_RESULTS + 1)]
    out_df[pdf_cols] = pd.DataFrame(pdf_rows, index=out_df.index)
    out_df[page_cols] = pd.DataFrame(page_rows, index=out_df.index)

    # pprint(out_df)

    logger.info(new_functions.print_to_logger("PDF_ROWS", pdf_rows))
    logger.info(new_functions.print_to_logger("PAGE_ROWS", page_rows))
    logger.info(new_functions.print_to_logger("OUT_DF", out_df))

    return out_df


def process_companies_for_fallback_reports(
    df: pd.DataFrame,
    scaleserp_api_key: str,
    # search_dict: dict,
    max_pdf_results: int = MAX_PDF_RESULTS,
    max_page_results: int = MAX_PAGE_RESULTS,
    include_country_in_search: bool = True,
) -> pd.DataFrame:
    """
    For each city in df, run the fallback search and attach columns
    pdf_link1..N, page_link1..N with the results.
    """

    if df is None or df.empty:
        out = df.copy()
        for i in range(1, max_pdf_results + 1):
            out[f"pdf_link{i}"] = None
        for i in range(1, max_page_results + 1):
            out[f"page_link{i}"] = None
        return out

    all_pdf_rows: List[List[str | None]] = []
    all_page_rows: List[List[str | None]] = []

    out_df = df.copy()

    with tqdm(total=out_df.shape[0], desc="Fallback: finding report links") as pbar:
        for _, row in out_df.iterrows():
            try:
                search_dict = (
                    row._asdict() if hasattr(row, "_asdict") else dict(zip(out_df.columns, row))
                )
                search_dict = {key: search_dict[key] for key in COLUMNS}
                # pprint(search_dict)
                # input()

                city_name = (row.get(CITY_NAME_COLUMN) or "").strip()
                city_country = (row.get(COUNTRY_NAME_COLUMN) or "").strip()

                pdf_links, page_links = find_report_links_fallback(
                    city_name=city_name,
                    api_key=scaleserp_api_key,
                    city_country=city_country,
                    max_pdf_results=max_pdf_results,
                    max_page_results=max_page_results,
                    include_country_in_search=include_country_in_search,
                    search_dict=search_dict,
                )
                pdf_links = list(pdf_links)[:max_pdf_results] if pdf_links else []
                page_links = list(page_links)[:max_page_results] if page_links else []
            except Exception as e:
                logger.warning(
                    "[Fallback] Error for '%s': %s",
                    city_name if "city_name" in locals() else "unknown",
                    e,
                )
                pdf_links, page_links = [], []

            # pad to fixed length
            all_pdf_rows.append(pdf_links + [None] * (max_pdf_results - len(pdf_links)))
            all_page_rows.append(page_links + [None] * (max_page_results - len(page_links)))

            pbar.update(1)

    pdf_cols = [f"pdf_link{i}" for i in range(1, max_pdf_results + 1)]
    page_cols = [f"page_link{i}" for i in range(1, max_page_results + 1)]

    out_df[pdf_cols] = pd.DataFrame(all_pdf_rows, index=out_df.index)
    out_df[page_cols] = pd.DataFrame(all_page_rows, index=out_df.index)
    return out_df


# --- scoring -----------------------------------------------------------------


def score_results(results: Dict[str, Any], city_name: str, city_country: str | None) -> List[str]:
    """
    Score ScaleSERP organic results and return link list sorted by score (desc).
    """
    links_scored: List[tuple[str, int]] = []
    current_year = _dt.datetime.now().year
    city_name_lower = city_name.lower()
    city_name_for_matching = re.sub(
        r"\b(?:inc|llc|ltd|gmbh|sa|nv|ag|pl)(\.?)\b", "", city_name_lower
    ).strip()

    if not isinstance(results, dict):
        return []

    for result in results.get("organic_results", []) or []:
        link = result.get("link")
        if not link:
            continue

        title = (result.get("title") or "").lower()
        snippet = (result.get("snippet") or "").lower()
        text_to_score = f"{(link or '').lower()} {title} {snippet}"
        total = 0
        domain = urlparse(link).netloc.replace("www.", "").lower()

        # strong city match
        if city_name_for_matching and city_name_for_matching in domain:
            total += 500
        elif city_name_lower and (city_name_lower in title or city_name_lower in snippet):
            total += 400

        # recency + keywords (only if strong match not already applied)
        if total < 400:
            cy = str(current_year)
            py = str(current_year - 1)
            if cy in text_to_score and any(
                t in text_to_score for t in ["sustainability", "esg", "corporate responsibility"]
            ):
                total += 100
            elif py in text_to_score and any(
                t in text_to_score for t in ["sustainability", "esg", "corporate responsibility"]
            ):
                total += 90
            elif cy in text_to_score and any(
                t in text_to_score for t in ["integrated", "annual report"]
            ):
                total += 80
            elif py in text_to_score and any(
                t in text_to_score for t in ["integrated", "annual report"]
            ):
                total += 70

            keywords = [
                "report",
                "publication",
                "impact",
                "climate",
                "bericht",
                "veröffentlichung",
                "auswirkung",
                "klima",
                "rapport",
                "publication",
                "impact",
                "climat",
                "informe",
                "publicación",
                "impacto",
                "clima",
                "отчет",
                "публикация",
                "влияние",
                "климат",
            ]
            total += sum(1 for kw in keywords if kw in text_to_score)

            # country signals
            if city_country:
                cc = str(city_country).lower()
                if cc in (link or "").lower():
                    total += 3
                if cc in title:
                    total += 2
                if cc in snippet:
                    total += 1

        links_scored.append((link, total))

    links_scored.sort(key=lambda x: x[1], reverse=True)
    return [link for link, _ in links_scored]
