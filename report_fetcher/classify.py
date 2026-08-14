# classify.py
"""
Classification helpers:
- is_relevant_pdf: call the LLM to classify relevance, city correctness, type, year
- pick_canonical_report_type_strict: strict phrase match against accepted types (no upgrades)
"""

from __future__ import annotations

import datetime as _dt
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
) -> tuple[bool, str, str, str, str, str, str, str, str]:
    """
    Ask the LLM to classify a PDF excerpt.

    Returns:
        (final_relevance, report_type, year, language, reasoning, raw_response, llm_type, year_start, publish_date)
        - final_relevance is True only if LLM says Relevance=Yes AND CorrectCity=Yes.
        - report_type is the raw type extracted from the LLM response (not canonicalized).
        - year is a 4-digit year or 'unknown year'. For ranges this is the LATER year (gating).
        - language is the 2-letter ISO 639-1 code for the document's language.
        - raw_response is the exact text returned by the LLM.
        - llm_type is the canonical type the LLM chose from ACCEPTABLE_REPORT_TYPES, or "" if none/parse failure.
        - year_start is the EARLIER 4-digit year when the report covers a range, else "".
        - publish_date is the explicit publication date stated in the document,
          normalized to "YYYY-MM-DD", "YYYY-MM", or "YYYY" (whatever precision the
          document gives); "" when unknown. Never derived from the period/title range.
    """
    if not text or len(text.strip()) < 50:
        logger.warning("is_relevant_pdf: empty or too-short text (len=%s)", len(text or ""))
        return False, "unknown_type", "unknown year", "unknown", "", "", "", "", ""

    excerpt = text[:max_chars]
    logger.debug(
        "is_relevant_pdf: model=%s, excerpt_len=%d, city=%s, country=%s",
        model,
        len(excerpt),
        city_name,
        country,
    )

    from .prompt_loader import get_prompt, get_llm_config

    prompt = get_prompt("classification_prompt")
    config = get_llm_config()

    try:
        resp = client.responses.create(
            model=model,
            input=(
                # SYNC: must match prompts.yaml OUTPUT FORMAT line
                "Respond ONLY in this exact format on one line: "
                "Relevance: <Yes/No>, CorrectCity: <Yes/No>, ReportType: <Type>, LLMType: <CanonicalType>, Year: <Year>, YearStart: <Year/none>, PublicationDate: <Date/unknown>, Language: <xx>, Reasoning: <Brief explanation>\n\n"
                + prompt.format(
                    excerpt=excerpt,
                    city_name=city_name,
                    province=province,
                    country=country,
                    acceptable_types=", ".join(ACCEPTABLE_REPORT_TYPES),
                )
            ),
            # GPT-5.4+ removed the "minimal" effort value; fall back to "medium".
            reasoning={"effort": config.get("reasoning_effort", "medium")},
            text={"verbosity": config.get("text_verbosity", "low")},
            max_output_tokens=config.get("max_output_tokens", 64),
        )
    except Exception as e:
        logger.error("OpenAI classification call failed: %s", e, exc_info=True)
        return False, "openai_error", "unknown year", "unknown", "", "", "", "", ""

    # Keep diagnostics outside the API exception boundary. Console/log encoding
    # failures must never turn a successful classification into an OpenAI error.
    raw = (getattr(resp, "output_text", None) or "").strip()
    logger.info("LLM classification raw: %s", raw)

    if not raw:
        logger.warning("LLM returned empty output_text.")
        return False, "parse_error", "unknown year", "unknown", "", "", "", "", ""
    # raise "dadum"
    logger.debug("LLM raw line: %r", raw)

    s = raw.lower()

    relevance = bool(re.search(r"\brelevance\s*:\s*yes\b", s))
    correct_city = bool(re.search(r"\bcorrectcity\s*:\s*yes\b", s))

    m_type = re.search(r"reporttype:\s*(.+?)(?:,\s*llmtype:|\s+llmtype:|\n|$)", raw, flags=re.I | re.S)
    report_type = m_type.group(1).strip() if m_type else "unknown_type"
    report_type = report_type.strip(" '\"")

    m_llm_type = re.search(r"llmtype:\s*(.+?)(?:,\s*year:|\s+year:|\n|$)", raw, flags=re.I | re.S)
    llm_type = m_llm_type.group(1).strip().strip(" '\"") if m_llm_type else ""

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

    # Earlier year of a range; "none" (or anything unparsable) -> "".
    # Must be a plausible year strictly before the (later) Year to be kept.
    m_year_start = re.search(r"\byearstart\s*:\s*([0-9]{4}|none)\b", s, flags=re.I)
    year_start_raw = m_year_start.group(1).strip().lower() if m_year_start else "none"
    year_start = ""
    if (
        year_start_raw.isdigit()
        and 1900 <= int(year_start_raw) <= 2100
        and year != "unknown year"
        and int(year_start_raw) < int(year)
    ):
        year_start = str(int(year_start_raw))

    # Explicit publication date stated in the document; "unknown" -> "".
    # Kept at whatever precision the model found: YYYY-MM-DD, YYYY-MM, or YYYY.
    # A future year is discarded — that is the signature of the period/title
    # range (e.g. "Plan 2015-2030") leaking in as a fake publication date.
    # The trailing (?![-\d]) rejects any hyphen/digit continuation, so a
    # malformed month ("2024-13") or a leaked range ("2015-2030") is dropped
    # whole rather than salvaged as a bare year.
    m_pub = re.search(
        r"\bpublicationdate\s*:\s*"
        r"((?:19|20)\d{2}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?)?|unknown)(?![-\d])",
        s,
        flags=re.I,
    )
    publish_date = ""
    if m_pub and m_pub.group(1) != "unknown":
        candidate = m_pub.group(1)
        if 1900 <= int(candidate[:4]) <= _dt.date.today().year:
            publish_date = candidate

    m_lang = re.search(r"\blanguage\s*:\s*([a-z]{2})\b", s)
    lang = m_lang.group(1) if m_lang else "unknown"

    m_reasoning = re.search(r"reasoning:\s*(.+)$", raw, flags=re.I | re.S)
    reasoning = m_reasoning.group(1).strip() if m_reasoning else ""

    if not (m_type and m_year):
        logger.warning(
            "LLM parse issue. raw=%r | parsed_type=%r parsed_year=%r", raw, report_type, year
        )

    final_relevance = bool(relevance and correct_city)
    return final_relevance, report_type, year, lang, reasoning, raw, llm_type, year_start, publish_date


_GATE_CLASSES = (
    "government_report",
    "financial_document",
    "research_publication",
    "other_org_document",
    "unknown",
)


def verify_issuer(
    text: str,
    city_name: str,
    country: str,
    client,
    *,
    model: str = OPENAI_MODEL,
    max_chars: int = 4000,
) -> dict:
    """
    Focused second-pass check on a document that already passed relevance:
    who issued it, and is it a document class we never want (financial
    document, third-party research, another organization's own plan)?

    Returns {"verdict": "pass"|"reject", "doc_class": str, "issuer": str,
    "reason": str, "raw": str}. Fails open: any call/parse problem returns a
    "pass" so the gate can never lose documents to infrastructure errors.
    """
    result = {"verdict": "pass", "doc_class": "unknown", "issuer": "", "reason": "", "raw": ""}
    if not text or len(text.strip()) < 50:
        return result

    from .prompt_loader import get_prompt, get_llm_config

    prompt = get_prompt("issuer_gate_prompt")
    config = get_llm_config()

    try:
        resp = client.responses.create(
            model=model,
            input=(
                # SYNC: must match prompts.yaml issuer_gate_prompt OUTPUT FORMAT line
                "Respond ONLY in this exact format on one line: "
                "Verdict: <Pass/Reject>, DocClass: <class>, Issuer: <organization name>, Reason: <brief explanation>\n\n"
                + prompt.format(
                    excerpt=text[:max_chars],
                    city_name=city_name,
                    country=country,
                )
            ),
            # "medium" (not the old sloppy "minimal", which GPT-5.4+ also
            # rejects): the gate's failure mode was verdict inconsistency,
            # and its call volume makes the extra reasoning tokens negligible.
            reasoning={"effort": config.get("gate_reasoning_effort", "medium")},
            text={"verbosity": "low"},
            max_output_tokens=config.get("max_output_tokens", 4096),
        )
        raw = (getattr(resp, "output_text", None) or "").strip()
    except Exception as e:
        logger.error("Issuer gate call failed (failing open): %s", e, exc_info=True)
        return result

    result["raw"] = raw
    if not raw:
        return result

    s = raw.lower()
    if re.search(r"\bverdict\s*:\s*reject\b", s):
        result["verdict"] = "reject"

    m_class = re.search(r"\bdocclass\s*:\s*([a-z_]+)\b", s)
    if m_class and m_class.group(1) in _GATE_CLASSES:
        result["doc_class"] = m_class.group(1)

    m_issuer = re.search(r"issuer:\s*(.+?)(?:,\s*reason:|\n|$)", raw, flags=re.I | re.S)
    if m_issuer:
        result["issuer"] = m_issuer.group(1).strip().strip(" '\"")

    m_reason = re.search(r"reason:\s*(.+)$", raw, flags=re.I | re.S)
    if m_reason:
        result["reason"] = m_reason.group(1).strip()

    # A reject without a recognized rejectable class is a malformed response;
    # fail open rather than trust it.
    if result["verdict"] == "reject" and result["doc_class"] in ("government_report", "unknown"):
        logger.warning("Issuer gate verdict/class mismatch (failing open): %r", raw)
        result["verdict"] = "pass"

    return result


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


def pick_canonical_report_type_with_llm(
    llm_type: str,
    ai_report_type: str,
) -> Optional[str]:
    """
    Two-step cascade for canonical type resolution:
      1. Try pick_canonical_report_type_strict on llm_type (LLM's own canonical guess).
      2. If that returns None, fall back to pick_canonical_report_type_strict on ai_report_type.

    Returns the canonical value from ACCEPTABLE_REPORT_TYPES or None.
    """
    if llm_type and llm_type.lower() not in ("none", ""):
        candidate = pick_canonical_report_type_strict(llm_type)
        if candidate is not None:
            logger.debug("LLM-guided match: %r -> %r", llm_type, candidate)
            return candidate
        logger.debug("LLMType %r did not strict-match; falling back to raw type.", llm_type)
    return pick_canonical_report_type_strict(ai_report_type)
