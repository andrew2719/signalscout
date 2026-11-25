"""Find recent article URLs for a source.

Each source in sources.json picks one discovery method:
  rss      read the item links of an RSS/Atom feed (feeds list newest items first)
  html     collect links from a news listing page, optionally narrowed by a CSS selector
  sitemap  read an XML sitemap and take the most recently modified pages

Discovery only returns URLs. It never crawls beyond the configured start page.
"""
import logging
import re
from urllib.parse import urlsplit

import feedparser
from bs4 import BeautifulSoup

from web import fetch, is_allowed_domain, normalize_url

log = logging.getLogger(__name__)

# Links to files, and path segments that mark listing/navigation pages, are never articles.
SKIP_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".pdf", ".mp3", ".mp4", ".mov", ".zip",
                   ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".ics", ".xml", ".json", ".css", ".js")
SKIP_PATH_SEGMENTS = {"tag", "tags", "category", "categories", "author", "authors", "page", "search",
                      "feed", "rss", "login", "subscribe", "contact", "about", "privacy", "terms"}


class DiscoveryError(Exception):
    """The start page could not be turned into a list of links."""


def discover_urls(source: dict, limit: int) -> list[str]:
    """Return up to `limit` article URLs for one source."""
    discovery = source["discovery"]
    method = discovery["method"]
    if method == "rss":
        links, base_url = links_from_rss(source["start_url"])
    elif method == "html":
        links, base_url = links_from_html(source["start_url"], discovery.get("link_selector"))
    elif method == "sitemap":
        links, base_url = links_from_sitemap(source["start_url"])
    else:
        raise DiscoveryError(f"unknown discovery method {method!r}")
    return filter_article_urls(links, source, base_url, limit)


def links_from_rss(feed_url: str) -> tuple[list[str], str]:
    response = fetch(feed_url)
    feed = feedparser.parse(response.content)  # parse the bytes we fetched; feedparser never downloads itself
    if not feed.entries and feed.bozo:
        raise DiscoveryError(f"not a valid RSS/Atom feed ({type(feed.bozo_exception).__name__})")
    return [entry.get("link") for entry in feed.entries], response.url


def links_from_html(page_url: str, link_selector: str | None) -> tuple[list[str], str]:
    response = fetch(page_url)
    soup = BeautifulSoup(response.text, "lxml")
    anchors = soup.select(link_selector) if link_selector else soup.find_all("a")
    return [a.get("href") for a in anchors], response.url


def links_from_sitemap(sitemap_url: str) -> tuple[list[str], str]:
    response = fetch(sitemap_url)
    soup = BeautifulSoup(response.content, "xml")
    entries = []
    for url_tag in soup.find_all("url"):
        loc, lastmod = url_tag.find("loc"), url_tag.find("lastmod")
        if loc:
            entries.append((lastmod.get_text(strip=True) if lastmod else "", loc.get_text(strip=True)))
    if not entries and soup.find("sitemapindex"):
        raise DiscoveryError("start_url is a sitemap index; point it at one of the sitemaps it lists")
    entries.sort(reverse=True)  # ISO dates sort correctly as text, so this puts the newest first
    return [loc for _, loc in entries], response.url


def filter_article_urls(links: list[str | None], source: dict, base_url: str, limit: int) -> list[str]:
    """Normalize links and keep unique article URLs on the source's allowed domains, in order."""
    pattern = source["discovery"].get("url_pattern")
    listing_page = normalize_url(source["start_url"])
    seen: set[str] = set()
    articles: list[str] = []
    for link in links:
        url = normalize_url(link, base=base_url)
        if not url or url in seen or url == listing_page:
            continue
        seen.add(url)
        path = urlsplit(url).path.lower()
        if not is_allowed_domain(url, source["allowed_domains"]):
            continue
        if path in ("", "/") or path.endswith(SKIP_EXTENSIONS):
            continue
        if any(segment in SKIP_PATH_SEGMENTS for segment in path.split("/")):
            continue
        if pattern and not re.search(pattern, url):
            continue
        articles.append(url)
        if len(articles) >= limit:
            break
    return articles
