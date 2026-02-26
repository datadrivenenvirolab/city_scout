# utils.py
from __future__ import annotations

import datetime as _dt
import re
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlparse

from .config import (
    ACCEPTABLE_REPORT_TYPES,
    BAD_KEYWORDS,
    GOOD_KEYWORDS,
    MIN_ACCEPTABLE_REPORT_YEAR,
    YEAR_REGEX,
)

# Compile the year regex from config
YEAR_RE = re.compile(YEAR_REGEX, re.I)

# City suffixes to ignore when matching names inside URLs/snippets
_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|gmbh|sa|nv|ag|pl|plc|spa|pte|s\.a\.|s\.p\.a\.)\.?\b",
    re.I,
)


def _type_synonyms() -> Dict[str, list[str]]:
    acc = [t.lower() for t in ACCEPTABLE_REPORT_TYPES]
    syn = {
        "annual": [
            r"\bannual\b",
            r"\bform\s*10-k\b",
            r"\b20-f\b",
            # zh / ja
            r"年度报告",
            r"年報",
            r"年次報告書",
            r"アニュアルレポート",
            r"有価証券報告書",
        ],
        "integrated": [
            r"\bintegrated\b",
            r"综合报告",
            r"綜合報告",
            r"整合报告",
            r"整合報告",
            r"統合報告書",
            r"統合報告",
        ],
        "sustainability": [
            r"\bsustainability\b",
            r"\bnon[-\s]?financial\b",
            r"\bnonfinancial\b",
            r"可持续发展报告",
            r"可持續發展報告",
            r"サステナビリティレポート",
            r"サステナビリティ報告書",
        ],
        "csr": [
            r"\bcorporate\s+social\s+responsibility\b",
            r"\bsocial\s+responsibility\b",
            r"\bcsr\b",
            r"企业社会责任报告",
            r"企業社會責任報告",
            r"CSR報告書",
        ],
        "esg": [
            r"\besg\b",
            r"environmental\s*,?\s*social\s*(?:and|&)\s*governance",
            r"ESG报告",
            r"ESG報告",
            r"ESGレポート",
        ],
        "impact": [r"\bimpact\b", r"影响报告", r"影響報告", r"インパクトレポート"],
        "cdp": [r"\bcdp\b", r"\bcarbon\s+disclosure\b", r"CDP报告", r"CDP報告", r"CDP\s*回答"],
    }
    return {k: v for k, v in syn.items() if k in acc}


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def _path(url: str) -> str:
    try:
        return (urlparse(url).path or "").lower()
    except Exception:
        return ""


def _tokens(s: str) -> set[str]:
    s = (s or "").lower()
    toks = re.split(r"[^a-z0-9]+", s)
    return {t for t in toks if t}


def _normalize_city_name(name: str) -> str:
    """Drop common suffixes to make domain/name matching less brittle."""
    n = (name or "").lower()
    n = _SUFFIX_RE.sub("", n)
    return re.sub(r"\s+", " ", n).strip()


def score_link(
    url: str,
    anchor_text: Optional[str],
    city: Optional[str],
    *,
    base_domain: Optional[str] = None,
    min_year: Optional[int] = MIN_ACCEPTABLE_REPORT_YEAR,
) -> float:
    """
    Heuristic score for a candidate link. Higher is better.
    Considers: file-type, keywords, report-type synonyms, year hints, domain cues, city mentions, and path depth.
    """
    if not url:
        return 0.0

    url_l = url.lower()
    path = _path(url)
    dom = _domain(url)
    anc = (anchor_text or "").lower()
    comp = _normalize_city_name(city or "")

    score = 0.0

    # 1) File-type priority
    if path.endswith(".pdf") or ".pdf" in path:
        score += 80
    elif path.endswith((".ashx", ".aspx", ".php")) and (
        "download" in url_l or "attachment" in url_l
    ):
        score += 35

    # 2) Good / bad keywords in URL or anchor
    toks = _tokens(path + " " + anc)
    score += 10 * sum(1 for k in GOOD_KEYWORDS if k in toks)
    score -= 12 * sum(1 for k in BAD_KEYWORDS if k in toks)

    # 3) ACCEPTABLE_REPORT_TYPES (and synonyms) present?
    syn = _type_synonyms()
    acceptable_l = [t.lower() for t in ACCEPTABLE_REPORT_TYPES]
    for idx, canon in enumerate(acceptable_l):
        boost = max(5, 30 - idx)  # earlier in list = higher priority
        if canon in toks:
            score += boost
        for pat in syn.get(canon, []):
            if re.search(pat, url_l) or re.search(pat, anc):
                score += boost

    # 4) Year proximity (recent years boost)
    y_boost = 0
    years = [int(m.group(0)) for m in YEAR_RE.finditer(url_l + " " + anc)]
    if years:
        y = max(years)
        cur = _dt.datetime.now().year
        if 2000 <= y <= cur:
            base = min_year if isinstance(min_year, int) else max(cur - 2, 2017)
            y_boost = max(0, (y - base)) * 2  # 2 pts per year above base
    score += y_boost

    # 5) Same-domain + path cues
    if base_domain and dom.endswith(base_domain.lower()):
        score += 8
    if any(
        seg in path
        for seg in (
            "/sustainab",
            "/esg",
            "/csr",
            "/report",
            "/reports",
            "/responsib",
            "/citizenship",
            "/non-financial",
            "/nonfinancial",
        )
    ):
        score += 12

    # 6) City mention (light boost)
    if comp:
        if comp in url_l or comp in anc:
            score += 6

    # 7) Shorter, cleaner paths slightly preferred
    depth = path.count("/")
    score += max(0, 6 - depth)

    return score


def select_top_links(
    candidates: Iterable[str | Dict[str, Any]],
    city: str,
    *,
    top_k_per_domain: int = 25,
    max_total: int = 120,
    min_year: Optional[int] = MIN_ACCEPTABLE_REPORT_YEAR,
    base_domain: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Normalize candidate links, score them, keep top-K per domain, then global cap.
    Returns a list of dicts like {"url": ..., "anchor": ..., "source": ..., "_score": ...}
    """
    # Normalize input to dicts
    norm: List[Dict[str, Any]] = []
    for c in candidates:
        if isinstance(c, str):
            norm.append({"url": c, "anchor": None, "source": None})
        else:
            norm.append(
                {
                    "url": c.get("url"),
                    "anchor": c.get("anchor"),
                    "source": c.get("source"),
                }
            )

    # Determine base domain if not provided
    first_url = next((n["url"] for n in norm if n.get("url")), "")
    base_dom = (base_domain or _domain(first_url)).lower()

    # Score all
    for item in norm:
        u = item.get("url")
        if not u:
            item["_score"] = 0.0
            continue
        item["_score"] = score_link(
            u, item.get("anchor"), city, base_domain=base_dom, min_year=min_year
        )

    # Group by domain
    from collections import defaultdict

    by_dom: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in norm:
        d = _domain(item.get("url") or "")
        by_dom[d].append(item)

    # Trim per domain
    trimmed: List[Dict[str, Any]] = []
    for d, items in by_dom.items():
        items.sort(key=lambda x: x["_score"], reverse=True)
        trimmed.extend(items[:top_k_per_domain])

    # Global cap
    trimmed.sort(key=lambda x: x["_score"], reverse=True)
    selected = trimmed[:max_total]

    # De-duplicate by normalized URL (remove fragment)
    seen = set()
    final: List[Dict[str, Any]] = []
    for it in selected:
        u = (it.get("url") or "").split("#")[0]
        if not u or u in seen:
            continue
        seen.add(u)
        final.append(it)

    return final
