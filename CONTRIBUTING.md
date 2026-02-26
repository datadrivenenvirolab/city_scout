# Contributing Guide

Welcome! Thanks for taking an interest in improving **report_fetcher** — a two‑stage pipeline that
1) searches the web for corporate sustainability/annual report links and  
2) downloads PDFs and classifies them with an LLM.

This guide explains how to set up your environment, the coding style we use, how to propose changes, and where in the codebase to make them.

---

## Table of contents
- [Code of Conduct](#code-of-conduct)
- [Project overview](#project-overview)
- [Getting started](#getting-started)
- [Running the pipeline](#running-the-pipeline)
- [How to contribute](#how-to-contribute)
- [Coding standards](#coding-standards)
- [Where to make changes](#where-to-make-changes)
- [Testing & verification](#testing--verification)
- [Security, privacy, and scraping etiquette](#security-privacy-and-scraping-etiquette)
- [Pull request checklist](#pull-request-checklist)
- [Release notes](#release-notes)
- [Authors](#authors)

---

## Code of Conduct
Be kind, respectful, and constructive. We follow the spirit of the [Contributor Covenant](https://www.contributor-covenant.org/).  
Harassment or disrespectful conduct is not tolerated.

---

## Project overview

**High‑level flow**

- **Stage 1** (`report_fetcher/pipeline_stage1.py`): Uses the ScaleSERP API to find PDF and non‑PDF links for each company (columns are configurable). Language handling, regional Google domains, and query terms live in `report_fetcher/search.py`.
- **Stage 2** (`report_fetcher/pipeline_stage2.py`): Tries each candidate link, downloads PDFs, extracts a text excerpt, calls an OpenAI model to classify relevance/report type/year/language, and saves accepted PDFs. If no PDFs are found for a company, it can run a **language fallback** search.
- **Utilities**:
  - `report_fetcher/fetch.py` – resilient PDF download + HTML scraping for embedded links
  - `report_fetcher/pdf_utils.py` – PDF text extraction (PyMuPDF) and the save/metadata flow
  - `report_fetcher/classify.py` – LLM prompt + parsing and canonical report type matching
  - `report_fetcher/utils.py` – link‑scoring and selection helpers
  - `report_fetcher/config.py` – all tunables (paths, API keys, limits, report types, etc.)
- **CLI entry point**: `run_pipeline.py` loads `.env` (if present), runs Stage 1 → Stage 2, and writes CSV artifacts under `outputs/`.

**Key outputs**
- `outputs/stage1_links.csv`: all candidate links per company
- `outputs/stage2_results.csv`: one row per **saved** PDF with canonical type, language, year
- `outputs/stage2_failed_downloads.csv`: download errors
- `outputs/stage2_type_mismatch.csv`: relevant by AI but not in `ACCEPTABLE_REPORT_TYPES`

---

## Getting started

### Prerequisites
- **Python** 3.10+ (3.11+ recommended)
- A virtual environment tool (`venv` or `conda`)
- API keys:
  - **SCALESERP_API_KEY** (for Stage 1 web search)
  - **OPENAI_API_KEY** (for Stage 2 classification)

### Setup
```bash
git clone <this-repo-url>
cd <repo-root>

# Create and activate a virtual environment
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# Install runtime + dev deps
pip install -U pip
pip install -r requirements.txt

# Provide secrets via environment (recommended) or a .env file
# Create a .env file in the repo root with:
#   OPENAI_API_KEY=sk-...
#   SCALESERP_API_KEY=...
```
> The CLI auto‑loads `.env` if present; otherwise pass keys as flags when running.

---

## Running the pipeline

With a CSV containing **Company** and **Country** columns (configurable in `config.py`):
```bash
python run_pipeline.py --input-csv companies.csv -vv
```
Or pass keys explicitly:
```bash
python run_pipeline.py --input-csv companies.csv \
  --scaleserp-api-key $SCALESERP_API_KEY \
  --openai-api-key $OPENAI_API_KEY \
  -vv
```
Artifacts will be written under `outputs/`.

---

## How to contribute

1. **Open an issue** describing the problem/enhancement. Propose a minimal plan of change.
2. **Create a feature branch** from `main`:
   ```bash
   git checkout -b feat/<short-slug>
   ```
3. **Implement & self‑review** (see standards below).
4. **Add/adjust tests** (or run the smoke checks below if formal tests are missing).
5. **Submit a PR** that references the issue and explains *what changed* and *why*.
6. Engage with review comments and iterate quickly.

**Commit messages**: Prefer Conventional Commits (e.g., `feat: add zh synonyms`, `fix: guard empty links`).

---

## Coding standards

- **Type hints everywhere** (the codebase uses `from __future__ import annotations`).
- **Logging** over `print` (respect verbosity flags; no secrets in logs).
- **Small, pure functions** where possible. Keep I/O at the edges.
- **Docstrings** for public functions; short comments for non‑obvious heuristics.
- **Config first**: place tunables in `config.py` (e.g., report types, limits, timeouts).
- **Avoid leaking secrets** in code or sample snippets. Read keys from env/`.env`.
- **Pandas**: avoid chained assignment; prefer explicit, side‑effect‑free transforms.
- **Network calls**: set timeouts, catch exceptions, and log succinctly.

**Optional tooling (nice to have)**
- Formatting: `black`
- Linting: `ruff`
- Pre‑commit: provide a minimal `.pre-commit-config.yaml` in a future PR

---

## Where to make changes

### Add/modify **report types**
- Edit `ACCEPTABLE_REPORT_TYPES` (and order) in `report_fetcher/config.py`.
- The LLM output is *strictly* mapped via `pick_canonical_report_type_strict()` — add synonyms there *only* if they still contain the exact canonical phrase.

### Improve **search** behavior
- Language terms and Google domain routing: `report_fetcher/search.py`.
- To support a new country → language mapping, extend `_COUNTRY_TO_LANG`.
- Use `STAGE1_SCORING_ENABLED` to toggle result re‑scoring logic if added.

### Tune **link scoring / selection**
- Update heuristics in `report_fetcher/utils.py` (`score_link`, `select_top_links`).
- Keep domain caps (`top_k_per_domain`) and `max_total` conservative.

### Harden **downloader/scraper**
- See `report_fetcher/fetch.py`. Maintain timeouts, user‑agent rotation, and the HTML viewer → PDF extraction flow. Be mindful of rate limits and polite delays.

### Adjust **Stage 2** behavior
- Gate years via `MIN_ACCEPTABLE_REPORT_YEAR` (in `config.py`).
- Control page‑scrape policy via `STAGE2_PAGE_PROCESSING_CONFIG`: `"process_all"`, `"process_if_no_pdfs"`, or `"skip"`.
- Fallback search toggle/filtering is in `pipeline_stage2.stage2_main()`.

---

## Testing & verification

The project currently relies on **smoke tests** plus targeted unit tests you add.

**Smoke checks**
1. Create a small `companies.csv` with 2–3 companies and countries.
2. Run `python run_pipeline.py --input-csv companies.csv -v`.
3. Verify that:
   - `outputs/stage1_links.csv` contains candidate links.
   - `outputs/stage2_results.csv` has rows only for accepted PDFs.
   - Failures and type mismatches are recorded in their respective CSVs.

**Suggested unit tests (PRs welcome)**
- `utils.score_link` and `select_top_links` on synthetic URLs.
- `classify.pick_canonical_report_type_strict` normalization edge cases.
- `pdf_utils.extract_text_from_pdf` on small fixtures.
- `fetch._find_pdf_link_in_html` with HTML snippets.

**Local test harness**
- Prefer `pytest` and `tox` (PRs to introduce these are appreciated).

---

## Security, privacy, and scraping etiquette

- **Respect website terms** and robots where applicable. This tool targets public corporate reports; do **not** add code to bypass authentication, paywalls, or technical measures.
- Keep crawl intensity low; obey **timeouts** and consider polite random sleep if adding broader crawling.
- **Do not log or commit** API keys, auth cookies, or personally identifying information.
- Only save PDFs that pass the *relevance* and *company* checks; discard temporary files on failure.

---

## Pull request checklist

- [ ] Code is typed, documented, and follows logging conventions.
- [ ] No secrets in code, tests, or logs.
- [ ] Configurable values live in `config.py`.
- [ ] Added/updated tests or ran smoke checks.
- [ ] Ran `black`/`ruff` locally (if available).
- [ ] Updated README if behavior/flags/outputs changed.
- [ ] PR description clearly explains the change and its impact.

---

## Release notes

- Keep a short **CHANGELOG** in your PR description if user‑visible behavior changes (flags, outputs, filenames, CSV schemas, or minimum Python version).

---

## Authors

- Developed by **Markas Nausėda**, 2025  
- Copyright ownership: **Arboretica**

