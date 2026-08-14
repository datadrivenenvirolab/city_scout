# config.py
"""
Global configuration constants for the sustainability pipeline.
"""

from datetime import datetime
import os
from pathlib import Path

# === API / Model Settings ===
from .prompt_loader import get_llm_config, get_config_section

paths_cfg = get_config_section("paths")
# CITYSCOUT_OUTPUT_DIR pins the scratch/output root to a stable path (used when
# running as a long-lived service, e.g. on Railway); default keeps CLI behavior.
_OUTPUT_DIR_OVERRIDE = os.getenv("CITYSCOUT_OUTPUT_DIR", "")
if _OUTPUT_DIR_OVERRIDE:
    BASE_DIR = Path(_OUTPUT_DIR_OVERRIDE)
else:
    BASE_DIR = Path.cwd() / paths_cfg.get("base_output_dir", "outputs")

DATE_TIME = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

try:
    # {SLURM_JOB_NAME}_${SLURM_JOB_ID}
    SLURM_JOB_NAME = os.getenv("SLURM_JOB_NAME")
    SLURM_JOB_ID = os.getenv("SLURM_JOB_ID")
    if SLURM_JOB_NAME and SLURM_JOB_ID:
        BASE_DIR = BASE_DIR / f"{SLURM_JOB_NAME}_{SLURM_JOB_ID}_{DATE_TIME}"

except Exception as e:
    print(f"Error accessing SLURM environment variables: {e}")
    BASE_DIR = BASE_DIR / f"run_{DATE_TIME}"

PDF_DIR = BASE_DIR / paths_cfg.get("pdfs_subdir", "pdfs")
HTML_DIR = BASE_DIR / paths_cfg.get("html_subdir", "html")
TEMP_PDF_DIR = BASE_DIR / paths_cfg.get("tmp_pdfs_subdir", "tmp_pdfs")
CONFIRMED_PDF_DIR = PDF_DIR
# Quarantine for documents the issuer gate would have rejected while running in
# shadow mode. They are still kept for review, but out of pdfs/ so that folder
# only ever holds documents nothing objected to.
SHADOW_PDF_DIR = BASE_DIR / paths_cfg.get("shadow_pdfs_subdir", "pdfs_shadow_rejected")

for p in (BASE_DIR, PDF_DIR, HTML_DIR, TEMP_PDF_DIR, SHADOW_PDF_DIR):
    p.mkdir(parents=True, exist_ok=True)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SCALESERP_API_KEY = os.getenv("SCALESERP_API_KEY", "")
OPENAI_MODEL = get_llm_config().get("model", "gpt-5.4-mini")


def _env_flag(name: str, default: bool) -> bool:
    """Parse a boolean env var; unset/empty -> default. Falsey: 0/false/no/off."""
    v = os.getenv(name)
    if v is None or not v.strip():
        return default
    return v.strip().lower() not in ("0", "false", "no", "off")

# === Report Type Settings (canonical, ordered by priority) ===
# ACCEPTABLE_REPORT_TYPES = ["Annual", "Integrated", "CSR", "ESG", "Impact", "CDP"]
ACCEPTABLE_REPORT_TYPES = [
    "Climate Action Plan",
    "Climate Plan",
    "Climate Strategy",
    "Climate Change Strategy",
    "Environment Strategy",
    "Sustainability Strategy",
    "Resilience Strategy",
    "Resilient Strategy",
    "Climate Ready Plan",
    "Clean Energy Plan",
    "Net Zero Plan",
    "Net Zero Strategy",
    "Carbon Neutral Plan",
    "Carbon Neutral Strategy",
    "Climate Neutral Strategy",
    "Climate Action Roadmap",
    "Climate Mitigation Plan",
    "Mitigation Plan",
    "Climate Emergency Plan",
    "Climate Assessment",
    "Climate Vulnerability Assessment",
    "Climate Adaptation Plan",
    "Adaptation Strategy",
    "Energy Action Plan",
    "Sustainable Energy Action Plan",
    "SEAP",
    "CAAP",
    "Climate Action & Adaptation Plan",
    "Climate Change Mitigation Plan",
    # Other acceptable report types with climate content
    "Sustainability Report",
    "Environmental Report",
    "ESG Report",
    "Annual Climate Report",
    "GHG Inventory",
    "Emissions Inventory",
    "Energy Transition Strategy",
    "Low-Carbon Strategy",
    # Valid but only if excerpt indicates it's the true type
    "Policy Document",
    "Master Plan",
    "Roadmap",
    # Required fallbacks
    "Press Release",
    "Brochure",
    "Summary Document",
]

ACCEPTABLE_REPORT_TYPES_LOWER = [t.lower() for t in ACCEPTABLE_REPORT_TYPES]

# === Classification Cascade Settings ===
_classification_cfg = get_config_section("classification_settings")
USE_LLM_TYPE_MATCHING: bool = bool(_classification_cfg.get("use_llm_type_matching", False))

# Issuer verification gate: a focused second LLM check on documents about to
# be saved, catching false positives the relevance prompt misses (financial
# documents, third-party research about the place, other organizations' own
# plans). Modes:
#   off     — gate never runs
#   shadow  — gate runs and its would-be rejections are logged for review,
#             but every document is still saved (default)
#   enforce — gate rejections are not saved (they land in scan events)
# CITYSCOUT_ISSUER_GATE_MODE (env) overrides config.yaml.
_VALID_GATE_MODES = ("off", "shadow", "enforce")
ISSUER_GATE_MODE: str = (
    os.getenv("CITYSCOUT_ISSUER_GATE_MODE", "").strip().lower()
    or str(_classification_cfg.get("issuer_gate_mode", "shadow")).strip().lower()
)
if ISSUER_GATE_MODE not in _VALID_GATE_MODES:
    ISSUER_GATE_MODE = "enforce"

# Hosts whose documents are categorically not city/regional government
# reports: universities and academic publishers/repositories (campus plans,
# research papers about a place) and financial-document hosts. Checked
# against candidate URLs before download — saves the download and the LLM
# call. Academic TLD patterns (.edu, .edu.xx, .ac.xx) are matched separately
# in utils.is_blocked_doc_domain().
BLOCKED_DOC_DOMAINS = {
    # academic publishers / repositories
    "researchgate.net",
    "academia.edu",
    "jstor.org",
    "springer.com",
    "sciencedirect.com",
    "mdpi.com",
    "arxiv.org",
    "ssrn.com",
    "semanticscholar.org",
    "core.ac.uk",
    "publications.jrc.ec.europa.eu",
    # financial-document hosts
    "chihousai.or.jp",  # Japan Local Government Bond Association (IR decks)
}

# === Networking / Download Settings ===
net_cfg = get_config_section("networking")
PDF_DOWNLOAD_TIMEOUT_SECONDS = net_cfg.get("pdf_download_timeout", 30)
USE_DOWNLOAD_CACHE = net_cfg.get("use_download_cache", True)
ALLOW_NEW_DOWNLOADS = net_cfg.get("allow_new_downloads", True)
MAX_PDF_DOWNLOAD_BYTES = net_cfg.get("max_pdf_download_bytes", 50 * 1024 * 1024)  # 50 MB
FETCH_HEAD_TIMEOUT = net_cfg.get("fetch_head_timeout", 8.0)

# --- Download robustness (hardened downloader) ---
# Per-URL retry rounds (1 initial pass + retries) on transient failures
# (connection errors, HTTP 403/429/5xx). Timeouts are not retried within
# download_pdf — a host that hangs a full window rarely recovers in seconds.
PDF_DOWNLOAD_RETRY_ROUNDS = int(
    os.getenv("CITYSCOUT_PDF_RETRY_ROUNDS") or net_cfg.get("pdf_download_retry_rounds", 3)
)
# Waits (seconds) before retry rounds 2 and 3 (last value reused if more rounds).
PDF_RETRY_BACKOFF_SECONDS = tuple(net_cfg.get("pdf_retry_backoff_seconds", [5, 20]))
# Minimum delay (seconds) between requests to the same host, to avoid tripping
# CDN per-IP rate limits (which usually surface as a short 403 window).
FETCH_DOMAIN_MIN_DELAY = float(net_cfg.get("fetch_domain_min_delay", 3.5))
# Max per-host sessions kept alive (LRU) so CDN cookies (e.g. Akamai AKA_A2)
# persist across retries and across rows hitting the same domain.
MAX_DOMAIN_SESSIONS = int(net_cfg.get("max_domain_sessions", 200))
# curl_cffi TLS impersonation profile ("chrome" = latest supported). Requires
# the curl_cffi package; the downloader falls back to plain requests without it.
CURL_IMPERSONATE = str(net_cfg.get("curl_impersonate", "chrome"))
# Require a %PDF- signature in the first 1 KB (True) or also accept a
# PDF-claiming Content-Type when bytes lack the signature but aren't HTML (False).
STRICT_PDF_VALIDATION = bool(net_cfg.get("strict_pdf_validation", False))

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "mc_cid",
    "mc_eid",
}

# === Stage 1 Search Settings ===
search_cfg = get_config_section("search_settings")
MAX_PDF_RESULTS = search_cfg.get("max_pdf_results", 10)
MAX_PAGE_RESULTS = search_cfg.get("max_page_results", 10)
SEARCH_LANGUAGE_PRIORITY = search_cfg.get("search_language_priority", "native")
STAGE1_SCORING_ENABLED = search_cfg.get("stage1_scoring_enabled", False)

# Stage 2 language-flip fallback search. CITYSCOUT_ENABLE_FALLBACK (env)
# overrides search_settings.enable_fallback (config.yaml); default on.
FALLBACK_ENABLED: bool = _env_flag(
    "CITYSCOUT_ENABLE_FALLBACK", bool(search_cfg.get("enable_fallback", True))
)

CITY_NAME_COLUMN = "City"
# ASSOCIATION_NAME_COLUMN = "Association"
SUBREGION_NAME_COLUMN = "SubRegion"
REGION_NAME_COLUMN = "Region"
COUNTRY_NAME_COLUMN = "Country"
SEARCH_FOR_AFTER_COLUMN = "MinYear"

COLUMNS = [
    CITY_NAME_COLUMN,
    # ASSOCIATION_NAME_COLUMN,
    SUBREGION_NAME_COLUMN,
    REGION_NAME_COLUMN,
    COUNTRY_NAME_COLUMN,
    SEARCH_FOR_AFTER_COLUMN,
]
COLUMNS_IMP = [
    CITY_NAME_COLUMN,
    COUNTRY_NAME_COLUMN,
]

# === PDF pipeline / year gates ===
# Reports whose extracted year is older than this many years are rejected as
# too_old. CITYSCOUT_MAX_REPORT_AGE_YEARS (env) overrides
# classification_settings.max_report_age_years (config.yaml); default 3.
# Range-titled reports ("Plan 2015-2030") extract the LATER year by prompt
# design, so future-horizon plans pass regardless of when they were published.
MAX_REPORT_AGE_YEARS = int(
    os.getenv("CITYSCOUT_MAX_REPORT_AGE_YEARS")
    or _classification_cfg.get("max_report_age_years", 3)
)
# Computed at import time; any worker restart/deploy refreshes it after a
# calendar-year boundary.
MIN_ACCEPTABLE_REPORT_YEAR = datetime.now().year - MAX_REPORT_AGE_YEARS
# Search after particular year
SEARCH_FOR_AFTER = 1900

# === Link scoring globals ===
GOOD_KEYWORDS = {
    "report",
    "publication",
    "integrated",
    "annual",
    "sustainability",
    "esg",
    "csr",
    "impact",
    "climate",
    "nonfinancial",
    "non-financial",
}
BAD_KEYWORDS = {
    "policy",
    "careers",
    "job",
    "press",
    "newsroom",
    "blog",
    "media",
    "terms",
    "privacy",
    "cookies",
    "disclaimer",
}

# === Negative Keywords for Search ===
NEGATIVE_KEYWORDS = [
    # "job",
]

# Year regex pattern (compiled in utils)
YEAR_REGEX = r"\b(19|20)\d{2}\b"

# === Stage 2 Page Processing Policy ===
# "process_all"         -> always process page links
# "process_if_no_pdfs"  -> process page links only if initial PDFs found none
# "skip"                -> never process page links
STAGE2_PAGE_PROCESSING_CONFIG = "skip"

# === Stage 2 Caps / Limits ===
TOP_K_PAGES_PER_DOMAIN = 3
MAX_PAGES_TOTAL = 8
TOP_K_PDFS_PER_DOMAIN = 10
MAX_PDFS_TOTAL = 30

# === Fallback Helper List ===
ENGLISH_SPEAKING_COUNTRIES = ["USA", "GBR", "CAN", "AUS", "NZL", "IRL"]

# Search Cache
SEARCH_CACHE_FILE = search_cfg.get("search_cache_file", "search_cache.json")

# Use Duck Duck Go instead?
USE_DDGS = search_cfg.get("use_ddgs", False)

# Enable or Disable Search Caching
USE_SEARCH_CACHE = search_cfg.get("use_search_cache", True)

MAX_QUERY_WORDS = search_cfg.get("max_query_words", 32)
RESULTS_PER_QUERY = search_cfg.get("results_per_query", 5)
USE_AFTER_YEAR_FILTER = search_cfg.get("use_after_year_filter", False)
