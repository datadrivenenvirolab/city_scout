from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple, cast
from urllib.parse import urlparse

import pandas as pd
from tqdm import tqdm

from .config import OPENAI_API_KEY
from .config import SCALESERP_API_KEY
from .config import (
    CITY_NAME_COLUMN,
    COLUMNS,
    CONFIRMED_PDF_DIR,
    COUNTRY_NAME_COLUMN,
    ENGLISH_SPEAKING_COUNTRIES,
    FALLBACK_ENABLED,
    MAX_PAGE_RESULTS,
    MAX_PAGES_TOTAL,
    MAX_PDF_RESULTS,
    MAX_PDFS_TOTAL,
    MIN_ACCEPTABLE_REPORT_YEAR,
    REGION_NAME_COLUMN,
    STAGE2_PAGE_PROCESSING_CONFIG,
    SUBREGION_NAME_COLUMN,
    TEMP_PDF_DIR,
    TOP_K_PAGES_PER_DOMAIN,
    TOP_K_PDFS_PER_DOMAIN,
)
from .fetch import scrape_report_page_for_pdfs
from .pdf_utils import check_and_save_pdf
from .search_focused import (
    process_companies_for_fallback_reports_focused as process_companies_for_fallback_reports,
)
from .utils import is_blocked_doc_domain, progress_label, select_top_links

logger = logging.getLogger(__name__)


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _normalize_city(name: str) -> str:
    import re

    n = (name or "").lower()
    n = re.sub(r"[^a-z0-9]+", " ", n).strip()
    return n.replace(" ", "")


def _filter_official_links(links: List[str], city_name: str) -> List[str]:
    if not links:
        return []

    bad_domains = (
        "linkedin.com",
        "facebook.com",
        "twitter.com",
        "x.com",
        "youtube.com",
        "bloomberg.com",
        "reuters.com",
        "wsj.com",
        "medium.com",
        "github.com",
        "cloudfront.net",
        "google.com",
        "docs.google.com",
        "drive.google.com",
    )
    flat = _normalize_city(city_name)

    keep: List[str] = []
    for u in links:
        d = _domain(u)
        if not d or any(bad in d for bad in bad_domains):
            continue
        if flat and flat in d.replace("-", "").replace(".", ""):
            keep.append(u)
    return keep


class _RobustCompletionWrapper:
    def __init__(self, original_create):
        self.original_create = original_create

    def create(self, *args, **kwargs):
        res = self.original_create(*args, **kwargs)
        content = res.choices[0].message.content
        if content and not content.strip().startswith("{"):
            parsed = {}
            pairs = re.split(r",\s*(?=[A-Za-z_]+:)", content)
            for pair in pairs:
                if ":" in pair:
                    k, v = pair.split(":", 1)
                    parsed[k.strip()] = v.strip()

            mapping = {
                "Relevance": "relevance",
                "CorrectCity": "correct_city",
                "ReportType": "canonical_type",
                "LLMType": "llm_type",
                "Year": "final_year",
                "YearStart": "year_start",
                "PublicationDate": "publish_date",
                "Language": "language",
                "Reasoning": "ai_reasoning"
            }

            structured = {}
            for k, v in parsed.items():
                target_key = mapping.get(k, k.lower())
                structured[target_key] = v
                if target_key in ["canonical_type", "llm_type"]:
                    structured["ai_report_type"] = v
                if target_key == "final_year":
                    structured["ai_year_raw"] = v

            structured["llm_response"] = content
            res.choices[0].message.content = json.dumps(structured)
        return res


def stage2_main(
    df_with_links: pd.DataFrame,
    *,
    openai_api_key: Optional[str] = None,
    scaleserp_api_key: Optional[str] = None,
    include_fallback_for_non_english_when_primary_is_english: bool = True,
    show_progress: bool = True,
    progress_offset: int = 0,
    progress_total: Optional[int] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    try:
        from openai import OpenAI

        client = OpenAI(api_key=(openai_api_key or OPENAI_API_KEY or ""))
        client.chat.completions.create = _RobustCompletionWrapper(client.chat.completions.create).create
    except Exception as e:
        logger.error("OpenAI client initialization failed: %s", e, exc_info=True)
        empty_cols = COLUMNS + ["Filename", "Saved Folder", "Quarantined", "Canonical Type", "Language", "Year", "Year Start", "Year End", "Publication Date", "Year Source", "Metadata Created", "Metadata Modified", "Save Date"]
        return (
            pd.DataFrame(columns=empty_cols),
            pd.DataFrame([{"Reason": f"OpenAI configuration error: {e}"}]),
            pd.DataFrame(
                columns=COLUMNS
                + ["PDF_Link", "AI_Report_Type", "AI_Year_Raw", "Final_Year_Used", "Reason"]
            ),
        )

    logger.info("Stage 2: Initial pass — downloading and classifying reports.")
    results_df, failed_df, type_mismatch_df = run_scraper_from_df(
        df_with_links,
        client=client,
        show_progress=show_progress,
        progress_offset=progress_offset,
        progress_total=progress_total,
    )

    all_companies_df = df_with_links[COLUMNS].drop_duplicates()
    saved_companies = (
        set(results_df[CITY_NAME_COLUMN].dropna().unique()) if not results_df.empty else set()
    )
    companies_without_pdfs = all_companies_df[
        ~all_companies_df[CITY_NAME_COLUMN].isin(saved_companies)
    ]

    if not FALLBACK_ENABLED and not companies_without_pdfs.empty:
        logger.info(
            "Stage 2: Fallback disabled (CITYSCOUT_ENABLE_FALLBACK=off); "
            "skipping language fallback for %d companies.",
            len(companies_without_pdfs),
        )

    if FALLBACK_ENABLED and not companies_without_pdfs.empty:
        logger.info(
            "Stage 2: Fallback — no PDFs for %d companies; running language fallback search.",
            len(companies_without_pdfs),
        )

        fallback_list = companies_without_pdfs.copy()

        if include_fallback_for_non_english_when_primary_is_english and ENGLISH_SPEAKING_COUNTRIES:
            fallback_list = fallback_list[
                ~fallback_list[COUNTRY_NAME_COLUMN].isin(ENGLISH_SPEAKING_COUNTRIES)
            ]

        if not fallback_list.empty:
            fallback_df = df_with_links[
                df_with_links[CITY_NAME_COLUMN].isin(fallback_list[CITY_NAME_COLUMN])
            ].copy()

            fallback_api_key = scaleserp_api_key or SCALESERP_API_KEY
            if not fallback_api_key:
                logger.warning("No ScaleSERP API key for fallback; skipping fallback stage.")
            else:
                fallback_links_df = process_companies_for_fallback_reports(
                    fallback_df,
                    scaleserp_api_key=fallback_api_key,
                )

                logger.info("Stage 2: Fallback — downloading and classifying fallback links.")
                fb_results, fb_failed, fb_type_mismatch = run_scraper_from_df(
                    fallback_links_df,
                    client=client,
                    show_progress=show_progress,
                )

                if not fb_results.empty:
                    results_df = pd.concat([results_df, fb_results], ignore_index=True)
                if not fb_failed.empty:
                    failed_df = pd.concat([failed_df, fb_failed], ignore_index=True)
                if not fb_type_mismatch.empty:
                    type_mismatch_df = pd.concat(
                        [type_mismatch_df, fb_type_mismatch], ignore_index=True
                    )
        else:
            logger.info("Stage 2: Fallback — no eligible companies (filtered by country list).")

    logger.info("Stage 2: Finished.")
    return results_df, failed_df, type_mismatch_df


def run_scraper_from_df(
    dataframe: pd.DataFrame,
    *,
    client,
    show_progress: bool = True,
    progress_offset: int = 0,
    progress_total: Optional[int] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    target_cols = COLUMNS + ["Filename", "Saved Folder", "Quarantined", "Canonical Type", "Extracted Type", "Language", "Year", "Year Start", "Year End", "Publication Date", "Year Source", "Metadata Created", "Metadata Modified", "Save Date", "PDF URL", "Source Page URL", "AI_Reasoning", "LLM Response"]

    if dataframe is None or dataframe.empty:
        return (
            pd.DataFrame(columns=target_cols),
            pd.DataFrame(columns=["City", "Country", "Failed_Link", "Reason", "AI_Reasoning", "LLM Response"]),
            pd.DataFrame(
                columns=[
                    "City",
                    "Country",
                    "PDF_Link",
                    "AI_Report_Type",
                    "AI_Year_Raw",
                    "Final_Year_Used",
                    "Reason",
                    "AI_Reasoning",
                    "LLM Response",
                ]
            ),
        )

    results: List[Dict[str, Any]] = []
    visited_urls: set[str] = set()
    failed_download_rows: List[Dict[str, Any]] = []
    type_mismatch_rows: List[Dict[str, Any]] = []

    rows: List[Dict[str, Any]] = cast(List[Dict[str, Any]], dataframe.to_dict(orient="records"))

    iterator = tqdm(
        rows,
        total=len(rows),
        disable=not show_progress,
        desc="Stage 2: classify & download",
    )

    for row_index, row_dict in enumerate(iterator, start=1):
        try:
            city = (str(row_dict.get(CITY_NAME_COLUMN, "")) or "").strip()
            region = (str(row_dict.get(REGION_NAME_COLUMN, "")) or "").strip()
            country = (str(row_dict.get(COUNTRY_NAME_COLUMN, "")) or "").strip()
            subregion = (str(row_dict.get(SUBREGION_NAME_COLUMN, "")) or "").strip()

            logger.info(
                "Stage 2 classify %s: %s, %s, %s (%s)",
                progress_label(row_index, len(rows), progress_offset, progress_total),
                city,
                subregion,
                region,
                country,
            )

            pdf_links = [row_dict.get(f"pdf_link{i}") for i in range(1, MAX_PDF_RESULTS + 1)]
            page_links = [row_dict.get(f"page_link{i}") for i in range(1, MAX_PAGE_RESULTS + 1)]

            initial_pdf_links = [u for u in pdf_links if isinstance(u, str) and u]
            initial_page_links = [u for u in page_links if isinstance(u, str) and u]

            found_files: List[str] = []

            for link in initial_pdf_links:
                if link in visited_urls:
                    continue
                visited_urls.add(link)

                if is_blocked_doc_domain(link):
                    logger.info("Skipping blocked domain (academic/financial host): %s", link)
                    failed_download_rows.append(
                        {"City": city, "Country": country, "Failed_Link": link,
                         "Reason": "Blocked domain (academic/financial host)",
                         "AI_Reasoning": "", "LLM Response": ""}
                    )
                    continue

                status, saved_path, meta = check_and_save_pdf(
                    link,
                    city,
                    country,
                    client=client,
                    temp_dir=TEMP_PDF_DIR,
                    confirmed_dir=CONFIRMED_PDF_DIR,
                    min_year=MIN_ACCEPTABLE_REPORT_YEAR,
                )

                if status == "saved" and saved_path:
                    found_files.append(saved_path)
                    if meta.get("gate_verdict") == "reject":
                        # Shadow mode: saved anyway, but queued for review.
                        type_mismatch_rows.append(
                            {
                                "City": city,
                                "Country": country,
                                "PDF_Link": meta.get("url") or link,
                                "Kind": "issuer_gate_shadow",
                                "Reason": (
                                    f"[shadow] issuer gate would reject: "
                                    f"class={meta.get('gate_doc_class')}, issuer={meta.get('gate_issuer')}"
                                ),
                                "AI_Reasoning": meta.get("gate_reason") or "",
                                "LLM Response": meta.get("llm_response") or "",
                            }
                        )
                    results.append(
                        {
                            CITY_NAME_COLUMN: city,
                            REGION_NAME_COLUMN: region,
                            SUBREGION_NAME_COLUMN: subregion,
                            COUNTRY_NAME_COLUMN: country,
                            "Filename": os.path.basename(saved_path),
                            "Saved Folder": meta.get("saved_dir") or "",
                            "Quarantined": bool(meta.get("gate_quarantined")),
                            "Canonical Type": meta.get("canonical_type") or "",
                            "Extracted Type": meta.get("ai_report_type") or "",
                            "Language": (meta.get("language") or "unknown"),
                            "Year": meta.get("final_year") or "",
                            "Year Start": meta.get("year_start") or "",
                            "Year End": meta.get("year_end") or "",
                            "Publication Date": meta.get("publish_date") or "",
                            "Year Source": meta.get("year_source") or "",
                            "Metadata Created": meta.get("metadata_created_date") or "",
                            "Metadata Modified": meta.get("metadata_modified_date") or "",
                            "Save Date": meta.get("save_date") or _dt.date.today().isoformat(),
                            "PDF URL": meta.get("url") or "",
                            "Source Page URL": meta.get("source_page_url") or "",
                            "AI_Reasoning": meta.get("ai_reasoning") or "",
                            "LLM Response": meta.get("llm_response") or "",
                        }
                    )
                elif status == "download_failed":
                    dl_err = meta.get("download_error") or ""
                    failed_download_rows.append(
                        {
                            "City": city,
                            "Country": country,
                            "Failed_Link": link,
                            "Reason": (
                                f"Initial PDF download failed: {dl_err}"
                                if dl_err
                                else "Initial PDF download failed"
                            ),
                            "AI_Reasoning": meta.get("ai_reasoning") or "",
                            "LLM Response": meta.get("llm_response") or "",
                        }
                    )
                elif status == "type_mismatch":
                    type_mismatch_rows.append(
                        {
                            "City": city,
                            "Country": country,
                            "PDF_Link": link,
                            "Extracted Type": meta.get("ai_report_type"),
                            "LLM Type": meta.get("llm_type") or "",
                            "AI_Year_Raw": meta.get("ai_year_raw"),
                            "Final_Year_Used": meta.get("final_year"),
                            "Reason": "Relevant by AI, but type not in ACCEPTABLE_REPORT_TYPES",
                            "AI_Reasoning": meta.get("ai_reasoning") or "",
                            "LLM Response": meta.get("llm_response") or "",
                        }
                    )
                elif status == "issuer_rejected":
                    type_mismatch_rows.append(
                        {
                            "City": city,
                            "Country": country,
                            "PDF_Link": link,
                            "Kind": "issuer_rejected",
                            "Reason": (
                                f"Rejected by issuer gate: "
                                f"class={meta.get('gate_doc_class')}, issuer={meta.get('gate_issuer')}"
                            ),
                            "AI_Reasoning": meta.get("gate_reason") or "",
                            "LLM Response": meta.get("llm_response") or "",
                        }
                    )
                elif status == "irrelevant":
                    failed_download_rows.append(
                        {"City": city, "Country": country, "Failed_Link": link, "Reason": "Irrelevant by AI", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                    )
                elif status == "too_old":
                    failed_download_rows.append(
                        {"City": city, "Country": country, "Failed_Link": link, "Reason": "Report too old", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                    )
                elif status == "classification_failed":
                    failed_download_rows.append(
                        {"City": city, "Country": country, "Failed_Link": link, "Reason": "Classification failed", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                    )
                else:
                    failed_download_rows.append(
                        {"City": city, "Country": country, "Failed_Link": link, "Reason": f"Unknown status: {status}", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                    )

            should_process_pages = (STAGE2_PAGE_PROCESSING_CONFIG == "process_all") or (
                STAGE2_PAGE_PROCESSING_CONFIG == "process_if_no_pdfs" and not found_files
            )

            if should_process_pages and initial_page_links:
                official_candidates = _filter_official_links(initial_page_links, city)

                limited_pages = select_top_links(
                    official_candidates,
                    city=city,
                    top_k_per_domain=TOP_K_PAGES_PER_DOMAIN,
                    max_total=MAX_PAGES_TOTAL,
                    min_year=MIN_ACCEPTABLE_REPORT_YEAR,
                )
                official_pages: List[str] = [
                    item["url"] if isinstance(item, dict) else item for item in limited_pages
                ]

                scraped_pdf_links: List[str] = []
                # Remember which landing page each PDF link was scraped from so the
                # downloader can replay a browse-then-click chain and fall back to
                # re-scanning that page if the direct link fails.
                pdf_source_page: Dict[str, str] = {}
                for official_url in official_pages:
                    if official_url in visited_urls:
                        continue
                    scraped = scrape_report_page_for_pdfs(
                        official_url,
                        city_name_for_filename=city,
                        country=country,
                        visited=visited_urls,
                    )
                    for scraped_link in scraped:
                        pdf_source_page.setdefault(scraped_link, official_url)
                    scraped_pdf_links.extend(scraped)

                scraped_pdf_links = [u for u in scraped_pdf_links if u not in visited_urls]

                selected_candidates = select_top_links(
                    scraped_pdf_links,
                    city=city,
                    top_k_per_domain=TOP_K_PDFS_PER_DOMAIN,
                    max_total=MAX_PDFS_TOTAL,
                    min_year=MIN_ACCEPTABLE_REPORT_YEAR,
                )

                for item in selected_candidates:
                    pdf_url = item["url"] if isinstance(item, dict) else item
                    if pdf_url in visited_urls:
                        continue
                    visited_urls.add(pdf_url)

                    if is_blocked_doc_domain(pdf_url):
                        logger.info("Skipping blocked domain (academic/financial host): %s", pdf_url)
                        failed_download_rows.append(
                            {"City": city, "Country": country, "Failed_Link": pdf_url,
                             "Reason": "Blocked domain (academic/financial host)",
                             "AI_Reasoning": "", "LLM Response": ""}
                        )
                        continue

                    status, saved_path, meta = check_and_save_pdf(
                        pdf_url,
                        city,
                        country,
                        client=client,
                        temp_dir=TEMP_PDF_DIR,
                        confirmed_dir=CONFIRMED_PDF_DIR,
                        min_year=MIN_ACCEPTABLE_REPORT_YEAR,
                        page_url=pdf_source_page.get(pdf_url),
                    )

                    if status == "saved" and saved_path:
                        found_files.append(saved_path)
                        if meta.get("gate_verdict") == "reject":
                            # Shadow mode: saved anyway, but queued for review.
                            type_mismatch_rows.append(
                                {
                                    "City": city,
                                    "Country": country,
                                    "PDF_Link": meta.get("url") or pdf_url,
                                    "Kind": "issuer_gate_shadow",
                                    "Reason": (
                                        f"[shadow] issuer gate would reject: "
                                        f"class={meta.get('gate_doc_class')}, issuer={meta.get('gate_issuer')}"
                                    ),
                                    "AI_Reasoning": meta.get("gate_reason") or "",
                                    "LLM Response": meta.get("llm_response") or "",
                                }
                            )
                        results.append(
                            {
                                CITY_NAME_COLUMN: city,
                                COUNTRY_NAME_COLUMN: country,
                                "Filename": os.path.basename(saved_path),
                                "Saved Folder": meta.get("saved_dir") or "",
                                "Quarantined": bool(meta.get("gate_quarantined")),
                                "Canonical Type": meta.get("canonical_type") or "",
                                "Extracted Type": meta.get("ai_report_type") or "",
                                "Language": (meta.get("language") or "unknown"),
                                "Year": meta.get("final_year") or "",
                                "Year Start": meta.get("year_start") or "",
                                "Year End": meta.get("year_end") or "",
                                "Publication Date": meta.get("publish_date") or "",
                                "Year Source": meta.get("year_source") or "",
                                "Metadata Created": meta.get("metadata_created_date") or "",
                                "Metadata Modified": meta.get("metadata_modified_date") or "",
                                "Save Date": meta.get("save_date") or _dt.date.today().isoformat(),
                                "PDF URL": meta.get("url") or "",
                                "Source Page URL": meta.get("source_page_url") or "",
                                "AI_Reasoning": meta.get("ai_reasoning") or "",
                                "LLM Response": meta.get("llm_response") or "",
                            }
                        )
                    elif status == "download_failed":
                        dl_err = meta.get("download_error") or ""
                        failed_download_rows.append(
                            {
                                "City": city,
                                "Country": country,
                                "Failed_Link": pdf_url,
                                "Reason": (
                                    f"Scraped PDF download failed: {dl_err}"
                                    if dl_err
                                    else "Scraped PDF download failed"
                                ),
                                "AI_Reasoning": meta.get("ai_reasoning") or "",
                                "LLM Response": meta.get("llm_response") or "",
                            }
                        )
                    elif status == "type_mismatch":
                        type_mismatch_rows.append(
                            {
                                "City": city,
                                "Country": country,
                                "PDF_Link": pdf_url,
                                "Extracted Type": meta.get("ai_report_type"),
                                "LLM Type": meta.get("llm_type") or "",
                                "AI_Year_Raw": meta.get("ai_year_raw"),
                                "Final_Year_Used": meta.get("final_year"),
                                "Reason": "Relevant by AI, but type not in ACCEPTABLE_REPORT_TYPES",
                                "AI_Reasoning": meta.get("ai_reasoning") or "",
                                "LLM Response": meta.get("llm_response") or "",
                            }
                        )
                    elif status == "issuer_rejected":
                        type_mismatch_rows.append(
                            {
                                "City": city,
                                "Country": country,
                                "PDF_Link": pdf_url,
                                "Kind": "issuer_rejected",
                                "Reason": (
                                    f"Rejected by issuer gate: "
                                    f"class={meta.get('gate_doc_class')}, issuer={meta.get('gate_issuer')}"
                                ),
                                "AI_Reasoning": meta.get("gate_reason") or "",
                                "LLM Response": meta.get("llm_response") or "",
                            }
                        )
                    elif status == "irrelevant":
                        failed_download_rows.append(
                            {"City": city, "Country": country, "Failed_Link": pdf_url, "Reason": "Irrelevant by AI", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                        )
                    elif status == "too_old":
                        failed_download_rows.append(
                            {"City": city, "Country": country, "Failed_Link": pdf_url, "Reason": "Report too old", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                        )
                    elif status == "classification_failed":
                        failed_download_rows.append(
                            {"City": city, "Country": country, "Failed_Link": pdf_url, "Reason": "Classification failed", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                        )
                    else:
                        failed_download_rows.append(
                            {"City": city, "Country": country, "Failed_Link": pdf_url, "Reason": f"Unknown status: {status}", "AI_Reasoning": meta.get("ai_reasoning") or "", "LLM Response": meta.get("llm_response") or ""}
                        )
        except Exception as e:
            logger.error("Error processing city '%s' (%s): %s", city if 'city' in locals() else "unknown", country if 'country' in locals() else "unknown", e, exc_info=True)
            continue

    results_df = pd.DataFrame(
        results,
        columns=COLUMNS + ["Filename", "Saved Folder", "Quarantined", "Canonical Type", "Extracted Type", "Language", "Year", "Year Start", "Year End", "Publication Date", "Year Source", "Metadata Created", "Metadata Modified", "Save Date", "PDF URL", "Source Page URL", "AI_Reasoning", "LLM Response"],
    )
    failed_df = pd.DataFrame(failed_download_rows)
    mismatch_df = pd.DataFrame(type_mismatch_rows)
    return results_df, failed_df, mismatch_df
