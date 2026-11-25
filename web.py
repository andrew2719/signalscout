"""Polite HTTP fetching and URL helpers, shared by discovery and extraction.

fetch() is the only function in the project that downloads web pages. It:
  - identifies itself with an honest bot User-Agent,
  - checks robots.txt before fetching a URL,
  - waits between requests to the same host (robots.txt Crawl-delay if larger),
  - retries timeouts, connection errors, 429 and 5xx at most twice,
  - honours Retry-After, and never retries 403/404 or other client errors.
"""
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)

BOT_NAME = "research-news-bot"
USER_AGENT = f"Mozilla/5.0 (compatible; {BOT_NAME}/0.1; educational project)"
TIMEOUT = (10, 30)       # seconds: (connect, read)
DEFAULT_DELAY = 2.0      # minimum seconds between two requests to the same host
MAX_RETRIES = 2          # extra attempts for transient failures only
MAX_RETRY_AFTER = 120    # if a server asks us to wait longer than this, give up for this run
RETRY_STATUSES = {429, 500, 502, 503, 504}

# Query parameters that only track clicks and never change the page content.
TRACKING_PARAMS = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "mkt_tok", "igshid", "yclid", "_ga"}

session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})

_robots: dict[str, RobotFileParser] = {}   # parsed robots.txt per "scheme://host"
_last_request: dict[str, float] = {}       # time of the last request per host


class FetchError(Exception):
    """A URL could not be fetched. `status` holds the HTTP status code when there was one."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def normalize_url(url: str | None, base: str | None = None) -> str | None:
    """Return a clean absolute URL, or None if it is not a usable http(s) URL.

    Resolves relative links against `base`, lowercases the scheme and host, drops the
    default port, the #fragment and known tracking parameters (utm_*, gclid, ...).
    The path, trailing slash and all other query parameters are kept exactly as they were.
    """
    if not url or not url.strip():
        return None
    url = urljoin(base, url.strip()) if base else url.strip()
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port  # .port raises ValueError for junk like ":abc"
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host or parts.username or parts.password:
        return None
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in (None, default_port) else f"{host}:{port}"

    query = parts.query
    params = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in params if not (k.lower().startswith("utm_") or k.lower() in TRACKING_PARAMS)]
    if len(kept) != len(params):  # only rebuild the query string when we actually removed something
        query = urlencode(kept)
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


def is_allowed_domain(url: str, allowed_domains: list[str]) -> bool:
    """True if the URL's host is one of the allowed domains or a subdomain of one."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d.lower() or host.endswith("." + d.lower()) for d in allowed_domains)


def fetch(url: str) -> requests.Response:
    """GET a URL politely. Returns the 200 response or raises FetchError."""
    robots = _robots_for(url)
    if not robots.can_fetch(BOT_NAME, url):
        raise FetchError("disallowed by robots.txt")
    host = urlsplit(url).netloc
    delay = max(DEFAULT_DELAY, float(robots.crawl_delay(BOT_NAME) or 0))

    for attempt in range(MAX_RETRIES + 1):
        _wait_for_turn(host, delay)
        try:
            response = session.get(url, timeout=TIMEOUT)
        except (requests.Timeout, requests.ConnectionError) as error:
            if attempt == MAX_RETRIES:
                raise FetchError(f"network error: {type(error).__name__}") from error
            log.warning("Network error for %s (%s), retrying", url, type(error).__name__)
            time.sleep(5 * (attempt + 1))
            continue
        except requests.RequestException as error:  # invalid URL, too many redirects, ...
            raise FetchError(f"request failed: {type(error).__name__}") from error

        if response.status_code == 200:
            if "charset" not in response.headers.get("Content-Type", "").lower():
                response.encoding = response.apparent_encoding  # requests would otherwise assume ISO-8859-1
            return response
        if response.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
            wait = _retry_after_seconds(response)
            wait = 5 * (attempt + 1) if wait is None else wait
            if wait > MAX_RETRY_AFTER:
                raise FetchError(f"HTTP {response.status_code}, asked to wait {wait:.0f}s", response.status_code)
            log.warning("HTTP %s for %s, retrying in %.0fs", response.status_code, url, wait)
            time.sleep(wait)
            continue
        raise FetchError(f"HTTP {response.status_code}", response.status_code)
    raise FetchError("retries exhausted")  # not reached; keeps type checkers happy


def _robots_for(url: str) -> RobotFileParser:
    """Download and cache robots.txt for the URL's host (once per run)."""
    parts = urlsplit(url)
    root = f"{parts.scheme}://{parts.netloc}"
    if root not in _robots:
        parser = RobotFileParser()
        _wait_for_turn(parts.netloc, DEFAULT_DELAY)
        try:
            response = session.get(root + "/robots.txt", timeout=TIMEOUT)
            status = response.status_code
            if status == 200:
                parser.parse(response.text.splitlines())
            elif 400 <= status < 500 and status not in (401, 403, 429):
                parser.allow_all = True     # no robots.txt (e.g. 404): nothing is disallowed
            else:
                parser.disallow_all = True  # forbidden, rate limited or server error: stay away this run
                log.warning("robots.txt for %s returned HTTP %s; treating the site as off-limits this run", root, status)
        except requests.RequestException as error:
            parser.disallow_all = True
            log.warning("robots.txt for %s unreachable (%s); treating the site as off-limits this run",
                        root, type(error).__name__)
        _robots[root] = parser
    return _robots[root]


def _wait_for_turn(host: str, delay: float) -> None:
    """Sleep so that consecutive requests to the same host are at least `delay` seconds apart."""
    last = _last_request.get(host)
    if last is not None:
        remaining = delay - (time.monotonic() - last)
        if remaining > 0:
            time.sleep(remaining)
    _last_request[host] = time.monotonic()


def _retry_after_seconds(response: requests.Response) -> float | None:
    """Parse a Retry-After header given either in seconds or as an HTTP date."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None
