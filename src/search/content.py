"""Webpage content fetching with caching, PDF extraction, and summarization helpers."""

import copy
import io
import ipaddress
import json
import os
import re
import logging
import socket
import typing
from datetime import datetime, timedelta
from typing import List
from urllib.parse import urljoin, urlparse

import httpcore
import httpx
from bs4 import BeautifulSoup

from .analytics import RateLimitError, error_logger
from .cache import (
    CONTENT_CACHE_DIR,
    content_cache_index,
    generate_cache_key,
    cleanup_cache,
)

logger = logging.getLogger(__name__)

_PRIVATE_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _is_private_address(addr: ipaddress._BaseAddress) -> bool:
    return any(addr in net for net in _PRIVATE_NETWORKS) or addr.is_private or addr.is_loopback


def _resolve_hostname_ips(hostname: str) -> List[ipaddress._BaseAddress]:
    ips = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(hostname, None):
        if family in (socket.AF_INET, socket.AF_INET6):
            ips.append(ipaddress.ip_address(sockaddr[0]))
    return ips


def _public_http_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "metadata.google.internal", "metadata"):
        return False
    try:
        return not _is_private_address(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        ips = _resolve_hostname_ips(host)
    except OSError:
        return False
    # Fail closed: a hostname that resolves to nothing is treated as
    # non-public (an empty all(...) would otherwise return True).
    return bool(ips) and all(not _is_private_address(ip) for ip in ips)


def _resolve_public_ips(url: str) -> List[ipaddress._BaseAddress]:
    """Resolve a URL's hostname to public IPs.

    Rejects private/metadata targets. Called on every redirect hop,
    not just the first URL, so a redirect chain can't pivot to an
    internal host after the initial check.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise httpx.RequestError(f"Blocked non-HTTP(S) URL: {url}")
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "metadata.google.internal", "metadata"):
        raise httpx.RequestError(f"Blocked metadata hostname: {host}")
    try:
        ip = ipaddress.ip_address(host)
        if _is_private_address(ip):
            raise httpx.RequestError(f"Blocked private IP: {host}")
        return [ip]
    except httpx.RequestError:
        raise
    except ValueError:
        pass
    ips = _resolve_hostname_ips(host)
    if not ips or any(_is_private_address(ip) for ip in ips):
        raise httpx.RequestError(f"Blocked non-public URL: {url}")
    return ips


class _PinnedBackend(httpcore.NetworkBackend):
    """Overrides connect_tcp to connect to a pre-resolved IP instead
    of performing DNS lookup.

    TLS/SNI and the Host header remain untouched — httpcore derives
    those from the original URL's origin, not from connect_tcp's host
    argument. Only the TCP destination is pinned.
    """

    def __init__(self, ip: ipaddress._BaseAddress):
        self._ip = str(ip)
        self._real = httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Any = None,
    ):
        return self._real.connect_tcp(
            self._ip, port, timeout, local_address, socket_options,
        )

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: typing.Any = None,
    ):
        return self._real.connect_unix_socket(path, timeout, socket_options)

    def sleep(self, seconds: float) -> None:
        return self._real.sleep(seconds)


class _PinnedTransport(httpx.HTTPTransport):
    """HTTPTransport that pins every TCP connection to a given IP.

    After the parent builds a standard ConnectionPool, we replace it
    with an identical pool that uses _PinnedBackend as its
    NetworkBackend. TLS/SNI and Host headers are derived from the URL
    by httpcore, not by connect_tcp, so they stay correct.
    """

    def __init__(self, ip: ipaddress._BaseAddress, **kw):
        super().__init__(**kw)
        orig = self._pool
        self._pool = httpcore.ConnectionPool(
            ssl_context=orig._ssl_context,
            max_connections=orig._max_connections,
            max_keepalive_connections=orig._max_keepalive_connections,
            keepalive_expiry=orig._keepalive_expiry,
            http1=orig._http1,
            http2=orig._http2,
            network_backend=_PinnedBackend(ip),
        )
        orig.close()


def _get_public_url(url: str, *, headers: dict, timeout: int) -> httpx.Response:
    current = url
    for _ in range(8):
        ips = _resolve_public_ips(current)
        with httpx.Client(
            headers=headers, timeout=timeout,
            follow_redirects=False,
            transport=_PinnedTransport(ips[0]),
        ) as client:
            response = client.get(current)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response
        location = response.headers.get("location")
        if not location:
            return response
        current = urljoin(current, location)
    raise httpx.RequestError("Too many redirects")

# PDF extraction (optional dependency)
try:
    from pdfminer.high_level import extract_text as pdf_extract_text
except ImportError:
    pdf_extract_text = None  # type: ignore


# ----------------------------------------------------------------------
# HTML extraction helpers
# ----------------------------------------------------------------------
def _extract_meta(soup: BeautifulSoup) -> dict:
    """Pull meta description and keywords if present."""
    description = ""
    keywords = ""
    desc_tag = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if desc_tag and desc_tag.get("content"):
        description = desc_tag["content"].strip()
    kw_tag = soup.find("meta", attrs={"name": re.compile("keywords", re.I)})
    if kw_tag and kw_tag.get("content"):
        keywords = kw_tag["content"].strip()
    return {"description": description, "keywords": keywords}


def _extract_og_image(soup: BeautifulSoup) -> str:
    """Extract the best representative image URL from meta tags.

    Only returns absolute http(s) URLs — skips relative paths and data URIs.
    """
    candidates = []
    # Open Graph image (most reliable)
    for prop in ("og:image", "og:image:url", "og:image:secure_url"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content", "").strip():
            candidates.append(tag["content"].strip())
    # Twitter card image
    tag = soup.find("meta", attrs={"name": "twitter:image"})
    if tag and tag.get("content", "").strip():
        candidates.append(tag["content"].strip())
    # Thumbnail meta
    tag = soup.find("meta", attrs={"name": "thumbnail"})
    if tag and tag.get("content", "").strip():
        candidates.append(tag["content"].strip())
    # Return first absolute https URL
    for url in candidates:
        if url.startswith("https://") and not url.endswith((".svg", ".ico")):
            return url
    return ""


def _extract_lists(soup: BeautifulSoup) -> List[List[str]]:
    """Return a list of lists, each inner list representing a <ul>/<ol>."""
    all_lists = []
    for lst in soup.find_all(["ul", "ol"]):
        items = [li.get_text(separator=" ", strip=True) for li in lst.find_all("li")]
        if items:
            all_lists.append(items)
    return all_lists


def _extract_tables(soup: BeautifulSoup) -> List[List[List[str]]]:
    """Return a list of tables, each table is a list of rows, each row a list of cell texts."""
    tables_data = []
    for table in soup.find_all("table"):
        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(separator=" ", strip=True) for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables_data.append(rows)
    return tables_data


def _extract_code_blocks(soup: BeautifulSoup) -> List[str]:
    """Collect text from <pre> and <code> blocks."""
    blocks = []
    for tag in soup.find_all(["pre", "code"]):
        txt = tag.get_text(separator=" ", strip=True)
        if txt:
            blocks.append(txt)
    return blocks


def _detect_js_frameworks(soup: BeautifulSoup) -> bool:
    """Very naive detection of common JS frameworks."""
    js_indicators = [
        "react", "angular", "vue", "svelte", "next", "nuxt",
        "ember", "backbone", "jquery", "polymer", "mithril",
    ]
    for script in soup.find_all("script"):
        src = script.get("src", "").lower()
        if any(fr in src for fr in js_indicators):
            return True
        if script.string:
            content = script.string.lower()
            if any(fr in content for fr in js_indicators):
                return True
    if soup.find(attrs={"data-reactroot": True}) or soup.find(attrs={"ng-app": True}):
        return True
    return False


def _empty_result(url: str, error: str = "") -> dict:
    """Build a standard failure result dict."""
    return {
        "url": url,
        "title": "",
        "content": "",
        "lists": [],
        "tables": [],
        "code_blocks": [],
        "meta_description": "",
        "meta_keywords": "",
        "js_rendered": False,
        "js_message": "",
        "success": False,
        "error": error,
    }


# ----------------------------------------------------------------------
# Main content fetcher
# ----------------------------------------------------------------------
def fetch_webpage_content(url: str, timeout: int = 5, retry_attempt: int = 0) -> dict:
    """Fetch and extract meaningful content from a webpage with caching."""
    cache_key = generate_cache_key(url)
    cache_file = CONTENT_CACHE_DIR / f"{cache_key}.cache"

    # Check cache
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cached_data = json.load(f)
            timestamp = datetime.fromisoformat(cached_data["timestamp"])
            if datetime.now() - timestamp < timedelta(hours=2):
                logger.debug(f"Content cache hit for URL: {url}")
                return cached_data["data"]
            else:
                cache_file.unlink(missing_ok=True)
                content_cache_index.pop(cache_key, None)
        except Exception as e:
            logger.warning(f"Failed to read content cache for {url}: {e}")
            cache_file.unlink(missing_ok=True)
            content_cache_index.pop(cache_key, None)

    # Fetch
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
        }
        response = _get_public_url(url, headers=headers, timeout=timeout)

        if response.status_code == 429:
            raise RateLimitError(f"Rate limit hit for {url} (attempt {retry_attempt})")

        response.raise_for_status()
    except httpx.RequestError as e:
        error_logger.error(f"NetworkError fetching {url} (attempt {retry_attempt}): {e}")
        return _empty_result(url, f"NetworkError: {e}")
    except RateLimitError as e:
        error_logger.error(str(e))
        return _empty_result(url, str(e))

    # PDF handling
    content_type = response.headers.get("Content-Type", "").lower()
    if "application/pdf" in content_type or url.lower().endswith(".pdf"):
        if pdf_extract_text is None:
            logger.error("pdfminer.six is not installed; cannot extract PDF text.")
            pdf_text = ""
        else:
            try:
                pdf_bytes = io.BytesIO(response.content)
                pdf_text = pdf_extract_text(pdf_bytes)
            except Exception as e:
                logger.warning(f"PDF extraction failed for {url}: {e}")
                pdf_text = ""
        result = {
            "url": url,
            "title": os.path.basename(url),
            "content": pdf_text,
            "lists": [],
            "tables": [],
            "code_blocks": [],
            "meta_description": "",
            "meta_keywords": "",
            "js_rendered": False,
            "js_message": "",
            "success": bool(pdf_text),
            "error": "" if pdf_text else "Failed to extract PDF text",
        }
        _cache_result(cache_file, cache_key, result, url)
        return result

    # HTML handling
    try:
        soup = BeautifulSoup(response.text, "html.parser")
    except Exception as e:
        error_logger.error(f"ParseError parsing HTML from {url} (attempt {retry_attempt}): {e}")
        result = _empty_result(url, f"ParseError: {e}")
        _cache_result(cache_file, cache_key, result, url)
        return result

    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    meta_info = _extract_meta(soup)
    og_image = _extract_og_image(soup)
    js_rendered = _detect_js_frameworks(soup)
    js_message = "Page appears to be rendered by a JavaScript framework; content may be incomplete." if js_rendered else ""

    # Main textual content (heuristic): prefer semantic / "content"-classed
    # containers to skip nav/footer/boilerplate; tuned for article pages.
    main_content = ""
    content_areas = soup.find_all(
        ["main", "article", "section", "div"],
        class_=re.compile("content|main|body|article|post|entry|text", re.I),
    )
    if content_areas:
        for area in content_areas[:3]:
            main_content += area.get_text(separator=" ", strip=True) + " "
    main_content = re.sub(r"\s+", " ", main_content).strip()

    # The class heuristic can latch onto a small wrapper and miss the real
    # content (app/landing pages, or SSR sites whose body isn't in a
    # "content"-classed div, so these came back nearly empty before). When the
    # heuristic returns nothing OR suspiciously little, fall back to the full
    # <body>, stripping scripts/styles (so JSON/JS doesn't leak into the text)
    # plus nav/header/footer/aside (boilerplate), and keep whichever yields
    # more readable text.
    THIN_CONTENT_CHARS = 600  # below this the heuristic likely missed the page
    if len(main_content) < THIN_CONTENT_CHARS:
        body = soup.find("body")
        if body:
            # Strip from a copy so the later list/table/code extractors still
            # see the original soup unmodified.
            body_copy = copy.copy(body)
            for _noise in body_copy.find_all(
                ["script", "style", "noscript", "template", "nav", "header", "footer", "aside"]
            ):
                _noise.extract()
            body_text = re.sub(r"\s+", " ", body_copy.get_text(separator=" ", strip=True)).strip()
            if len(body_text) > len(main_content):
                main_content = body_text

    result = {
        "url": url,
        "title": title_text,
        "content": main_content,
        "lists": _extract_lists(soup),
        "tables": _extract_tables(soup),
        "code_blocks": _extract_code_blocks(soup),
        "meta_description": meta_info.get("description", ""),
        "meta_keywords": meta_info.get("keywords", ""),
        "og_image": og_image,
        "js_rendered": js_rendered,
        "js_message": js_message,
        "success": True,
        "error": "",
    }
    _cache_result(cache_file, cache_key, result, url)
    return result


def _cache_result(cache_file, cache_key: str, result: dict, url: str):
    """Write a result to the content cache."""
    try:
        cache_data = {"timestamp": datetime.now().isoformat(), "data": result}
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(cache_data, f)
        content_cache_index[cache_key] = datetime.now()
        cleanup_cache(CONTENT_CACHE_DIR, content_cache_index, timedelta(hours=2))
    except Exception as e:
        logger.warning(f"Failed to write content cache for {url}: {e}")


# ----------------------------------------------------------------------
# Content summarization helpers
# ----------------------------------------------------------------------
def extract_key_points(text: str) -> List[str]:
    """Pull out bullet-style key points from a block of text."""
    points: List[str] = []
    bullet_pat = re.compile(r"^\s*[-*•]\s+(.*)")
    numbered_pat = re.compile(r"^\s*\d+[\.\)]\s+(.*)")
    for line in text.splitlines():
        m = bullet_pat.match(line) or numbered_pat.match(line)
        if m:
            points.append(m.group(1).strip())
    return points


def get_tldr(text: str, max_sentences: int = 3) -> str:
    """Produce a very short TL;DR by taking the first few sentences."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    selected = [s.strip() for s in sentences if s][:max_sentences]
    return " ".join(selected)


def extract_quotes(text: str) -> List[str]:
    """Return quoted excerpts that are at least 15 characters long."""
    return [m.group(1).strip() for m in re.finditer(r'["\']([^"\']{15,}?)["\']', text)]


def extract_statistics(text: str) -> List[str]:
    """Find numbers, percentages, dates and simple measurements."""
    pattern = re.compile(
        r"\b\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(%|percent|‰|per cent|[a-zA-Z]+)?\b",
        re.IGNORECASE,
    )
    return [m.group(0).strip() for m in pattern.finditer(text)]
