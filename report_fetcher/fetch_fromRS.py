# fetch.py
from __future__ import annotations

import ipaddress
import logging
import os
import random
import re
import socket
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, urlencode, parse_qsl

import requests

logger = logging.getLogger(__name__)

# Import config settings
try:
    from .config import (
        ENABLE_FETCH_BACKOFF,
        FETCH_BACKOFF_BASE,
        FETCH_BACKOFF_MAX,
        ENABLE_FETCH_WARMUP,
        FETCH_TIMEOUT,
        FETCH_MAX_RETRIES,
        STRICT_PDF_VALIDATION,
        TRACKING_PARAMS,
        REUSE_FETCH_SESSIONS,
        REUSE_SESSIONS_PER_DOMAIN,
        FETCH_DOMAIN_MIN_DELAY,
        FETCH_403_RETRY_DELAY_MIN,
        FETCH_403_RETRY_DELAY_MAX,
        MAX_DOMAIN_SESSIONS,
    )
except ImportError:
    # Fallback values if config import fails
    ENABLE_FETCH_BACKOFF = False
    FETCH_BACKOFF_BASE = 0.6
    FETCH_BACKOFF_MAX = 5.0
    ENABLE_FETCH_WARMUP = True
    FETCH_TIMEOUT = 60.0
    FETCH_MAX_RETRIES = 3
    STRICT_PDF_VALIDATION = False
    TRACKING_PARAMS = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "mc_cid", "mc_eid"
    }
    REUSE_FETCH_SESSIONS = False
    REUSE_SESSIONS_PER_DOMAIN = True
    FETCH_DOMAIN_MIN_DELAY = 3.5
    FETCH_403_RETRY_DELAY_MIN = 2.0
    FETCH_403_RETRY_DELAY_MAX = 4.0
    MAX_DOMAIN_SESSIONS = 500

# User-Agent pool (browser-like)
USER_AGENTS = [
    # Chrome (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Edge (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0",
    # Firefox (Windows)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    # Safari (Mac)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.3 Safari/605.1.15",
]

# If you want to force a custom UA via env:
#   REPORTRADAR_UA="..."
ENV_UA = os.getenv("REPORTRADAR_UA", "").strip()

# Use environment overrides if set, otherwise use config
DEFAULT_TIMEOUT = float(os.getenv("FETCH_TIMEOUT", FETCH_TIMEOUT))
DEFAULT_MAX_RETRIES = int(os.getenv("FETCH_MAX_RETRIES", FETCH_MAX_RETRIES))
DEFAULT_BACKOFF_BASE = float(os.getenv("FETCH_BACKOFF_BASE", FETCH_BACKOFF_BASE))

# Per-domain rate limiting: records time.monotonic() of the last request sent to each host.
# Checked in fetch_bytes() to enforce FETCH_DOMAIN_MIN_DELAY between same-domain requests.
_domain_last_request: dict = {}   # netloc -> float

# Per-domain session cache: one requests.Session per host, kept for the process lifetime so
# CDN session cookies (e.g. Akamai AKA_A2) issued on first visit are reused on retries.
# Uses an OrderedDict as an LRU cache capped at MAX_DOMAIN_SESSIONS to prevent unbounded growth.
_domain_sessions: OrderedDict = OrderedDict()  # netloc -> requests.Session


@dataclass
class FetchResult:
    final_url: str
    content_type: str
    status_code: int
    bytes_len: int
    was_pdf: bool


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------
def _pick_user_agent() -> str:
    return ENV_UA or random.choice(USER_AGENTS)


def _build_headers(
    *,
    referer: Optional[str] = None,
    user_agent: Optional[str] = None,
    accept: Optional[str] = None,
) -> dict:
    ua = user_agent or _pick_user_agent()
    headers = {
        "User-Agent": ua,
        # A more browser-ish Accept helps with some CDNs/WAFs.
        "Accept": accept
        or "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        # Sec-Fetch headers that real Chrome always sends — WAFs check these
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Site": "cross-site" if referer else "none",
        # Client hints
        "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if referer:
        headers["Referer"] = referer
        try:
            headers["Origin"] = f"{urlparse(referer).scheme}://{urlparse(referer).netloc}"
        except Exception:
            pass
    return headers


def _clean_url(url: str) -> str:
    """
    Normalize URL by stripping fragment and trimming obvious tracking params.
    Keeps query params that look meaningful, drops common tracking.
    """
    url = (url or "").strip()
    if not url:
        return url

    p = urlparse(url)
    # drop fragment
    fragmentless = p._replace(fragment="")

    # remove common tracking params
    qs = parse_qsl(fragmentless.query, keep_blank_values=True)
    qs2 = [(k, v) for (k, v) in qs if k not in TRACKING_PARAMS]
    new_query = urlencode(qs2, doseq=True)

    return urlunparse(fragmentless._replace(query=new_query))


def _looks_like_pdf_url(url: str) -> bool:
    u = (url or "").lower()
    if ".pdf" in u:
        return True
    # Some CDNs serve PDFs behind query params
    if "pdf" in u and ("download" in u or "file" in u or "document" in u):
        return True
    return False


def _is_pdf_response(content_type: str, first_bytes: bytes) -> bool:
    """
    Check if response is a PDF.
    If STRICT_PDF_VALIDATION is False (default), accept PDFs with non-standard headers.
    If STRICT_PDF_VALIDATION is True, require proper Content-Type or magic bytes.
    """
    ct = (content_type or "").lower()
    has_pdf_ct = "application/pdf" in ct
    has_magic_bytes = first_bytes.startswith(b"%PDF-")
    
    if STRICT_PDF_VALIDATION:
        # Strict: require either proper content-type or magic bytes
        return has_pdf_ct or has_magic_bytes
    else:
        # Permissive: accept if magic bytes present, OR if content-type suggests PDF
        if has_magic_bytes:
            return True
        if has_pdf_ct:
            return True
        # Also accept octet-stream if it looks like a PDF inside
        if "application/octet-stream" in ct and has_magic_bytes:
            return True
        return False


def _extract_pdf_link_from_html(html: str, base_url: str) -> Optional[str]:
    """
    Conservative extraction: find a PDF link inside HTML.
    Prefer simple .pdf links in anchors (like old codebase).
    Also checks: meta refresh, JSON-LD, direct URLs in scripts.
    """
    if not html:
        return None

    # 1) href="...pdf" (simple anchors - preferred)
    m = re.search(r'href\s*=\s*["\']([^"\']*\.pdf[^"\']*)["\']', html, flags=re.I)
    if m:
        return urljoin(base_url, m.group(1))

    # 2) Direct https://...pdf in scripts/text
    m = re.search(r'https?://[^\s\'"]+\.pdf[^\s\'"]*', html, flags=re.I)
    if m:
        return m.group(0)

    # 3) iframe/object/embed src attributes pointing to PDFs
    for tag_pattern in [
        r'<iframe[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<object[^>]+data=["\']([^"\']+\.pdf[^"\']*)["\']',
        r'<embed[^>]+src=[^"\']([^"\']+\.pdf[^"\']*)["\']'
    ]:
        m = re.search(tag_pattern, html, flags=re.I)
        if m:
            return urljoin(base_url, m.group(1))

    # 4) Meta refresh content="0; url=..."
    m = re.search(r'http-equiv\s*=\s*["\']refresh["\'][^>]*content\s*=\s*["\'][^;]*url=([^"\']+)["\']',
                  html, flags=re.I)
    if m:
        trial_url = urljoin(base_url, m.group(1))
        if ".pdf" in trial_url.lower():
            return trial_url

    return None


def _backoff_sleep(attempt: int, base: float = DEFAULT_BACKOFF_BASE) -> None:
    """
    Conditional backoff: only sleep if ENABLE_FETCH_BACKOFF is True.
    If False (default), returns immediately for fast retries.
    If True, uses exponential backoff with jitter.
    """
    if not ENABLE_FETCH_BACKOFF:
        # No backoff - return immediately
        return

    # Exponential backoff with jitter (only if enabled)
    t = base * (2 ** max(attempt - 1, 0)) + random.random() * 0.2
    time.sleep(min(t, FETCH_BACKOFF_MAX))


def _warmup_request(
    sess: requests.Session,
    url: str,
    headers: dict,
    timeout: float,
    *,
    page_url: Optional[str] = None,
) -> None:
    """
    Warm up the request by simulating a real browsing chain before the PDF download.

    If page_url is provided (the report listing/landing page), it is visited FIRST so
    the CDN sees a realistic referrer chain (user landed on the page, then clicked the PDF).
    Then falls through to the PDF's origin root and direct parent directory.

    If ENABLE_FETCH_WARMUP is False, this does nothing.
    """
    if not ENABLE_FETCH_WARMUP:
        return

    try:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        site_root = f"{origin}/"
        dir_url = url.rsplit("/", 1)[0] + "/"

        # Warmup requests are HTML page fetches, not PDF downloads — use a browser Accept header
        # so WAFs don't flag them as non-human traffic.
        html_headers = dict(headers)
        html_headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        html_headers["Sec-Fetch-Dest"] = "document"
        html_headers["Sec-Fetch-Mode"] = "navigate"
        html_headers["Sec-Fetch-Site"] = "none"

        # Build the warmup chain: report listing page first (if known), then PDF origin/dir.
        # Visiting the page URL first is the most important step — it's what a real user does
        # before clicking the PDF link, and CDNs track this context per session.
        warm_chain = []
        if page_url and page_url != url:
            warm_chain.append(page_url)
        warm_chain.extend([site_root, dir_url])

        for warm_url in warm_chain:
            try:
                sess.get(warm_url, headers=html_headers, timeout=timeout, allow_redirects=True)
                # Small delay between warmup requests to look like natural browsing
                time.sleep(random.uniform(0.3, 1.1))
            except Exception as e:
                logger.debug(f"Warmup request failed (ignored) for {warm_url}: {e}")
                continue
    except Exception as e:
        logger.debug(f"Warmup sequence failed (ignored): {e}")


# ---------------------------------------------------------------------
# SSRF protection
# ---------------------------------------------------------------------
def _is_safe_url(url: str) -> bool:
    """Return False if the URL uses a non-HTTP(S) scheme or resolves to a private/loopback/link-local address."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname
        if not host:
            return False
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except Exception:
        return False


# ---------------------------------------------------------------------
# Core fetch functions
# ---------------------------------------------------------------------
def fetch_bytes(
    url: str,
    *,
    referer: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    allow_html_fallback: bool = True,
    session: Optional[requests.Session] = None,
) -> Tuple[bytes, FetchResult]:
    """
    Download content at URL. If it returns HTML and allow_html_fallback is True,
    try to find a PDF link inside HTML and retry that link.

    Returns: (content_bytes, FetchResult)
    Raises requests.HTTPError for non-retriable failures after retries.
    """
    if not _is_safe_url(url):
        raise ValueError(f"Blocked request to private/internal address: {url}")

    clean_url = _clean_url(url)
    parsed_domain = urlparse(clean_url).netloc

    # Session selection: per-domain cache takes priority so CDN session cookies (e.g. Akamai
    # AKA_A2) issued on first visit survive across retries and subsequent pipeline fetches.
    if REUSE_SESSIONS_PER_DOMAIN:
        if parsed_domain not in _domain_sessions:
            if len(_domain_sessions) >= MAX_DOMAIN_SESSIONS:
                _domain_sessions.popitem(last=False)  # evict least-recently-used
            _domain_sessions[parsed_domain] = requests.Session()
        else:
            _domain_sessions.move_to_end(parsed_domain)  # mark as recently used
        sess = _domain_sessions[parsed_domain]
    elif REUSE_FETCH_SESSIONS:
        sess = session or requests.Session()
    else:
        # Always create a fresh session for isolation (backward-compatible default)
        sess = requests.Session()

    last_exc: Optional[Exception] = None
    got_403 = False

    for attempt in range(1, max_retries + 1):
        ua = _pick_user_agent()

        # Choose Accept header based on content type expected: PDFs get a PDF-specific Accept
        # so CDNs serving mixed content can serve the right MIME type without negotiation guessing.
        is_pdf_url = _looks_like_pdf_url(clean_url)
        accept = (
            "application/pdf,*/*;q=0.8"
            if is_pdf_url
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        )
        headers = _build_headers(referer=referer, user_agent=ua, accept=accept)

        # Attempt 1: include Range header (many servers prefer it for PDFs).
        # Retries: drop Range — some CDN WAFs reject Range requests from non-browsers,
        # so falling back to a plain GET gives a better chance on 403 retries.
        if attempt == 1:
            req_headers = dict(headers)
            req_headers["Range"] = "bytes=0-"
        else:
            req_headers = headers

        # On the last retry after a 403, use a completely fresh session + new UA so we
        # don't carry cookies/state that the CDN may have already flagged.
        active_sess = sess
        if got_403 and attempt == max_retries:
            active_sess = requests.Session()
            fresh_ua = _pick_user_agent()
            req_headers = _build_headers(referer=referer, user_agent=fresh_ua, accept=accept)
            logger.debug(f"Last-chance retry with fresh session for {parsed_domain}")

        try:
            if ENABLE_FETCH_WARMUP and attempt == 1 and allow_html_fallback:
                # Warm up: visit the report listing page (if known via referer) then the PDF's
                # origin/directory. This simulates a real user navigating to the page and then
                # clicking the PDF link, which is the browsing chain CDNs expect to see.
                _warmup_request(active_sess, clean_url, headers, timeout, page_url=referer)

            # Per-domain rate limiting: ensure a minimum gap between requests to the same host.
            # CDNs count request rate per IP; hitting the same domain twice within seconds often
            # triggers a temporary 403 window.
            if FETCH_DOMAIN_MIN_DELAY > 0:
                last = _domain_last_request.get(parsed_domain, 0.0)
                elapsed = time.monotonic() - last
                if elapsed < FETCH_DOMAIN_MIN_DELAY:
                    wait = FETCH_DOMAIN_MIN_DELAY - elapsed
                    logger.debug(f"Domain rate limit: sleeping {wait:.2f}s before request to {parsed_domain}")
                    time.sleep(wait)

            r = active_sess.get(clean_url, headers=req_headers, timeout=timeout, allow_redirects=True)
            # Record the timestamp immediately after the request so subsequent calls to the same
            # domain (including retries that change clean_url) respect the minimum delay.
            _domain_last_request[parsed_domain] = time.monotonic()

            status = r.status_code
            ct = (r.headers.get("Content-Type") or "").strip()
            final_url = str(r.url)

            # Handle 403/429 with retry (often WAF/rate limiting)
            if status in (403, 429, 500, 502, 503, 504):
                if attempt < max_retries:
                    if status == 403:
                        got_403 = True
                        # 403 from a CDN is almost always a short rate-limit window, not a hard
                        # block. Mandatory brief pause (with jitter) before retry, regardless of
                        # ENABLE_FETCH_BACKOFF, gives the CDN window time to expire.
                        delay = random.uniform(FETCH_403_RETRY_DELAY_MIN, FETCH_403_RETRY_DELAY_MAX)
                        logger.debug(f"403 received from {parsed_domain}; sleeping {delay:.1f}s before retry")
                        time.sleep(delay)
                    else:
                        _backoff_sleep(attempt)
                    continue
                r.raise_for_status()

            # If it's HTML and we want to attempt to resolve a PDF inside it:
            body = r.content or b""
            first = body[:8]
            was_pdf = _is_pdf_response(ct, first)

            if not was_pdf and allow_html_fallback:
                # if content-type says html or bytes don't look like PDF, try parse
                if "text/html" in ct.lower() or body[:200].lstrip().startswith(b"<!DOCTYPE") or body[:50].lstrip().startswith(b"<"):
                    html = ""
                    try:
                        html = r.text or ""
                    except Exception:
                        try:
                            html = body.decode("utf-8", errors="ignore")
                        except Exception:
                            html = ""
                    pdf_link = _extract_pdf_link_from_html(html, base_url=final_url)
                    if pdf_link and pdf_link != clean_url:
                        # Retry downloading the discovered PDF, with referer as the page
                        clean_url = _clean_url(pdf_link)
                        referer = final_url
                        # Backoff before next attempt
                        _backoff_sleep(attempt)
                        continue

            # If still not PDF but URL itself looks like PDF, accept it (some servers send octet-stream)
            # We'll still pass bytes/magic check back to caller.
            result = FetchResult(
                final_url=final_url,
                content_type=ct,
                status_code=status,
                bytes_len=len(body),
                was_pdf=was_pdf,
            )
            return body, result

        except Exception as e:
            last_exc = e
            if attempt < max_retries:
                _backoff_sleep(attempt)
                continue
            # final attempt -> raise
            raise

    # Should never hit
    raise RuntimeError(f"Failed to fetch after retries: {clean_url}. Last error: {last_exc}")


def download_to_file(
    url: str,
    out_path: Path,
    *,
    referer: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
    allow_html_fallback: bool = True,
    session: Optional[requests.Session] = None,
) -> FetchResult:
    """
    Download URL to out_path. Creates parent dirs if needed.
    Returns FetchResult.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    content, meta = fetch_bytes(
        url,
        referer=referer,
        timeout=timeout,
        max_retries=max_retries,
        allow_html_fallback=allow_html_fallback,
        session=session,
    )
    out_path.write_bytes(content)
    return meta


# ---------------------------------------------------------------------
# Convenience for pipeline validator
# ---------------------------------------------------------------------
def download_pdf(
    pdf_url: str,
    tmp_dir: Path,
    *,
    filename_hint: str = "document",
    referer: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Tuple[Path, str, str]:
    """
    Downloads a PDF URL to tmp_dir and returns:
      (local_path, final_url, content_type)

    Validates that the saved file is a real PDF by:
      - Content-Type includes application/pdf OR
      - first bytes start with %PDF-
      
    If STRICT_PDF_VALIDATION is False (default), also accepts:
      - application/octet-stream with %PDF- magic bytes
      - PDFs with non-standard Content-Type but valid magic bytes
      
    Raises RuntimeError if response is not a PDF (unless validation is relaxed).
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", filename_hint).strip("_") or "document"
    local_path = tmp_dir / f"{safe_name}.pdf"

    # Fetch bytes so we can validate PDF-ness before writing
    content, meta = fetch_bytes(
        pdf_url,
        referer=referer,
        timeout=timeout,
        max_retries=max_retries,
        allow_html_fallback=True,
    )

    # Validate it's actually a PDF
    if not _is_pdf_response(meta.content_type, content[:8]):
        snippet = ""
        try:
            snippet = content[:200].decode("utf-8", errors="ignore")
        except Exception:
            snippet = ""
        
        # If not strict validation and we have significant content, try a bit harder
        if not STRICT_PDF_VALIDATION and len(content) > 1000:
            # Search for %PDF in the first kilobyte
            if b"%PDF-" in content[:1000]:
                logger.info(f"Accepting PDF despite non-standard headers: {meta.content_type}")
                local_path.write_bytes(content)
                return local_path, meta.final_url, meta.content_type
        
        raise RuntimeError(
            f"Not a PDF by headers or magic bytes (Content-Type={meta.content_type}, "
            f"final_url={meta.final_url}). Snippet={snippet[:120]!r}"
        )

    local_path.write_bytes(content)
    return local_path, meta.final_url, meta.content_type


def download_document(
    url: str,
    tmp_dir: Path,
    *,
    filename_hint: str = "document",
    referer: Optional[str] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> Tuple[Path, str, str]:
    """
    Downloads a document (PDF/DOCX/etc.) to tmp_dir.

    - Keeps the extension from the URL path if present.
    - Falls back to extension inferred from Content-Type.
    - Allows HTML fallback because many "download" links are viewer pages.

    Returns: (local_path, final_url, content_type)
    """
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Decide extension
    parsed = urlparse(url)
    ext = Path(parsed.path).suffix.lower()

    content, meta = fetch_bytes(
        url,
        referer=referer,
        timeout=timeout,
        max_retries=max_retries,
        allow_html_fallback=True,
    )

    if not ext or ext in (".php", ".aspx", ".jsp", ".html", ".htm"):
        ct = (meta.content_type or "").lower()
        if "application/pdf" in ct or content[:8].startswith(b"%PDF-"):
            ext = ".pdf"
        elif "officedocument.wordprocessingml.document" in ct:
            ext = ".docx"
        elif "msword" in ct:
            ext = ".doc"
        elif "officedocument.spreadsheetml.sheet" in ct:
            ext = ".xlsx"
        elif "officedocument.presentationml.presentation" in ct:
            ext = ".pptx"
        else:
            ext = ".bin"

    safe_name = re.sub(r"[^a-zA-Z0-9_\-\.]+", "_", filename_hint).strip("_") or "document"
    local_path = tmp_dir / f"{safe_name}{ext}"

    local_path.write_bytes(content)
    return local_path, meta.final_url, meta.content_type