"""Turn an article page's HTML into clean text and metadata.

Generic first: Trafilatura finds the main article text and reads the page's metadata
(JSON-LD, Open Graph and <meta> tags). BeautifulSoup is used for the canonical URL,
the language, simple fallbacks, and optional per-site clean-up selectors.

Webpage content is untrusted: we only parse it (never execute it), strip control
characters, cap field lengths, and leave anything we cannot find as None.
"""
import re
from datetime import date, timedelta
from urllib.parse import urlsplit

import trafilatura
from bs4 import BeautifulSoup

from web import is_allowed_domain, normalize_url

MIN_TEXT_CHARS = 300        # shorter than this is not a real article (error page, stub, cookie wall...)
MAX_TEXT_CHARS = 100_000    # guard against enormous pages
# Titles of bot-check and error pages that some sites serve with a normal-looking response.
ERROR_PAGE_TITLE = re.compile(
    r"^(access denied|just a moment|attention required|checking connection|page not found|forbidden|error\b|40[134]\b)", re.I
)
# Bylines guessed from page text sometimes grab contact boxes ("Media Contact Jane Doe Tel 555...").
JUNK_AUTHOR = re.compile(r"\d|@|https?:|\b(contact|tel|phone|email)\b", re.I)
# Only trust dates found in the page markup (meta tags, JSON-LD, <time>) or in the URL.
# Trafilatura's default "extensive" search guesses from free text: on a bot-check page it returned 2000-01-01.
DATE_PARAMS = {"extensive_search": False, "original_date": True}
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ExtractionError(Exception):
    """The page does not contain a usable article."""


def extract_article(html: str, page_url: str, source: dict) -> dict:
    """Extract an article from HTML. Returns a dict of fields or raises ExtractionError."""
    if not html or not html.strip():
        raise ExtractionError("empty HTML")
    config = source.get("extraction", {})
    if config.get("method", "auto") != "auto":
        raise ExtractionError(f"unsupported extraction method {config.get('method')!r}")

    soup = BeautifulSoup(html, "lxml")
    selectors = config.get("remove_selectors", [])
    for selector in selectors:  # site-specific boilerplate that generic extraction keeps (e.g. disclaimers)
        for element in soup.select(selector):
            element.decompose()

    result = trafilatura.bare_extraction(
        str(soup) if selectors else html,
        url=page_url,
        with_metadata=True,
        favor_precision=True,       # prefer dropping a borderline paragraph over keeping menus/footers
        include_comments=False,
        include_tables=False,
        date_extraction_params=DATE_PARAMS,
    )
    data = result.as_dict() if result else {}

    title = clean_line(data.get("title") or meta_content(soup, "og:title") or (soup.title.get_text() if soup.title else ""))
    title = strip_site_name(title, source["name"])
    if not title or ERROR_PAGE_TITLE.match(title):
        raise ExtractionError(f"missing or error-page title: {title!r}")
    text = clean_text(data.get("text") or "", title)
    if len(text) < MIN_TEXT_CHARS:
        raise ExtractionError(f"article text too short ({len(text)} chars)")

    description = clean_line(data.get("description") or meta_content(soup, "description") or meta_content(soup, "og:description"))
    author = clean_line(data.get("author"))
    if JUNK_AUTHOR.search(author):
        author = ""
    # Some sites' generic date tags are wrong (MGB's "published-time" is when the page was last re-published).
    # A source can name the one meta tag holding the real publication date; if that tag is missing: None.
    date_meta = config.get("date_meta")
    published = meta_content(soup, date_meta) if date_meta else data.get("date")
    return {
        "canonical_url": find_canonical_url(soup, page_url, source),
        "fetched_url": page_url,
        "title": title[:300],
        "description": description[:1000] or None,
        "text": text[:MAX_TEXT_CHARS],
        "published_date": plausible_date(published),
        "author": author[:200] or None,
        "language": find_language(soup),
    }


def find_canonical_url(soup: BeautifulSoup, page_url: str, source: dict) -> str:
    """The page's declared canonical URL if it is trustworthy, otherwise the fetched URL.

    Trustworthy means: an http(s) URL on one of the source's allowed domains that is not the
    site's home page or the listing page (a common misconfiguration that would merge articles).
    """
    link = soup.find("link", rel="canonical")
    candidates = [link.get("href") if link else None, meta_content(soup, "og:url")]
    listing_page = normalize_url(source["start_url"])
    for candidate in candidates:
        url = normalize_url(candidate, base=page_url)
        if url and is_allowed_domain(url, source["allowed_domains"]) and urlsplit(url).path not in ("", "/") \
                and url != listing_page:
            return url
    return normalize_url(page_url) or page_url


def find_language(soup: BeautifulSoup) -> str | None:
    """Primary language code from <html lang> or og:locale, e.g. 'en-US' -> 'en'."""
    raw = (soup.html.get("lang") if soup.html else None) or meta_content(soup, "og:locale")
    code = re.split(r"[-_]", raw.strip().lower())[0] if raw else ""
    return code if re.fullmatch(r"[a-z]{2,3}", code) else None


def plausible_date(value: str | None) -> str | None:
    """Keep a YYYY-MM-DD date only if it parses and is neither ancient nor in the future."""
    if not value:
        return None
    try:
        parsed = date.fromisoformat(str(value)[:10])
    except ValueError:
        return None
    if parsed < date(1995, 1, 1) or parsed > date.today() + timedelta(days=1):
        return None
    return parsed.isoformat()


def meta_content(soup: BeautifulSoup, key: str) -> str:
    """Content of <meta property=key> or <meta name=key>, or ''."""
    tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
    return (tag.get("content") or "") if tag else ""


def strip_site_name(title: str, site_name: str) -> str:
    """Remove a trailing ' | Site Name' (or ' - Site Name') that some pages append to titles."""
    for separator in (" | ", " - ", " – ", " — "):
        suffix = separator + site_name
        if title.lower().endswith(suffix.lower()):
            return title[: -len(suffix)].rstrip()
    return title


def clean_line(value: str | None) -> str:
    """Collapse whitespace and remove control characters."""
    return " ".join(CONTROL_CHARS.sub(" ", value or "").split())


def clean_text(text: str, title: str) -> str:
    """Clean every line, drop empty lines and leaked template values, and a leading repeat of the headline."""
    lines = [clean_line(line) for line in text.splitlines()]
    # Some site builders leave config values such as a bare "false" in the HTML; they are never content.
    lines = [line for line in lines if line and line.lower() not in ("true", "false", "null", "undefined")]
    if lines and lines[0].lower() == title.lower():
        lines = lines[1:]
    return "\n".join(lines)
