import re
import requests

JINA_BASE = "https://r.jina.ai"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
_JINA_RENDER_TIMEOUT_SECONDS = 180
_JINA_REQUEST_TIMEOUT_SECONDS = 200
_JINA_CACHE_FALLBACK_TIMEOUT_SECONDS = 30
_FIRECRAWL_RENDER_TIMEOUT_MS = 120000
_FIRECRAWL_REQUEST_TIMEOUT_SECONDS = 135

# Elements to strip server-side via Jina's X-Remove-Selector.
# More reliable than X-Target-Selector because we remove known noise
# rather than trying to guess content class names per platform.
_REMOVE_SELECTOR = ", ".join([
    "nav", "header", "footer", "aside",
    "#cart", ".cart", "[class*='cart']",
    "#header", "#footer", "#nav", "#sidebar",
    "[class*='sidebar']", "[class*='navigation']",
    "[class*='breadcrumb']", "[class*='cookie']",
    "[class*='popup']", "[class*='modal']",
    "[class*='newsletter']", "[class*='subscribe']",
    "[class*='related']", "[class*='recommended']",
    "[class*='upsell']", "[class*='cross-sell']",
    "form", "script", "style", "noscript", "iframe",
])

# Collection pages keep sidebars and filter panels — strip less aggressively
_COLLECTION_REMOVE_SELECTOR = ", ".join([
    "nav", "header", "footer",
    "#cart", ".cart", "[class*='cart']",
    "#header", "#footer", "#nav",
    "[class*='navigation']", "[class*='breadcrumb']", "[class*='cookie']",
    "[class*='popup']", "[class*='modal']",
    "[class*='newsletter']", "[class*='subscribe']",
    "[class*='related']", "[class*='recommended']",
    "[class*='upsell']", "[class*='cross-sell']",
    "script", "style", "noscript", "iframe",
])

# Lines that are almost certainly noise regardless of platform
_NOISE_LINE_PATTERNS = re.compile(
    r"^\s*("
    r"\$[\d,.]+|"                          # prices
    r"Add to cart|Sold out|Sale price|"
    r"Regular price|Unit price|"
    r"Quantity must be|Adding product|"
    r"Please allow \d|"
    r"Pickup available|Usually ready|"
    r"Check availability|Service Center|"
    r"Skip to content|Log in|Sign in|"
    r"Search$|Menu$|Close$|"
    r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}"  # phone numbers
    r")\s*$",
    re.IGNORECASE
)

_COLLECTION_NOISE_LINE_PATTERNS = re.compile(
    r"^\s*("
    r"Add to cart|Sold out|Sale price|Regular price|Unit price|"
    r"Quantity must be|Adding product|"
    r"Please allow \d|"
    r"Pickup available|Usually ready|"
    r"Check availability|Service Center|"
    r"Skip to content|Log in|Sign in|"
    r"Search$|Menu$|Close$|Footer$|"
    r"\+?1?[\s\-.]?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}"
    r")\s*$",
    re.IGNORECASE
)

_PRICE_RE = re.compile(r"(?:[$£€]\s?\d[\d,.]*(?:\.\d{2})?|\d[\d,.]*(?:\.\d{2})?\s?(?:USD|GBP|EUR))")
_PRODUCT_LINK_RE = re.compile(r"^\s*#{0,4}\s*(?:[-*]\s*)?\[(?P<name>[^\]]{3,})\]\(https?://[^\)]+\)\s*$")
_FILTER_LABELS = {
    "brand", "brands", "size", "sizes", "color", "colour", "colors", "colours",
    "price", "material", "materials", "style", "styles", "type", "types",
    "category", "categories", "availability", "product type", "fit", "capacity",
    "flavor", "flavour", "weight", "finish", "features",
}


def is_ecommerce_collection_page(business_type: str, page_type: str) -> bool:
    """Return True when the row should use collection/product-grid scraping."""
    if (business_type or "").strip().lower() != "ecommerce":
        return False
    page_type_norm = (page_type or "").strip().lower()
    return "category" in page_type_norm or "collection" in page_type_norm


def _score_paragraph(para: str) -> float:
    """Score a paragraph by content quality. Higher = more substantive.

    Penalises: short lines, link-heavy lines, price/button text.
    Rewards: sentence-like text with multiple words.
    """
    words = para.split()
    if len(words) < 8:
        return 0.0

    # Penalise link density
    link_count = len(re.findall(r"\[.+?\]\(https?://", para))
    if link_count > 2:
        return 0.0

    # Penalise lines that are mostly numbers or special chars
    alpha_ratio = sum(c.isalpha() for c in para) / max(len(para), 1)
    if alpha_ratio < 0.5:
        return 0.0

    return len(words) * alpha_ratio


def _extract_title(text: str) -> str:
    title_match = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
    return title_match.group(1).strip() if title_match else ""


def _normalise_lines(text: str, noise_pattern: re.Pattern) -> list:
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or noise_pattern.match(line):
            continue
        lines.append(line)
    return lines


def _extract_collection_products(lines: list, limit: int = 30) -> list:
    products = []
    for idx, line in enumerate(lines):
        match = _PRODUCT_LINK_RE.match(line)
        if not match:
            continue
        name = re.sub(r"\s+", " ", match.group("name")).strip()
        price = ""
        for follow in lines[idx + 1: idx + 5]:
            price_match = _PRICE_RE.search(follow)
            if price_match:
                price = price_match.group(0).strip()
                break
            if _PRODUCT_LINK_RE.match(follow):
                break
        if name and not any(p["name"].lower() == name.lower() for p in products):
            products.append({"name": name, "price": price})
        if len(products) >= limit:
            break
    return products


def _extract_collection_filters(lines: list) -> dict:
    filters = {}
    current = None
    seen_filter_anchor = False

    for line in lines:
        clean = re.sub(r"^#+\s*", "", line).strip()
        clean = re.sub(r"^\*\s*", "", clean).strip()
        clean_l = clean.lower().rstrip(":")

        if clean_l == "filters":
            seen_filter_anchor = True
            current = None
            continue
        if _PRODUCT_LINK_RE.match(line):
            current = None
            continue
        if clean_l in _FILTER_LABELS:
            seen_filter_anchor = True
            current = clean.rstrip(":")
            filters.setdefault(current, [])
            continue
        if not seen_filter_anchor or not current:
            continue
        if _PRICE_RE.search(clean) and current.lower() != "price":
            continue
        if len(clean) > 40:
            current = None
            continue
        if clean and clean not in filters[current]:
            filters[current].append(clean)

    return {k: v[:12] for k, v in filters.items() if v}


def _build_collection_context(text: str, max_chars: int) -> tuple:
    title = _extract_title(text)
    lines = _normalise_lines(text, _COLLECTION_NOISE_LINE_PATTERNS)
    products = _extract_collection_products(lines)
    filters = _extract_collection_filters(lines)

    excerpt_text = "\n".join(lines)
    excerpt_text = re.sub(r"^\s*\*\s+\[.+?\]\(https?://.+?\)\s*$", "", excerpt_text, flags=re.MULTILINE)
    excerpt_text = re.sub(r"^#{1,4}\s+\[.+?\]\(https?://.+?\)\s*$", "", excerpt_text, flags=re.MULTILINE)
    excerpt_text = re.sub(r"\n{3,}", "\n\n", excerpt_text).strip()

    paragraphs = re.split(r"\n{2,}", excerpt_text)
    excerpt_parts = []
    chars_used = 0
    for para in paragraphs:
        if chars_used >= max_chars // 2:
            break
        if _score_paragraph(para) > 0 or para.strip().startswith("#"):
            excerpt_parts.append(para)
            chars_used += len(para)

    sections = ["COLLECTION CONTEXT"]
    if products:
        sections.append(
            "Products found:\n" + "\n".join(
                f"- {p['name']} | {p['price']}" if p["price"] else f"- {p['name']}"
                for p in products
            )
        )
    if filters:
        sections.append(
            "Filters found:\n" + "\n".join(
                f"- {name}: {', '.join(values)}"
                for name, values in filters.items()
            )
        )
    if excerpt_parts:
        sections.append("Page excerpt:\n" + "\n\n".join(excerpt_parts))

    content = "\n\n".join(sections).strip()
    if len(content) > max_chars:
        content = content[:max_chars].strip()
    return content, title


def scrape_page_context(
    api_key: str,
    url: str,
    max_chars: int = 10000,
    mode: str = "default",
    _cached: bool = False,
) -> dict:
    """Scrape a page via Jina Reader and return cleaned topic context.

    Strategy:
    1. Use X-Remove-Selector to strip nav/cart/footer server-side
    2. Post-process: score each paragraph by content density
    3. Keep highest-scoring paragraphs up to max_chars
    4. Works on any platform — no CSS class guessing required

    Returns:
        {"content": str, "title": str, "success": bool, "error": str}
    """
    if not url:
        return {"content": "", "title": "", "success": False, "error": "No URL provided"}

    remove_selector = _COLLECTION_REMOVE_SELECTOR if mode == "ecommerce_collection" else _REMOVE_SELECTOR
    headers = {
        "Accept": "text/plain",
        "X-Return-Format": "markdown",
        "X-With-Links-Summary": "false",
        "X-With-Images-Summary": "false",
    }
    if not _cached:
        headers.update({
            "X-Remove-Selector": remove_selector,
            "X-No-Cache": "true",
            "X-Timeout": str(_JINA_RENDER_TIMEOUT_SECONDS),
        })
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        request_timeout = _JINA_CACHE_FALLBACK_TIMEOUT_SECONDS if _cached else _JINA_REQUEST_TIMEOUT_SECONDS
        resp = requests.get(f"{JINA_BASE}/{url}", headers=headers, timeout=request_timeout)

        # If remove selector causes issues, retry clean
        if resp.status_code in (422, 400):
            headers.pop("X-Remove-Selector", None)
            resp = requests.get(f"{JINA_BASE}/{url}", headers=headers, timeout=request_timeout)

        if not _cached and 500 <= resp.status_code < 600:
            return scrape_page_context(api_key, url, max_chars=max_chars, mode=mode, _cached=True)

        resp.raise_for_status()
        text = resp.text.strip()

        if not text and not _cached:
            return scrape_page_context(api_key, url, max_chars=max_chars, mode=mode, _cached=True)
        if not text:
            return {"content": "", "title": "", "success": False,
                    "error": "Jina returned empty content after cached fallback", "source": "cached_fallback"}

        if mode == "ecommerce_collection":
            content, title = _build_collection_context(text, max_chars)
            if (not content or content == "COLLECTION CONTEXT") and not _cached:
                return scrape_page_context(api_key, url, max_chars=max_chars, mode=mode, _cached=True)
            if not content or content == "COLLECTION CONTEXT":
                return {"content": "", "title": title, "success": False,
                        "error": "No collection products, filters, or content found after cached fallback",
                        "source": "cached_fallback"}
            return {"content": content, "title": title, "success": True, "error": "",
                    "source": "cached_fallback" if _cached else "live"}

        # Extract title from Jina metadata block
        title = _extract_title(text)

        # Drop image lines, pure link-list lines, and heading-wrapped links
        text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
        text = re.sub(r"^\s*\*\s+\[.+?\]\(https?://.+?\)\s*$", "", text, flags=re.MULTILINE)
        text = re.sub(r"^#{1,4}\s+\[.+?\]\(https?://.+?\)\s*$", "", text, flags=re.MULTILINE)

        # Drop known noise lines
        lines = text.splitlines()
        lines = [l for l in lines if not _NOISE_LINE_PATTERNS.match(l)]
        text = "\n".join(lines)

        # Collapse whitespace
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        if not text and not _cached:
            return scrape_page_context(api_key, url, max_chars=max_chars, mode=mode, _cached=True)
        if not text:
            return {"content": "", "title": title, "success": False,
                    "error": "No content found after stripping boilerplate after cached fallback",
                    "source": "cached_fallback"}

        # Score paragraphs and keep the best ones up to max_chars
        paragraphs = re.split(r"\n{2,}", text)
        scored = [(p, _score_paragraph(p)) for p in paragraphs]

        result_paras = []
        chars_used = 0
        for para, score in scored:
            if chars_used >= max_chars:
                break
            is_heading = para.strip().startswith("#")
            if score > 0 or is_heading:
                result_paras.append(para)
                chars_used += len(para)

        content = "\n\n".join(result_paras).strip()

        # Final trim to max_chars at sentence boundary
        if len(content) > max_chars:
            truncated = content[:max_chars]
            last_period = truncated.rfind(".")
            if last_period > max_chars * 0.5:
                content = truncated[: last_period + 1].strip()
            else:
                content = truncated.strip()

        if not content and not _cached:
            return scrape_page_context(api_key, url, max_chars=max_chars, mode=mode, _cached=True)
        if not content:
            return {"content": "", "title": title, "success": False,
                    "error": "No substantive content found after scoring after cached fallback",
                    "source": "cached_fallback"}

        return {"content": content, "title": title, "success": True, "error": "",
                "source": "cached_fallback" if _cached else "live"}

    except requests.exceptions.Timeout:
        if not _cached:
            return scrape_page_context(api_key, url, max_chars=max_chars, mode=mode, _cached=True)
        return {"content": "", "title": "", "success": False,
                "error": "Request timed out after cached fallback", "source": "cached_fallback"}
    except requests.exceptions.HTTPError as e:
        suffix = " after cached fallback" if _cached else ""
        return {"content": "", "title": "", "success": False,
                "error": f"HTTP {e.response.status_code}{suffix}",
                "source": "cached_fallback" if _cached else "live"}
    except requests.exceptions.RequestException as e:
        return {"content": "", "title": "", "success": False, "error": str(e)}
    except Exception as e:
        return {"content": "", "title": "", "success": False, "error": str(e)}


def _process_firecrawl_text(text: str, max_chars: int, mode: str) -> dict:
    text = (text or "").strip()
    if not text:
        return {"content": "", "title": "", "success": False, "error": "Firecrawl returned empty content."}
    if mode == "ecommerce_collection":
        content, title = _build_collection_context(text, max_chars)
        if not content or content == "COLLECTION CONTEXT":
            return {
                "content": "",
                "title": title,
                "success": False,
                "error": "No collection products, filters, or content found",
            }
        return {"content": content, "title": title, "success": True, "error": ""}

    title = _extract_title(text)
    text = re.sub(r"!\[.*?\]\(.*?\)", "", text)
    text = re.sub(r"^\s*\*\s+\[.+?\]\(https?://.+?\)\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^#{1,4}\s+\[.+?\]\(https?://.+?\)\s*$", "", text, flags=re.MULTILINE)
    lines = [line for line in text.splitlines() if not _NOISE_LINE_PATTERNS.match(line)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    result_paragraphs = []
    chars_used = 0
    for paragraph in re.split(r"\n{2,}", text):
        if chars_used >= max_chars:
            break
        if _score_paragraph(paragraph) > 0 or paragraph.strip().startswith("#"):
            result_paragraphs.append(paragraph)
            chars_used += len(paragraph)
    content = "\n\n".join(result_paragraphs).strip()
    if len(content) > max_chars:
        truncated = content[:max_chars]
        last_period = truncated.rfind(".")
        content = truncated[:last_period + 1].strip() if last_period > max_chars * 0.5 else truncated.strip()
    if not content:
        return {"content": "", "title": title, "success": False, "error": "No substantive content found after scoring"}
    return {"content": content, "title": title, "success": True, "error": ""}


def _firecrawl_failure(message: str) -> dict:
    return {"content": "", "title": "", "success": False, "error": message, "source": "firecrawl"}


def _firecrawl_http_error(status_code: int) -> str:
    if status_code in (401, 403):
        return "Firecrawl authentication failed. Update the API key in Settings."
    if status_code == 402:
        return "Firecrawl credits are unavailable. Check the Firecrawl account."
    if status_code == 429:
        return "Firecrawl rate limit reached. Try again later."
    if status_code >= 500:
        return "Firecrawl is temporarily unavailable. Try again later."
    return "Firecrawl could not scrape this page."


def scrape_page_context_firecrawl(
    api_key: str,
    url: str,
    max_chars: int = 10000,
    mode: str = "default",
) -> dict:
    if not url:
        return _firecrawl_failure("No URL provided")
    if not api_key:
        return _firecrawl_failure("Firecrawl API key is not configured.")
    try:
        response = requests.post(
            FIRECRAWL_SCRAPE_URL,
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "onlyCleanContent": False,
                "maxAge": 0,
                "waitFor": 0,
                "timeout": _FIRECRAWL_RENDER_TIMEOUT_MS,
                "removeBase64Images": True,
                "blockAds": True,
                "proxy": "auto",
                "storeInCache": False,
            },
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=_FIRECRAWL_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.Timeout:
        return _firecrawl_failure("Firecrawl request timed out.")
    except requests.exceptions.RequestException:
        return _firecrawl_failure("Firecrawl could not be reached. Try again later.")

    if not 200 <= response.status_code < 300:
        return _firecrawl_failure(_firecrawl_http_error(response.status_code))
    try:
        response_body = response.json()
    except ValueError:
        return _firecrawl_failure("Firecrawl returned an invalid response.")
    page_data = response_body.get("data") if isinstance(response_body, dict) else None
    if not isinstance(response_body, dict) or response_body.get("success") is not True or not isinstance(page_data, dict):
        return _firecrawl_failure("Firecrawl could not scrape this page.")
    markdown = (page_data.get("markdown") or "").strip()
    metadata = page_data.get("metadata") or {}
    metadata_title = metadata.get("title", "") if isinstance(metadata, dict) else ""
    reader_text = f"Title: {metadata_title}\n\n{markdown}" if metadata_title else markdown
    result = _process_firecrawl_text(reader_text, max_chars, mode)
    if metadata_title and not result.get("title"):
        result["title"] = metadata_title
    result["source"] = "firecrawl"
    return result
