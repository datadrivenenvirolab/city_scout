# fetch.py
from __future__ import annotations

import ipaddress
import json
import logging
import random
import re
import socket
import time
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

logger = logging.getLogger(__name__)


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


# ---- streaming helper -------------------------------------------------------


def _read_response_body(r: requests.Response) -> bytes:
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
    sess: requests.Session,
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


def build_headers(user_agent: str):
    """
    Return (pdf_headers, html_headers) prefilled with a realistic browser profile.
    """
    pdf = {
        "User-Agent": user_agent,
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,zh-CN,zh;q=0.8,ja-JP,ja;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "no-cors",
        "Sec-Fetch-Dest": "document",
        "sec-ch-ua": '"Chromium";v="136", "Not=A?Brand";v="24"',
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-mobile": "?0",
    }

    html = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": pdf["Accept-Language"],
        "Accept-Encoding": pdf["Accept-Encoding"],
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Dest": "document",
        "sec-ch-ua": pdf["sec-ch-ua"],
        "sec-ch-ua-platform": pdf["sec-ch-ua-platform"],
        "sec-ch-ua-mobile": pdf["sec-ch-ua-mobile"],
    }
    return pdf, html


def with_headers(base: dict, **overrides) -> dict:
    """
    Lightweight copy+update: with_headers(h, Referer=url, Range="bytes=0-")
    """
    h = dict(base)
    h.update({k: v for k, v in overrides.items() if v is not None})
    return h


# ---- main download ----------------------------------------------------------


def download_pdf(
    url: str,
    save_path: str | Path,
    *,
    sess: Optional[requests.Session] = None,
    timeout: Optional[int] = None,
) -> bool:
    """
    Attempt to download a PDF from `url` into `save_path`.

    Strategy:
      1) Direct GET (byte-range enabled)
      2) If HTML/viewer is returned, parse for a real PDF link and fetch it
      3) Warm up origin (root/listing/dir) to appease WAF, then retry

    Returns:
        True on success, False otherwise.
    """
    save_path = Path(save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    sess = sess or requests.Session()
    timeout = timeout or PDF_DOWNLOAD_TIMEOUT_SECONDS

    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    ]
    ua = random.choice(user_agents)
    pdf_headers, html_headers = build_headers(ua)

    try:
        # --- Cache Check ---
        if USE_DOWNLOAD_CACHE and save_path.exists() and save_path.stat().st_size > 1000:
            logger.info("PDF already exists in cache, skipping download: %s", save_path)
            return True

        # --- New Download Check ---
        if not ALLOW_NEW_DOWNLOADS:
            logger.info("New downloads disabled via config, skipping: %s", url)
            return False

        # Normalize URL and derive related endpoints
        clean_url = _strip_tracking_params(url)
        p = urlparse(clean_url)
        origin = f"{p.scheme}://{p.netloc}"
        site_root = f"{origin}/"
        dir_url = clean_url.rsplit("/", 1)[0] + "/"
        listing_url = f"{origin}/csrreports/"  # harmless if 404

        # --- SSRF guard ---
        if not _is_safe_url(clean_url):
            logger.warning("Blocked private/internal URL: %s", url)
            return False

        # --- Size pre-check (one HEAD before any GETs) ---
        _check_content_length(sess, clean_url, pdf_headers, timeout)

        part_path = save_path.with_suffix(save_path.suffix + ".part")

        def finalize_download(content: bytes):
            part_path.write_bytes(content)
            part_path.replace(save_path)

        # --- Attempt 1: direct fetch ---
        logger.info("Download attempt 1 (direct): %s", clean_url)
        hdrs = with_headers(pdf_headers, Range="bytes=0-")
        r = sess.get(clean_url, headers=hdrs, timeout=timeout, allow_redirects=True,
                     verify=True, stream=True)

        if r.ok and _is_pdf_response(r):
            finalize_download(_read_response_body(r))
            logger.info("Downloaded PDF (direct): %s -> %s", clean_url, save_path)
            return True
        else:
            r.close()
            logger.debug(
                "Attempt 1 not PDF/OK: status=%s, ctype=%s",
                getattr(r, "status_code", None),
                r.headers.get("Content-Type", ""),
            )

        # --- Attempt 2: parse HTML/viewer for real PDF link ---
        if ("text/html" in (r.headers.get("Content-Type", "") or "").lower()) or (
            getattr(r, "status_code", 0) in (403, 451, 503)
        ):
            logger.info("Attempt 2: parsing HTML/viewer for PDF: %s", clean_url)
            html_resp = sess.get(
                clean_url,
                headers=dict(html_headers),
                timeout=timeout,
                allow_redirects=True,
                verify=True,
            )
            if "text/html" in (html_resp.headers.get("Content-Type", "") or "").lower() and (
                html_resp.text or ""
            ):
                pdf_url = _find_pdf_link_in_html(html_resp.text, base_url=html_resp.url)
                if pdf_url:
                    if not _is_safe_url(pdf_url):
                        logger.warning("HTML fallback URL blocked (SSRF): %s", pdf_url)
                    else:
                        hdrs2 = with_headers(pdf_headers, Referer=html_resp.url, Range="bytes=0-")
                        logger.info("Found PDF link on page: %s", pdf_url)
                        pdf_resp = sess.get(
                            pdf_url, headers=hdrs2, timeout=timeout, allow_redirects=True,
                            verify=True, stream=True,
                        )
                        if pdf_resp.ok and _is_pdf_response(pdf_resp):
                            finalize_download(_read_response_body(pdf_resp))
                            logger.info("Downloaded PDF (parsed link): %s -> %s", pdf_url, save_path)
                            return True
                        else:
                            pdf_resp.close()
                            logger.debug(
                                "Parsed link not PDF/OK: status=%s, ctype=%s",
                                getattr(pdf_resp, "status_code", None),
                                pdf_resp.headers.get("Content-Type", ""),
                            )

        # --- Attempt 3: WAF warm-up then retry ---
        logger.info("Attempt 3: WAF warm-up then retry")
        for warm_url in (site_root, listing_url, dir_url):
            try:
                sess.get(
                    warm_url,
                    headers=dict(html_headers),
                    timeout=timeout,
                    allow_redirects=True,
                    verify=True,
                )
                time.sleep(random.uniform(0.3, 1.1))
            except requests.RequestException:
                logger.debug("Warm-up fetch failed (ignored): %s", warm_url)

        hdrs3 = with_headers(pdf_headers, Referer=listing_url, Range="bytes=0-")
        logger.info("Retry after warm-up: %s", clean_url)
        r2 = sess.get(clean_url, headers=hdrs3, timeout=timeout, allow_redirects=True,
                      verify=True, stream=True)
        if r2.ok and _is_pdf_response(r2):
            finalize_download(_read_response_body(r2))
            logger.info("Downloaded PDF (after warm-up): %s -> %s", clean_url, save_path)
            return True
        r2.close()

        logger.warning(
            "All attempts failed: %s (status=%s / %s)",
            clean_url,
            getattr(r2, "status_code", None),
            r2.headers.get("Content-Type", ""),
        )
        return False

    except FileTooLargeError:
        logger.warning("PDF too large, skipping: %s", url)
        return False
    except requests.exceptions.Timeout:
        logger.warning("Download timed out: %s", url)
        return False
    except requests.RequestException as e:
        logger.error("Request error during download: %s | %s", url, e)
        return False
    except Exception:
        logger.exception("Unexpected error during download: %s", url)
        return False


# ---- page scraping ----------------------------------------------------------


def scrape_report_page_for_pdfs(
    url: str,
    city_name_for_filename: str,  # kept for signature parity; not used here
    country: str,  # kept for signature parity; not used here
    *,
    visited: Optional[set[str]] = None,
    sess: Optional[requests.Session] = None,
    timeout: int = 20,
) -> list[str]:
    """
    Scrape an HTML page and extract candidate PDF links (.pdf endings or via HEAD Content-Type).
    Returns a de-duplicated list of absolute URLs.
    """
    sess = sess or requests.Session()
    visited = visited or set()

    if url in visited:
        logger.debug("Skipping already visited page: %s", url)
        return []
    visited.add(url)

    potential_pdf_links: list[str] = []

    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36 Edg/136.0.0.0",
    ]
    headers = {"User-Agent": random.choice(user_agents)}

    try:
        r = sess.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()

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
                    head_headers = {"User-Agent": random.choice(user_agents)}
                    h = sess.head(full_url, headers=head_headers, timeout=5, allow_redirects=True)
                    h.raise_for_status()
                    ctype = (h.headers.get("Content-Type") or "").lower()
                    if "application/pdf" in ctype:
                        potential_pdf_links.append(full_url)
                except requests.RequestException:
                    pass  # ignore link errors

    except requests.RequestException as e:
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


def _is_pdf_response(resp) -> bool:
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "application/pdf" in ctype:
        return True
    if "application/octet-stream" in ctype:
        cd = (resp.headers.get("Content-Disposition") or "").lower()
        if ".pdf" in cd or "filename=" in cd:
            return True
    if (resp.url or "").lower().endswith(".pdf"):
        return True
    return False


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
