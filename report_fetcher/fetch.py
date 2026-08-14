# fetch.py
from __future__ import annotations

import ipaddress
import json
import logging
import random
import re
import socket
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import requests
import requests.compat
from bs4 import BeautifulSoup

from .config import (
    ALLOW_NEW_DOWNLOADS,
    FETCH_HEAD_TIMEOUT,
    MAX_PDF_DOWNLOAD_BYTES,
    PDF_DOWNLOAD_TIMEOUT_SECONDS,
    TRACKING_PARAMS,
    USE_DOWNLOAD_CACHE,
)

# Robustness knobs added for the hardened downloader. Imported defensively so an
# older config.py (missing these keys) still loads — fetch.py then falls back to
# the same defaults config.py would supply.
try:
    from .config import (
        CURL_IMPERSONATE,
        FETCH_DOMAIN_MIN_DELAY,
        MAX_DOMAIN_SESSIONS,
        PDF_DOWNLOAD_RETRY_ROUNDS,
        PDF_RETRY_BACKOFF_SECONDS,
        STRICT_PDF_VALIDATION,
    )
except Exception:  # pragma: no cover - fallback for a stale config
    CURL_IMPERSONATE = "chrome"
    FETCH_DOMAIN_MIN_DELAY = 3.5
    MAX_DOMAIN_SESSIONS = 200
    PDF_DOWNLOAD_RETRY_ROUNDS = 3
    PDF_RETRY_BACKOFF_SECONDS = (5, 20)
    STRICT_PDF_VALIDATION = False

# curl_cffi impersonates a real Chrome TLS handshake (JA3 fingerprint), which
# gets past WAFs that silently drop connections from Python's default TLS stack
# no matter what headers are sent. Falls back to plain requests if not installed.
try:
    from curl_cffi import requests as curl_requests

    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover - optional dependency
    curl_requests = None
    HAVE_CURL_CFFI = False

logger = logging.getLogger(__name__)


# ---- constants --------------------------------------------------------------

# 403 from a CDN is often a short rate-limit window, not a hard block — the round
# backoff gives the window time to expire before retrying.
RETRYABLE_HTTP_STATUS = {403, 429, 500, 502, 503, 504}

# Browser profiles keep the User-Agent and client hints consistent: real
# Firefox/Safari never send sec-ch-ua, so attaching a Chromium hint to their UA
# (as a flat UA pool would) is itself a fingerprint WAFs flag.
BROWSER_PROFILES = [
    {  # Chrome on Windows
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
        "platform": '"Windows"',
    },
    {  # Edge on Windows
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
        "sec_ch_ua": '"Chromium";v="136", "Microsoft Edge";v="136", "Not.A/Brand";v="99"',
        "platform": '"Windows"',
    },
    {  # Firefox on Windows (no client hints)
        "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
        "sec_ch_ua": None,
        "platform": None,
    },
    {  # Safari on macOS (no client hints)
        "ua": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
        "sec_ch_ua": None,
        "platform": None,
    },
]

# Per-domain session cache (LRU) so WAF/CDN cookies earned during a warm-up
# survive across retries and across rows that hit the same host.
_domain_sessions: "OrderedDict[str, object]" = OrderedDict()  # netloc -> session
_domain_last_request: dict = {}  # netloc -> time.monotonic() of last request


# ---- error types ------------------------------------------------------------


class FileTooLargeError(ValueError):
    """Raised when a download would exceed MAX_PDF_DOWNLOAD_BYTES."""


# ---- SSRF guard -------------------------------------------------------------


def _is_safe_url(url: str) -> bool:
    """Return False if the URL is non-HTTP(S) or resolves to a private/loopback address."""
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


# ---- session management -----------------------------------------------------


def _new_session():
    """Create a session; Chrome-impersonating (TLS + headers) when curl_cffi is available."""
    if HAVE_CURL_CFFI:
        return curl_requests.Session(impersonate=CURL_IMPERSONATE)
    return requests.Session()


def _session_for_domain(netloc: str):
    """Return the cached session for a host, creating one if needed (LRU-capped).

    Reusing one session per host means CDN cookies (e.g. Akamai AKA_A2) earned on
    a warm-up visit are still presented on retries and on later rows that hit the
    same domain.
    """
    netloc = (netloc or "").lower()
    if netloc in _domain_sessions:
        _domain_sessions.move_to_end(netloc)
        return _domain_sessions[netloc]
    if len(_domain_sessions) >= MAX_DOMAIN_SESSIONS:
        _, evicted = _domain_sessions.popitem(last=False)
        try:
            evicted.close()
        except Exception:
            pass
    sess = _new_session()
    _domain_sessions[netloc] = sess
    return sess


def _respect_domain_delay(netloc: str) -> None:
    """Sleep just enough to keep FETCH_DOMAIN_MIN_DELAY between requests to a host."""
    if FETCH_DOMAIN_MIN_DELAY <= 0 or not netloc:
        return
    last = _domain_last_request.get(netloc)
    if last is None:
        return
    remaining = FETCH_DOMAIN_MIN_DELAY - (time.monotonic() - last)
    if remaining > 0:
        logger.debug("Domain pacing: sleeping %.1fs before request to %s", remaining, netloc)
        time.sleep(remaining)


# ---- streaming helper -------------------------------------------------------


def _read_response_body(r) -> bytes:
    """Stream response body in chunks, raising FileTooLargeError if limit exceeded."""
    chunks: list[bytes] = []
    total = 0
    for chunk in r.iter_content(65536):
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_PDF_DOWNLOAD_BYTES:
            r.close()
            raise FileTooLargeError(
                f"Response exceeds {MAX_PDF_DOWNLOAD_BYTES:,} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


# ---- size pre-check ---------------------------------------------------------


def _check_content_length(
    sess,
    url: str,
    headers: dict,
    timeout: float,
) -> None:
    """HEAD request to reject oversized files before downloading.

    Silently skips if HEAD is not supported or times out — the streaming cap
    in _read_response_body handles servers that don't advertise Content-Length.
    """
    try:
        r = sess.head(url, headers=headers, timeout=FETCH_HEAD_TIMEOUT, allow_redirects=True)
        cl = r.headers.get("Content-Length", "")
        if cl.isdigit() and int(cl) > MAX_PDF_DOWNLOAD_BYTES:
            raise FileTooLargeError(
                f"Content-Length {int(cl):,} bytes exceeds limit for {url}"
            )
    except FileTooLargeError:
        raise
    except Exception:
        pass


# ---- header helpers ---------------------------------------------------------


def build_headers(profile: Optional[dict] = None):
    """Return (pdf_headers, html_headers) for a consistent browser profile.

    Both header sets describe a top-level navigation (a user clicking a link),
    which is what a real browser sends when opening a PDF URL — the old
    same-origin/no-cors combination was not a fingerprint any browser produces.
    Client-hint headers are only included for Chromium-based profiles.
    """
    profile = profile or random.choice(BROWSER_PROFILES)
    common = {
        "User-Agent": profile["ua"],
        "Accept-Language": "en-US,en;q=0.9,zh-CN,zh;q=0.8,ja-JP,ja;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-User": "?1",
    }
    if profile["sec_ch_ua"]:
        common["sec-ch-ua"] = profile["sec_ch_ua"]
        common["sec-ch-ua-mobile"] = "?0"
        common["sec-ch-ua-platform"] = profile["platform"]

    pdf = dict(common, Accept="application/pdf,application/octet-stream;q=0.9,*/*;q=0.8")
    html = dict(
        common,
        Accept="text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    )
    return pdf, html


def with_headers(base: dict, **overrides) -> dict:
    """
    Lightweight copy+update: with_headers(h, Referer=url, Range="bytes=0-")
    """
    h = dict(base)
    h.update({k: v for k, v in overrides.items() if v is not None})
    return h


def _headers_for(url: str) -> tuple[dict, dict]:
    """Return (pdf_headers, html_headers) appropriate for the URL's host."""
    if HAVE_CURL_CFFI:
        # Chrome impersonation already sends a complete, self-consistent header
        # set (UA, sec-ch-ua, Sec-Fetch, Accept) matched to its TLS fingerprint.
        # Overriding any of them would reintroduce a mismatch, so only
        # per-request extras (Referer, Range) get layered on via with_headers.
        return {}, {}
    return build_headers()


# ---- PDF validation ---------------------------------------------------------


def _is_pdf_response(resp) -> bool:
    """Header/URL heuristic that a response *claims* to be a PDF."""
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ctype:
        return True
    if "application/octet-stream" in ctype:
        cd = (resp.headers.get("Content-Disposition") or "").lower()
        if ".pdf" in cd or "filename=" in cd:
            return True
    if (str(getattr(resp, "url", "")) or "").lower().endswith(".pdf"):
        return True
    return False


def _looks_like_html(body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return (
        head.startswith(b"<!doctype")
        or head.startswith(b"<html")
        or head.startswith(b"<head")
        or b"<html" in head
    )


def _is_valid_pdf(resp, body: bytes) -> bool:
    """Validate a candidate PDF by its bytes, not just its headers.

    A real PDF carries the ``%PDF-`` signature within the first 1 KB (the window
    Acrobat itself scans). Trusting Content-Type alone lets a ``200
    application/pdf`` response that actually holds a WAF challenge or error page
    get saved as a corrupt file — the failure mode this guards against.

    Default (lenient): accept if the signature is present, or if the header/URL
    claims PDF and the body is not HTML. Strict (STRICT_PDF_VALIDATION): require
    the signature unconditionally.
    """
    if not body:
        return False
    if b"%PDF-" in body[:1024]:
        return True
    if STRICT_PDF_VALIDATION:
        return False
    return _is_pdf_response(resp) and not _looks_like_html(body)


# ---- request helper ---------------------------------------------------------


def _safe_get(sess, url, headers, timeout, stream=True, pace=True):
    """GET that returns (response, None) or (None, short error string).

    Works with both requests and curl_cffi sessions. Isolating every GET behind
    this helper means a timeout or connection error in one strategy no longer
    aborts the strategies that follow it. When ``pace`` is True the per-domain
    minimum delay is enforced first (warm-up calls pass pace=False since they
    already sleep between themselves). Every call records the request time so
    later paced calls to the same host keep their distance.
    """
    netloc = (urlparse(url).netloc or "").lower()
    if pace:
        _respect_domain_delay(netloc)
    try:
        r = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True,
                     verify=True, stream=stream)
        return r, None
    except Exception as e:
        name = type(e).__name__
        if "timeout" in name.lower() or "timed out" in str(e).lower():
            return None, "timeout"
        return None, name
    finally:
        _domain_last_request[netloc] = time.monotonic()


def _finalize(save_path: Path, content: bytes) -> None:
    """Write atomically via a .part file."""
    part_path = save_path.with_suffix(save_path.suffix + ".part")
    part_path.write_bytes(content)
    part_path.replace(save_path)


def _describe_response(r) -> tuple[str, bool]:
    """Return (short description, retryable) for a non-PDF response."""
    ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
    return f"HTTP {r.status_code} ({ctype or 'no content-type'})", r.status_code in RETRYABLE_HTTP_STATUS


# ---- main download ----------------------------------------------------------


def download_pdf(
    url: str,
    save_path: str | Path,
    *,
    sess=None,
    timeout: Optional[int] = None,
    page_url: Optional[str] = None,
) -> tuple[bool, Optional[str]]:
    """Backward-compatible entry point returning (ok, resolved_url).

    ``resolved_url`` is the URL the PDF bytes actually came from — equal to
    ``url`` for direct downloads, or the link parsed out of a page for the
    HTML/viewer and landing-page fallbacks (so callers can record the real PDF
    URL instead of the page that hosted it). None on failure.

    See :func:`download_pdf_detailed` for the variant that also returns the
    failure reason.
    """
    ok, resolved_url, _ = download_pdf_detailed(
        url, save_path, sess=sess, timeout=timeout, page_url=page_url
    )
    return ok, resolved_url


def download_pdf_detailed(
    url: str,
    save_path: str | Path,
    *,
    sess=None,
    timeout: Optional[int] = None,
    page_url: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[str]]:
    """
    Attempt to download a PDF from `url` into `save_path`.

    Each round tries three strategies, each shielded from the others' errors (a
    timeout in one no longer aborts the rest):
      1) Direct GET (byte-range enabled on round 1; some WAFs reject Range from
         non-browsers, so retry rounds drop it), with Referer set to the
         report's landing page when one is known
      2) If HTML/viewer is returned, parse for a real PDF link and fetch it
      3) Warm up origin (root/listing/dir + landing page) to appease WAF, then
         retry

    Rounds repeat up to PDF_DOWNLOAD_RETRY_ROUNDS times with backoff when the
    failure looks transient (connection error, HTTP 403/429/5xx). After a 403,
    the final round runs on a fresh session with a new browser profile,
    discarding any cookies the CDN may have flagged. If all rounds fail and
    `page_url` is given, the report's landing page is fetched and scanned for an
    alternative PDF link.

    Sessions default to a per-domain cache (Chrome-impersonating via curl_cffi
    when installed), so WAF cookies persist across rows.

    Returns:
        (ok, resolved_url, error_detail) — ok True on success, resolved_url the
        URL the bytes came from (None on failure), error_detail None on success
        and a short reason string on failure.
    """
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    timeout = timeout or PDF_DOWNLOAD_TIMEOUT_SECONDS

    try:
        # --- Cache Check ---
        if USE_DOWNLOAD_CACHE and save_path.exists() and save_path.stat().st_size > 1000:
            logger.info("PDF already exists in cache, skipping download: %s", save_path)
            return True, url, None

        # --- New Download Check ---
        if not ALLOW_NEW_DOWNLOADS:
            logger.info("New downloads disabled via config, skipping: %s", url)
            return False, None, "new downloads disabled via config"

        clean_url = _strip_tracking_params(url)

        # --- SSRF guard ---
        if not _is_safe_url(clean_url):
            logger.warning("Blocked private/internal URL: %s", url)
            return False, None, "URL blocked (unresolvable, private, or non-HTTP)"

        sess = sess or _session_for_domain(urlparse(clean_url).netloc)

        # Normalize the landing page once so strategies can use it as a Referer
        # (browse-then-click chain) as well as for the final fallback.
        page_url = str(page_url).strip() if page_url else None
        if page_url and not page_url.lower().startswith(("http://", "https://")):
            page_url = None

        detail = None
        all_timeout = False
        saw_403 = False
        for round_no in range(1, PDF_DOWNLOAD_RETRY_ROUNDS + 1):
            if round_no > 1:
                wait = PDF_RETRY_BACKOFF_SECONDS[
                    min(round_no - 2, len(PDF_RETRY_BACKOFF_SECONDS) - 1)
                ]
                logger.info(
                    "Transient failure, retry round %d/%d in %ds: %s",
                    round_no, PDF_DOWNLOAD_RETRY_ROUNDS, wait, clean_url,
                )
                time.sleep(wait)

            round_sess = sess
            if saw_403 and round_no == PDF_DOWNLOAD_RETRY_ROUNDS:
                # Last chance after a 403: a fresh session discards cookies the
                # CDN may already have flagged and re-primes from scratch.
                round_sess = _new_session()
                logger.info("Last-round retry on fresh session after 403: %s", clean_url)

            ok, resolved_url, detail, retryable, all_timeout, saw_403 = _run_download_strategies(
                round_sess, clean_url, url, save_path, timeout, page_url=page_url
            )
            if ok:
                return True, resolved_url, None
            if not retryable:
                break

        # --- Landing-page fallback ---
        if page_url:
            page_host = (urlparse(page_url).hostname or "").lower()
            pdf_host = (urlparse(clean_url).hostname or "").lower()
            if all_timeout and page_host == pdf_host:
                # The host never responds; the landing page will hang too.
                detail = f"{detail}; page fallback skipped (host unresponsive)"
            else:
                logger.info("Trying landing-page fallback: %s", page_url)
                ok, fb_url, fb_detail = _try_page_fallback(sess, page_url, save_path, timeout)
                if ok:
                    return True, fb_url, None
                detail = (
                    f"{detail}; page fallback: {fb_detail}" if detail else f"page fallback: {fb_detail}"
                )

        logger.warning("All attempts failed: %s (%s)", clean_url, detail)
        return False, None, detail or "download failed"

    except FileTooLargeError:
        logger.warning("PDF too large, skipping: %s", url)
        return False, None, f"exceeds size limit ({MAX_PDF_DOWNLOAD_BYTES:,} bytes)"
    except Exception as e:
        logger.exception("Unexpected error during download: %s", url)
        return False, None, f"unexpected error: {e}"


def _run_download_strategies(
    sess,
    clean_url: str,
    original_url: str,
    save_path: Path,
    timeout: float,
    page_url: Optional[str] = None,
) -> tuple[bool, Optional[str], Optional[str], bool, bool, bool]:
    """One round of the three download strategies.

    Returns:
        (ok, resolved_url, error_detail, retryable, all_timeout, saw_403) —
        retryable means the failure looked transient and another round may help;
        timeouts do NOT count (a host that hangs for a full timeout window won't
        recover in a few seconds). all_timeout is True when every attempt ended
        in a timeout, i.e. the host is unresponsive. saw_403 is True if any
        attempt got an HTTP 403, signalling the caller to try a fresh session
        next round. resolved_url is the URL the bytes came from on success.
    """
    pdf_headers, html_headers = _headers_for(clean_url)

    p = urlparse(clean_url)
    origin = f"{p.scheme}://{p.netloc}"
    site_root = f"{origin}/"
    dir_url = clean_url.rsplit("/", 1)[0] + "/"
    listing_url = f"{origin}/csrreports/"  # harmless if 404

    # Size pre-check (one HEAD before any GETs); raises FileTooLargeError
    _check_content_length(sess, clean_url, pdf_headers, timeout)

    errors: list[str] = []
    retryable = False
    saw_403 = False

    def _note_403(resp) -> None:
        nonlocal saw_403
        if getattr(resp, "status_code", None) == 403:
            saw_403 = True

    # Referer for the direct fetch: the report's landing page if we have one (a
    # real browse-then-click chain), otherwise the PDF's own directory.
    direct_referer = page_url or dir_url

    # --- Attempt 1: direct fetch ---
    logger.info("Download attempt 1 (direct): %s", clean_url)
    r, err = _safe_get(
        sess, clean_url,
        with_headers(pdf_headers, Range="bytes=0-", Referer=direct_referer),
        timeout,
    )
    if r is not None and r.ok:
        body = _read_response_body(r)
        r.close()
        if _is_valid_pdf(r, body):
            _finalize(save_path, body)
            logger.info("Downloaded PDF (direct): %s -> %s", clean_url, save_path)
            return True, original_url, None, False, False, False
        # 200 but not a real PDF — record it (may be an HTML viewer we parse next).
        ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip()
        errors.append(f"direct: HTTP {r.status_code} ({ctype or 'no content-type'}, not a PDF)")
    elif r is None:
        errors.append(f"direct: {err}")
        retryable = retryable or err != "timeout"
    else:
        r.close()
        _note_403(r)
        desc, retry_status = _describe_response(r)
        errors.append(f"direct: {desc}")
        retryable = retryable or retry_status

    # --- Attempt 2: parse HTML/viewer for real PDF link ---
    if r is not None and (
        "text/html" in (r.headers.get("Content-Type", "") or "").lower()
        or getattr(r, "status_code", 0) in (403, 451, 503)
    ):
        logger.info("Attempt 2: parsing HTML/viewer for PDF: %s", clean_url)
        html_resp, err = _safe_get(sess, clean_url, dict(html_headers), timeout, stream=False)
        if html_resp is None:
            errors.append(f"viewer-parse: {err}")
            retryable = retryable or err != "timeout"
        elif "text/html" in (html_resp.headers.get("Content-Type", "") or "").lower() and (
            html_resp.text or ""
        ):
            pdf_url = _find_pdf_link_in_html(html_resp.text, base_url=str(html_resp.url))
            if not pdf_url:
                errors.append("viewer-parse: no PDF link found")
            elif not _is_safe_url(pdf_url):
                errors.append("viewer-parse: found link blocked (SSRF)")
            else:
                logger.info("Found PDF link on page: %s", pdf_url)
                hdrs2 = with_headers(pdf_headers, Referer=str(html_resp.url), Range="bytes=0-")
                pdf_resp, err2 = _safe_get(sess, pdf_url, hdrs2, timeout)
                if pdf_resp is not None and pdf_resp.ok:
                    body = _read_response_body(pdf_resp)
                    pdf_resp.close()
                    if _is_valid_pdf(pdf_resp, body):
                        _finalize(save_path, body)
                        logger.info("Downloaded PDF (parsed link): %s -> %s", pdf_url, save_path)
                        return True, pdf_url, None, False, False, False
                    errors.append("viewer-parse: linked resource is not a PDF")
                elif pdf_resp is None:
                    errors.append(f"viewer-parse: {err2}")
                    retryable = retryable or err2 != "timeout"
                else:
                    pdf_resp.close()
                    _note_403(pdf_resp)
                    desc, retry_status = _describe_response(pdf_resp)
                    errors.append(f"viewer-parse: {desc}")
                    retryable = retryable or retry_status

    # --- Attempt 3: WAF warm-up then retry ---
    logger.info("Attempt 3: WAF warm-up then retry")
    # Warm-ups are cookie/session primers, not downloads — keep their timeout
    # short so a hanging host can't burn 3 full timeout windows. Visit the real
    # landing page first (if known) so the session carries the cookies a genuine
    # browse-then-click flow would have earned.
    warm_timeout = min(timeout, 10)
    warm_chain = [u for u in (page_url, site_root, listing_url, dir_url) if u]
    for warm_url in warm_chain:
        _safe_get(sess, warm_url, dict(html_headers), warm_timeout, stream=False, pace=False)
        time.sleep(random.uniform(0.3, 1.1))

    # Drop the Range header here — some WAFs reject Range from non-browsers, so
    # the warm-up retry looks more like a plain browser navigation.
    retry_referer = page_url or listing_url
    hdrs3 = with_headers(pdf_headers, Referer=retry_referer)
    logger.info("Retry after warm-up: %s", clean_url)
    r2, err = _safe_get(sess, clean_url, hdrs3, timeout)
    if r2 is not None and r2.ok:
        body = _read_response_body(r2)
        r2.close()
        if _is_valid_pdf(r2, body):
            _finalize(save_path, body)
            logger.info("Downloaded PDF (after warm-up): %s -> %s", clean_url, save_path)
            return True, original_url, None, False, False, False
        ctype = (r2.headers.get("Content-Type", "") or "").split(";")[0].strip()
        errors.append(f"warm-up retry: HTTP {r2.status_code} ({ctype or 'no content-type'}, not a PDF)")
    elif r2 is None:
        errors.append(f"warm-up retry: {err}")
        retryable = retryable or err != "timeout"
    else:
        r2.close()
        _note_403(r2)
        desc, retry_status = _describe_response(r2)
        errors.append(f"warm-up retry: {desc}")
        retryable = retryable or retry_status

    all_timeout = bool(errors) and all(e.endswith("timeout") for e in errors)
    return False, None, "; ".join(errors), retryable, all_timeout, saw_403


def _try_page_fallback(
    sess,
    page_url: str,
    save_path: Path,
    timeout: float,
) -> tuple[bool, Optional[str], Optional[str]]:
    """Fetch the report's landing page and try to download a PDF linked from it.

    Returns (ok, resolved_url, error_detail).
    """
    if not page_url.lower().startswith(("http://", "https://")):
        return False, None, "invalid page URL"
    if not _is_safe_url(page_url):
        return False, None, "page URL blocked (SSRF)"

    pdf_headers, html_headers = _headers_for(page_url)
    resp, err = _safe_get(sess, page_url, dict(html_headers), timeout, stream=False)
    if resp is None:
        return False, None, f"page fetch {err}"

    # The "page" URL may itself be a PDF.
    if resp.ok and _is_pdf_response(resp):
        body = resp.content
        if len(body) > MAX_PDF_DOWNLOAD_BYTES:
            raise FileTooLargeError(f"Landing page PDF exceeds {MAX_PDF_DOWNLOAD_BYTES:,} bytes")
        if _is_valid_pdf(resp, body):
            _finalize(save_path, body)
            logger.info("Downloaded PDF (page URL was a PDF): %s -> %s", page_url, save_path)
            return True, page_url, None

    if not resp.ok:
        desc, _ = _describe_response(resp)
        return False, None, f"page fetch {desc}"

    pdf_url = _find_pdf_link_in_html(resp.text or "", base_url=str(resp.url))
    if not pdf_url:
        return False, None, "no PDF link found on page"
    if not _is_safe_url(pdf_url):
        return False, None, "found link blocked (SSRF)"

    logger.info("Found PDF link on landing page: %s", pdf_url)
    hdrs = with_headers(pdf_headers, Referer=str(resp.url), Range="bytes=0-")
    pdf_resp, err2 = _safe_get(sess, pdf_url, hdrs, timeout)
    if pdf_resp is not None and pdf_resp.ok:
        body = _read_response_body(pdf_resp)
        pdf_resp.close()
        if _is_valid_pdf(pdf_resp, body):
            _finalize(save_path, body)
            logger.info("Downloaded PDF (landing page): %s -> %s", pdf_url, save_path)
            return True, pdf_url, None
        return False, None, "linked resource is not a PDF"
    if pdf_resp is None:
        return False, None, f"linked PDF {err2}"
    pdf_resp.close()
    desc, _ = _describe_response(pdf_resp)
    return False, None, f"linked PDF {desc}"


# ---- page scraping ----------------------------------------------------------


def scrape_report_page_for_pdfs(
    url: str,
    city_name_for_filename: str,  # kept for signature parity; not used here
    country: str,  # kept for signature parity; not used here
    *,
    visited: Optional[set[str]] = None,
    sess=None,
    timeout: int = 20,
) -> list[str]:
    """
    Scrape an HTML page and extract candidate PDF links (.pdf endings or via HEAD Content-Type).
    Returns a de-duplicated list of absolute URLs.
    """
    sess = sess or _session_for_domain(urlparse(url).netloc)
    visited = visited or set()

    if url in visited:
        logger.debug("Skipping already visited page: %s", url)
        return []
    visited.add(url)

    potential_pdf_links: list[str] = []

    _, html_headers = _headers_for(url)

    try:
        r, err = _safe_get(sess, url, dict(html_headers), timeout, stream=False)
        if r is None:
            logger.debug("Error fetching page (ignored): %s | %s", url, err)
            return []
        if not r.ok:
            logger.debug("Non-OK page (ignored): %s | HTTP %s", url, r.status_code)
            return []

        if "text/html" not in (r.headers.get("Content-Type", "") or "").lower():
            logger.debug("Skipping non-HTML content: %s", url)
            return []

        soup = BeautifulSoup(r.content, "html.parser")
        all_links = [str(a.get("href")) for a in soup.find_all("a", href=True)]

        for link in all_links:
            full_url = urljoin(url, link)
            if full_url.lower().endswith(".pdf"):
                potential_pdf_links.append(full_url)
            else:
                try:
                    h = sess.head(full_url, headers=dict(html_headers), timeout=5, allow_redirects=True)
                    ctype = (h.headers.get("Content-Type") or "").lower()
                    if "application/pdf" in ctype:
                        potential_pdf_links.append(full_url)
                except Exception:
                    pass  # ignore link errors

    except Exception as e:
        logger.debug("Error fetching page (ignored): %s | %s", url, e)

    deduped = list(dict.fromkeys(potential_pdf_links))  # de-duplicate, preserve order
    logger.info("Found %d potential PDF links at %s", len(deduped), url)
    return deduped


# ---- helpers ----------------------------------------------------------------


def _strip_tracking_params(url: str) -> str:
    p = urlparse(url)
    q = [
        (k, v)
        for k, v in parse_qsl(p.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    ]
    return urlunparse(p._replace(query=urlencode(q)))


def _find_pdf_link_in_html(html: str, base_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")

    # Anchors with .pdf in the href
    for a in soup.find_all("a", href=True):
        href = str(a["href"])
        if ".pdf" in href.lower():
            return requests.compat.urljoin(base_url, href)

    # Embedded viewers
    for tag in soup.find_all(["iframe", "object", "embed"]):
        src = tag.get("src") or tag.get("data")
        src = str(src)
        if not src:
            continue
        if ".pdf" in src.lower() or "application/pdf" in (str(tag.get("type")) or "").lower():
            return requests.compat.urljoin(base_url, src)

    # JSON-LD
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "{}")
            nodes = data if isinstance(data, list) else [data]
            for n in nodes:
                u = (n or {}).get("contentUrl") or (n or {}).get("url")
                if u and ".pdf" in (u or "").lower():
                    return requests.compat.urljoin(base_url, u)
        except Exception:
            pass

    # Scripts with direct links
    for s in soup.find_all("script"):
        txt = s.string or s.text or ""
        m = re.search(r'https?://[^\s\'"]+\.pdf', txt, flags=re.I)
        if m:
            return m.group(0)

    return None
