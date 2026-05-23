# Changelog

## [Unreleased]

### report_fetcher/classify.py

- **LLM-guided canonical type cascade** — Added `pick_canonical_report_type_with_llm(llm_type, ai_report_type)`, a two-step resolver that first tries a strict match on the LLM's own canonical guess (`LLMType`), then falls back to the existing strict match on the raw extracted title. Documents with creative or non-standard names (e.g. "Green City Action Plan") that previously fell through as `type_mismatch` are now correctly mapped to their canonical type.
- **New `LLMType` response field** — `is_relevant_pdf` now returns a 7-tuple. The new 7th element is the `llm_type` string parsed from the model's `LLMType:` output field. All early-return error paths updated accordingly.
- **Updated `ReportType` regex** — The `ReportType` capture group now stops at `LLMType:` instead of `Year:`, preventing greedy consumption of the new field.

### report_fetcher/prompts.yaml

- **Added `LLMType` classification field** — The prompt now instructs the model to output a 7th field (`LLMType`) by choosing the best match from the canonical list, or `"none"` if nothing fits. The canonical list is injected dynamically via an `{acceptable_types}` placeholder at call time, keeping `ACCEPTABLE_REPORT_TYPES` in `config.py` as the single source of truth.
- Updated OUTPUT FORMAT line to include `LLMType` between `ReportType` and `Year`.
- **Relaxed `CorrectCity` rule to allow joint/collaborative reports** — The rule previously required the target city to be the sole subject, rejecting valid documents co-authored by 2–3 cities (e.g. a joint "Climate Neutrality Plan for Eindhoven and Helmond"). The rule now accepts documents where the target city is a co-equal primary subject, while still rejecting regional or national overviews where the city is one of many. Removed a duplicate bullet that repeated the private-entity exclusion verbatim.

### report_fetcher/config.py

- Added `USE_LLM_TYPE_MATCHING` (default `False` if key absent, `True` in `config.yaml`) — gates the LLM-guided cascade behavior. Set to `false` in `config.yaml` to restore legacy strict-only matching.
- Added `MAX_PDF_DOWNLOAD_BYTES` (default 50 MB) — controls the download size limit.
- Added `FETCH_HEAD_TIMEOUT` (default 8 s) — controls the timeout for HEAD pre-check requests.
- Added `RESULTS_PER_QUERY` (default 5) — per-query result cap used by the focused 3-query search in `search_focused.py`.
- Added `USE_AFTER_YEAR_FILTER` (default `False`) — when `True`, a `MinYear` value in the input row is emitted as `after:<year>` in the search query.
- Added `NEGATIVE_KEYWORDS` (default empty list) — terms appended as exclusions (`-keyword`) to every search query.
- Added `MAX_QUERY_WORDS` (default 32) — maximum word count per query for `build_queries_with_limit` in `search.py`.
- Added `SEARCH_FOR_AFTER_COLUMN` (`"MinYear"`) — name of the input DataFrame column that carries the earliest acceptable result year.

### report_fetcher/config.yaml

- Added `classification_settings.use_llm_type_matching: true`.

### report_fetcher/pdf_utils.py

- `check_and_save_pdf` updated to unpack the new 7-tuple from `is_relevant_pdf`, store `llm_type` in `meta`, and route to `pick_canonical_report_type_with_llm` or `pick_canonical_report_type_strict` based on `USE_LLM_TYPE_MATCHING`.
- **Removed upper bound on acceptable report year** — The year gate previously rejected documents whose LLM-extracted year exceeded the current calendar year, causing valid climate action plans with future target years (e.g. "Climate Neutrality 2030") to be incorrectly marked `too_old`. The upper bound is now removed; only the lower bound (`MIN_ACCEPTABLE_REPORT_YEAR`) applies.
- **PDF filename now uses AI-extracted report type** — The second segment of the saved filename was the canonical type (a fixed string from `ACCEPTABLE_REPORT_TYPES`); it is now the raw type extracted by the LLM (`ai_report_type`, shown as `Extracted Type` in the output CSV), which is more descriptive and matches the document's actual title. The extracted type is passed through the same character-sanitization regex already applied to the city name (`[^\w\-_. ]` stripped), preventing filesystem errors from LLM output that contains colons, slashes, or other unsafe characters.

### report_fetcher/pipeline_stage2.py

- **Revised output columns** — Results CSV now includes `Canonical Type` (validated match from `ACCEPTABLE_REPORT_TYPES`) and `Extracted Type` (raw document title as extracted by the LLM), replacing the previous `AI Report Type` column name. The intermediate `LLM Type` column has been removed from the results CSV; it is retained in the `type_mismatch` debug output where it aids diagnosis of unmatched documents.

### report_fetcher/search_focused.py (new file)

- **Focused 3-query Stage 1 strategy** — Replaces the OR-chunked multi-term approach in `search.py` for the primary Stage 1 search pass. Instead of one large OR query, each city now triggers three semantically distinct, short queries: Q1 targets plan/strategy names, Q2 targets net-zero/neutrality commitments, Q3 targets adaptation and mitigation plans. Results from all three queries are merged via round-robin deduplication so no single query monopolises the final link list.
- **Multilingual query slots** — For non-English cities, Q1 and Q2 use native-language terms loaded from `focused_climate_terms.yaml`; Q3 falls back to the English Q1 slot to catch EU documents, translations, and international summaries. For English cities all three English slots are used.
- **`_build_focused_queries()`** — Assembles the three full query strings from the city's search dict, the resolved language, and the `filetype:` filter. City name is quoted; `after:<year>` is appended when `USE_AFTER_YEAR_FILTER` is enabled.
- **`_call_search_api()`** — Executes a single pre-built query string against DDGS or ScaleSERP with cache read/write logic identical to `search.py`.
- **`_zip_longest_merge()`** — Round-robin merge: takes one result from each query list in turn, skipping duplicates, until `max_results` unique URLs are collected or all lists are exhausted.
- **`find_report_links_focused()`** — Runs a PDF pass (3 queries, `filetype:pdf`) and a page pass (3 queries, `-filetype:pdf`), validates PDF links, and returns `(pdf_links, page_links)`.
- **`process_companies_for_reports_focused()`** — Drop-in DataFrame-level replacement for `search.process_companies_for_reports`. Called by `pipeline_stage1.py`.

### report_fetcher/focused_climate_terms.yaml (new file)

- **Native-language focused query term groups** — Stores Q1/Q2/Q3 term strings for DE, FR, ES, RU, ZH, and JA. Loaded at runtime by `search_focused.py` via `prompt_loader.load_yaml`. English terms are hardcoded in `search_focused.py` and injected under the `en` key at load time, keeping `focused_climate_terms.yaml` free of English entries.

### report_fetcher/pipeline_stage1.py

- **Switched to focused search** — `stage1_main` now calls `process_companies_for_reports_focused` (from `search_focused.py`) instead of `process_companies_for_reports` (from `search.py`). To revert to the old OR-chunked behaviour, change the import back to `from .search import process_companies_for_reports`.

### report_fetcher/search.py

- **Hybrid PDF validation for Stage 1** — `pdf_links` returned by the `filetype:pdf` search are now validated before being stored. URLs ending in `.pdf` are accepted immediately at no cost. All others are probed with a `Range: bytes=0-3` GET request; only URLs whose first bytes match the `%PDF` magic number are kept. Non-PDF URLs are dropped and a DEBUG log entry is emitted. On any network error the URL is kept as a fail-safe (Google ranked it as a PDF result).
- **Multi-query splitting via `build_queries_with_limit()`** — Search term lists are distributed across multiple queries, each capped at `MAX_QUERY_WORDS` words (default 32). Multi-word terms are automatically quoted; an `OR` join is counted in the word budget. If the base query alone exceeds the limit it is returned as-is.
- **`perform_search()` now accepts a term list** — The function signature changed from a single `core_terms` string to `search_terms_list: List[str]`. It internally calls `build_queries_with_limit`, executes each sub-query, and returns a merged deduplicated result dict in the same format as before, so all callers are unaffected.
- **Negative keyword filtering** — `NEGATIVE_KEYWORDS` from `config.py` are appended to every query as `-keyword` (or `-"multi word"` for phrases), excluding unwanted result categories across all searches.
- **`after:<year>` filter** — When a `MinYear` key is present in the search dict, its value is emitted as `after:<year>` in the query base rather than as a plain term.

### report_fetcher/fetch.py

- **SSRF protection** — URLs pointing to private/internal addresses (localhost, LAN, cloud metadata) are now blocked before any request is made. PDF links discovered inside HTML pages are also validated, closing a bypass where a public page could redirect the downloader to an internal address.
- **Streaming download with size cap** — Files are now downloaded in 64 KB chunks instead of being loaded into memory all at once. Downloads exceeding 50 MB (`MAX_PDF_DOWNLOAD_BYTES`) are aborted mid-stream, preventing large or malicious files from exhausting process memory.
- **HEAD pre-check** — A lightweight HEAD request is sent before each download. If the server advertises a `Content-Length` above the limit, the download is refused immediately without transferring any body. Falls back silently to the streaming cap if HEAD is unsupported.
- **Graceful handling of oversized files** — `FileTooLargeError` is now caught as its own case, returning `False` with a clear log message instead of falling through to the generic unexpected-error handler.
- **Tightened HTML link extraction** — `_find_pdf_link_in_html` no longer follows any anchor whose visible text contains "Download". Only anchors with `.pdf` in the href are considered, avoiding false matches on app downloads, signup forms, and similar links.
- **Updated User-Agent strings** — Browser version strings updated from Chrome 126 / Firefox 127 (early 2024) to Chrome 136 / Firefox 137 / Safari 18 / Edge 136 (mid-2025) to avoid WAF filters that flag outdated browser identities.

### report_fetcher/search_vars.py

- **Added `_CORE_TERMS_LISTS`** — A dict mapping language codes to lists of individual search terms (mirroring the existing `_CORE_TERMS` string dict). Used by `perform_search` in `search.py` to drive `build_queries_with_limit`, replacing the old single-string OR join.
