# classify.py
"""
Classification helpers:
- is_relevant_pdf: call the LLM to classify relevance, city correctness, type, year
- pick_canonical_report_type_strict: strict phrase match against accepted types (no upgrades)
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Iterable, Optional

from .config import ACCEPTABLE_REPORT_TYPES, ACCEPTABLE_REPORT_TYPES_LOWER, OPENAI_MODEL

logger = logging.getLogger(__name__)

from openai import (
    APIStatusError,
    AuthenticationError,
    NotFoundError,
    OpenAI,
    PermissionDeniedError,
    RateLimitError,
)


def is_relevant_pdf(
    text: str,
    city_name: str,
    country: str,
    client,  # OpenAI client instance
    *,
    province: str = "a province",
    model: str = OPENAI_MODEL,
    max_chars: int = 6000,
) -> tuple[bool, str, str, str]:
    """
    Ask the LLM to classify a PDF excerpt.

    Returns:
        (final_relevance: bool, report_type: str, year: str, language: str)
        - final_relevance is True only if LLM says Relevance=Yes AND CorrectCity=Yes.
        - report_type is the raw type extracted from the LLM response (not canonicalized).
        - year is a 4-digit year or 'unknown year'.
        - language is the 2-letter ISO 639-1 code for the document's language.
    """
    if not text or len(text.strip()) < 50:
        logger.warning("is_relevant_pdf: empty or too-short text (len=%s)", len(text or ""))
        return False, "unknown_type", "unknown year", "unknown"

    excerpt = text[:max_chars]
    logger.debug(
        "is_relevant_pdf: model=%s, excerpt_len=%d, city=%s, country=%s",
        model,
        len(excerpt),
        city_name,
        country,
    )

    # prompt_old = f"""You are a strict report-classifier.

    # TASK
    # From the excerpt below, output five fields:

    # 1. ReportType – determine the original report type from the title page.
    #    • If not in English, translate to English
    #    • If title page is unclear, check hints in subsequent pages
    #    • Use “Annual” ONLY when it is the primary report type (e.g., “Annual Report”).
    #      If “annual” modifies another type (e.g., “Annual Sustainability Report”), classify by that other type.

    # 2. Year – the most recent 4-digit year describing the report period or title.
    #    • If multiple (e.g., 2023–2024) take the later year. If none, write “unknown”.

    # 3. Relevance – “Yes” only if ALL are true:
    #    • Contains measurable sustainability progress (historical KPIs, achieved results)
    #    • Is a full corporate report document (NOT an announcement, summary, press release, media article, or promotional brochure about a report)
    #      • If the document only announces that a report has been published
    #        (e.g., “City X publishes its 2024 Integrated Report”), classify as
    #        Relevance: No and ReportType: Press release.
    #    • Not a multi-city overview; focuses on the target city
    #    • Not purely financial-only

    # 4. CorrectCity – “Yes” only if the main subject is exactly '{city_name}' in '{country}'.

    # 5. Language – the primary language of the document as a 2-letter ISO 639-1 code (e.g., "en", "ja", "zh", "ru", "de", "fr").

    # OUTPUT FORMAT (one line, no extra words):
    # Relevance: <Yes/No>, CorrectCity: <Yes/No>, ReportType: <Type>, Year: <Year>, Language: <Language>

    # Document text:
    # {excerpt}
    # """

    prompt = f"""You are a strict report-classifier.

    TASK
    From the excerpt below, output five fields:

    1. ReportType – determine the original report type from the title page.
       • If not in English, translate to English
       • If title page is unclear, check hints in subsequent pages
       
    2. Year – the most recent 4-digit year describing the report period or title.
       • If multiple (e.g., 2023–2024) take the later year. If none, write “unknown”.

    3. Relevance – "Yes" only if ALL are true:
       • Contains information about climate, environmental or energy commitments, pledges, targets, actions.
       • Is a full document (NOT an announcement, summary, press release, media article, or promotional brochure about a report)
         • If the document only announces that a report has been published
           (e.g., "City X publishes its Climate Plan"), classify as
           Relevance: No and ReportType: Press release.
       • Not a multi-region overview; focuses on the target region.
       • Not purely financial-only.
       • Must include some information about climate mitigation, energy or climate adaptation actions or targets, not only about air pollution or water.

    4. CorrectCity – “Yes” only if the city is '{city_name}' in {province} of '{country}'.
       • Only classify as "Yes" if the main subject is exactly that city. If the document focuses on a different city, or multiple cities including the target city, classify as "No".
       • If the document focuses on private entities (e.g., companies, universities) rather than the city government, classify as "No".

    5. Language – the primary language of the document as a 2-letter ISO 639-1 code (e.g., "en", "ja", "zh", "ru", "de", "fr").

    OUTPUT FORMAT (one line, no extra words):
    Relevance: <Yes/No>, CorrectCity: <Yes/No>, ReportType: <Type>, Year: <Year>, Language: <Language>

    Document text:
    {excerpt}
    """

    try:
        resp = client.responses.create(
            model=model,
            input=(
                "Respond ONLY in this exact format on one line: "
                "Relevance: <Yes/No>, CorrectCity: <Yes/No>, ReportType: <Type>, Year: <Year>, Language: <xx>\n\n"
                + prompt
            ),
            reasoning={"effort": "minimal"},
            text={"verbosity": "low"},
            max_output_tokens=64,
        )
        raw = (getattr(resp, "output_text", None) or "").strip()
        logger.info("LLM classification raw: %s", raw)
    except Exception as e:
        logger.error("OpenAI classification call failed: %s", e, exc_info=True)
        return False, "openai_error", "unknown year", "unknown"

    if not raw:
        logger.warning("LLM returned empty output_text.")
        return False, "parse_error", "unknown year", "unknown"
    # raise "dadum"
    logger.debug("LLM raw line: %r", raw)

    s = raw.lower()

    relevance = bool(re.search(r"\brelevance\s*:\s*yes\b", s))
    correct_city = bool(re.search(r"\bcorrectcity\s*:\s*yes\b", s))

    m_type = re.search(r"reporttype:\s*(.+?)(?:,\s*year:|\s+year:|\n|$)", raw, flags=re.I | re.S)
    report_type = m_type.group(1).strip() if m_type else "unknown_type"
    report_type = report_type.strip(" '\"")

    m_year = re.search(r"\byear\s*:\s*([0-9]{4}|unknown)\b", s, flags=re.I)
    year_raw = m_year.group(1).strip().lower() if m_year else "unknown"
    year = (
        "unknown year"
        if year_raw == "unknown"
        else (
            str(int(year_raw))
            if year_raw.isdigit() and 1900 <= int(year_raw) <= 2100
            else "unknown year"
        )
    )

    m_lang = re.search(r"\blanguage\s*:\s*([a-z]{2})\b", s)
    lang = m_lang.group(1) if m_lang else "unknown"

    if not (m_type and m_year):
        logger.warning(
            "LLM parse issue. raw=%r | parsed_type=%r parsed_year=%r", raw, report_type, year
        )

    final_relevance = bool(relevance and correct_city)
    return final_relevance, report_type, year, lang


def pick_canonical_report_type_strict(ai_report_type: str) -> Optional[str]:
    """
    Strict: return the exact canonical name from ACCEPTABLE_REPORT_TYPES
    if the AI-detected label *contains that phrase* (case-insensitive), after normalization.
    No cross-category upgrades (e.g., CSR -> Sustainability) are performed.

    Returns the canonical value from ACCEPTABLE_REPORT_TYPES (original casing) or None.
    """

    def norm(s: str) -> str:
        s = unicodedata.normalize("NFKC", s or "").lower()
        s = re.sub(r"\b(19|20)\d{2}(?:\s*[-–—/]\s*(19|20)\d{2})?\b", "", s)
        s = s.replace("-", " ").replace("_", " ")
        s = re.sub(r"[^\w\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    ai_norm = norm(ai_report_type)
    if not ai_norm:
        return None

    accepted_map = {norm(a): a for a in ACCEPTABLE_REPORT_TYPES}

    for a_norm, canonical in accepted_map.items():
        if not a_norm:
            continue
        if re.search(rf"\b{re.escape(a_norm)}\b", ai_norm):
            return canonical

    return None
