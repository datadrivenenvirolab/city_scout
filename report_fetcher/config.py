# config.py
"""
Global configuration constants for the sustainability pipeline.
"""

from datetime import datetime
import os
from pathlib import Path

# === Paths ===
BASE_DIR = Path.cwd() / "outputs"

# basedir = basedir + datetime .now().strftime("%Y-%m-%d_%H-%M-%S")

# BASE_DIR = Path.cwd() / "outputs"

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

PDF_DIR = BASE_DIR / "pdfs"
HTML_DIR = BASE_DIR / "html"
TEMP_PDF_DIR = BASE_DIR / "tmp_pdfs"
CONFIRMED_PDF_DIR = PDF_DIR

for p in (BASE_DIR, PDF_DIR, HTML_DIR, TEMP_PDF_DIR):
    p.mkdir(parents=True, exist_ok=True)

# === API / Model Settings ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SCALESERP_API_KEY = os.getenv("SCALESERP_API_KEY", "")
OPENAI_MODEL = "gpt-5-mini"

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
    "Summary Document"
]

ACCEPTABLE_REPORT_TYPES_LOWER = [t.lower() for t in ACCEPTABLE_REPORT_TYPES]

# === Networking / Download Settings ===
PDF_DOWNLOAD_TIMEOUT_SECONDS = 30
PDF_DOWNLOAD_TIMEOUT_SECONDS = 5
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
MAX_PDF_RESULTS = 10
MAX_PAGE_RESULTS = 10
SEARCH_LANGUAGE_PRIORITY = "native"  # or "english"
STAGE1_SCORING_ENABLED = True

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
    SEARCH_FOR_AFTER_COLUMN
]
COLUMNS_IMP = [
    CITY_NAME_COLUMN,
    COUNTRY_NAME_COLUMN,
]

# === PDF pipeline / year gates ===
# Manually set the minimum acceptable report year here
MIN_ACCEPTABLE_REPORT_YEAR = 1900
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

# Year regex pattern (compiled in utils)
YEAR_REGEX = r"\b(19|20)\d{2}\b"

# === Stage 2 Page Processing Policy ===
# "process_all"         -> always process page links
# "process_if_no_pdfs"  -> process page links only if initial PDFs found none
# "skip"                -> never process page links
STAGE2_PAGE_PROCESSING_CONFIG = "process_if_no_pdfs"

# === Stage 2 Caps / Limits ===
TOP_K_PAGES_PER_DOMAIN = 3
MAX_PAGES_TOTAL = 8
TOP_K_PDFS_PER_DOMAIN = 10
MAX_PDFS_TOTAL = 30

# === Fallback Helper List ===
ENGLISH_SPEAKING_COUNTRIES = ["USA", "GBR", "CAN", "AUS", "NZL", "IRL"]

# Search Cache
SEARCH_CACHE_FILE = "search_cache.json"

# Use Duck Duck Go instead?
USE_DDGS = False
# USE_DDGS = True


# Enable or Disable Search Caching, No time limit for expiring cache yet. Mainly for testing PDF identification stuff to keep search results consistent. Could add a time limit for expiry in the future.
USE_SEARCH_CACHE = False
# USE_SEARCH_CACHE = True

MAX_QUERY_WORDS = 32 # Google search seems to have a 32 word limit from what I checked. So limiting queries to 32 Words. 