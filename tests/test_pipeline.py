"""Tests for the SignalScout pipeline. They run offline: no websites, no MongoDB.

Run:  conda run -n ml python -m pytest -v

The last test is opt-in and uses MongoDB Atlas with a separate collection, articles_test:
      SIGNALSCOUT_DB_TESTS=1 conda run -n ml python -m pytest -v -k real_mongodb
"""
import os
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.robotparser import RobotFileParser

import pytest
from pydantic import ValidationError

import discover
import ingest
import web
from database import DB_NAME, ensure_indexes, get_client, save_article, search_articles
from extract import ExtractionError, extract_article
from models import Article, build_article, compute_content_hash, find_tags, make_excerpt
from web import FetchError, is_allowed_domain, normalize_url

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "article.html").read_text(encoding="utf-8")
ARTICLE_URL = "https://news.example-health.org/2026/11/20/ai-imaging-center"
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
SOURCE = {
    "id": "example_health",
    "name": "Example Health",
    "industry": "healthcare",
    "start_url": "https://news.example-health.org/news",
    "allowed_domains": ["example-health.org"],
    "discovery": {"method": "html", "link_selector": None, "url_pattern": r"/\d{4}/\d{2}/\d{2}/"},
    "extraction": {"method": "auto", "content_scope": "excerpt", "remove_selectors": ["p.usage-notice"]},
    "enabled": True,
}

# A plain page with no date, author, description or canonical link anywhere.
NO_METADATA_HTML = """<html><head><title>Hospital Volunteers Plant a Community Garden</title></head><body>
<article><h1>Hospital Volunteers Plant a Community Garden</h1>
<p>Volunteers from the hospital spent Saturday morning planting vegetables and herbs in a new community garden next to the outpatient clinic, working alongside neighbors and local students.</p>
<p>The garden will supply fresh produce to the clinic's nutrition program, which teaches families how to cook simple, healthy meals on a budget, and any extra harvest will go to the food pantry.</p>
<p>Organizers said they hope to add a small greenhouse next spring so the program can keep running through the colder months of the year.</p>
</article></body></html>"""


def sample_document() -> dict:
    return build_article(SOURCE, extract_article(FIXTURE_HTML, ARTICLE_URL, SOURCE)).model_dump(mode="json")


# ---------- URL normalization ----------

@pytest.mark.parametrize("raw, expected", [
    ("https://News.Example.org/a/Story?id=7&utm_source=x#top", "https://news.example.org/a/Story?id=7"),
    ("https://example.org/a?b=1&fbclid=abc&gclid=def", "https://example.org/a?b=1"),
    ("https://example.org/list?page=2&sort=date", "https://example.org/list?page=2&sort=date"),  # meaningful params kept
    ("https://example.org/news/story/", "https://example.org/news/story/"),                       # trailing slash kept
    ("https://example.org:443/a", "https://example.org/a"),
    ("http://example.org", "http://example.org/"),
    ("mailto:press@example.org", None),
    ("javascript:void(0)", None),
    ("", None),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_normalize_url_resolves_relative_links():
    assert normalize_url("../2026/story#comments", base="https://example.org/news/list") == "https://example.org/2026/story"


def test_allowed_domain_accepts_subdomains_but_not_lookalikes():
    assert is_allowed_domain("https://news.example-health.org/x", ["example-health.org"])
    assert not is_allowed_domain("https://example-health.org.evil.com/x", ["example-health.org"])
    assert not is_allowed_domain("https://notexample-health.org/x", ["example-health.org"])


# ---------- Discovery and duplicate URLs ----------

def fake_response(body: str, url: str) -> SimpleNamespace:
    return SimpleNamespace(text=body, content=body.encode("utf-8"), url=url)


def test_html_discovery_filters_and_removes_duplicates(monkeypatch):
    listing = """<html><body>
      <a href="/2026/11/20/story-one">Story one</a>
      <a href="/2026/11/20/story-one#comments">Same story, with a fragment</a>
      <a href="https://news.example-health.org/2026/11/19/story-two?utm_source=rss">Story two</a>
      <a href="/category/news">A category page</a>
      <a href="/media/2026/11/20/photo.jpg">An image</a>
      <a href="https://other-site.com/2026/11/20/story">Off-site link</a>
      <a href="mailto:press@example-health.org">Email</a>
      <a href="/news">The listing page itself</a>
    </body></html>"""
    monkeypatch.setattr(discover, "fetch", lambda url: fake_response(listing, url))
    assert discover.discover_urls(SOURCE, limit=10) == [
        "https://news.example-health.org/2026/11/20/story-one",
        "https://news.example-health.org/2026/11/19/story-two",
    ]
    assert len(discover.discover_urls(SOURCE, limit=1)) == 1  # the per-source limit is respected


def test_rss_discovery_keeps_feed_order(monkeypatch):
    feed = """<?xml version="1.0"?><rss version="2.0"><channel><title>News</title>
      <item><title>Newest</title><link>https://news.example-health.org/2026/11/21/newest/</link></item>
      <item><title>Older</title><link>https://news.example-health.org/2026/11/18/older/</link></item>
      <item><title>Older again</title><link>https://news.example-health.org/2026/11/18/older/</link></item>
    </channel></rss>"""
    monkeypatch.setattr(discover, "fetch", lambda url: fake_response(feed, url))
    source = {**SOURCE, "discovery": {"method": "rss", "url_pattern": None}}
    assert discover.discover_urls(source, limit=10) == [
        "https://news.example-health.org/2026/11/21/newest/",
        "https://news.example-health.org/2026/11/18/older/",
    ]


def test_sitemap_discovery_returns_newest_first(monkeypatch):
    sitemap = """<?xml version="1.0" encoding="UTF-8"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://news.example-health.org/en/old-story</loc><lastmod>2026-08-05</lastmod></url>
      <url><loc>https://news.example-health.org/en/new-story</loc><lastmod>2026-11-23</lastmod></url>
      <url><loc>https://news.example-health.org/en/middle-story</loc><lastmod>2026-10-01</lastmod></url>
    </urlset>"""
    monkeypatch.setattr(discover, "fetch", lambda url: fake_response(sitemap, url))
    source = {**SOURCE, "discovery": {"method": "sitemap", "url_pattern": None}}
    assert discover.discover_urls(source, limit=2) == [
        "https://news.example-health.org/en/new-story",
        "https://news.example-health.org/en/middle-story",
    ]


def test_ingest_skips_urls_that_resolve_to_the_same_canonical_url(monkeypatch):
    # Three different discovered URLs, but the page declares one canonical URL: only one document is kept.
    urls = [ARTICLE_URL, ARTICLE_URL + "?ref=homepage", ARTICLE_URL + "/amp"]
    monkeypatch.setattr(ingest, "discover_urls", lambda source, limit: urls)
    monkeypatch.setattr(ingest, "fetch_and_extract",
                        lambda url, source: build_article(source, extract_article(FIXTURE_HTML, url, source)))
    counts = Counter()
    documents = ingest.collect_source(SOURCE, 10, set(), counts)
    assert len(documents) == 1
    assert counts["valid"] == 1 and counts["duplicates"] == 2


# ---------- Extraction ----------

def test_extraction_from_saved_fixture():
    article = extract_article(FIXTURE_HTML, ARTICLE_URL, SOURCE)
    assert article["title"] == "Example Health Opens AI-Assisted Imaging Center"
    assert article["description"].startswith("The new center uses machine learning tools")
    assert article["published_date"] == "2026-11-20"
    assert article["author"] == "Jordan Lee"
    assert article["language"] == "en"
    # The declared canonical URL is used, without its tracking parameters and fragment.
    assert article["canonical_url"] == ARTICLE_URL
    assert article["text"].startswith("SPRINGFIELD")
    assert "radiologists still review every scan" in article["text"].lower()


def test_extraction_removes_navigation_footer_and_configured_boilerplate():
    text = extract_article(FIXTURE_HTML, ARTICLE_URL, SOURCE)["text"]
    for junk in ("Careers at Example Health", "Contact Us", "All rights reserved", "Privacy Policy",
                 "parking garage", "news media use only"):
        assert junk not in text


def test_missing_optional_metadata_stays_null():
    article = extract_article(NO_METADATA_HTML, "https://news.example-health.org/stories/garden", SOURCE)
    assert article["title"] == "Hospital Volunteers Plant a Community Garden"
    assert article["published_date"] is None  # never today's date or a guess
    assert article["author"] is None
    assert article["description"] is None
    assert article["canonical_url"] == "https://news.example-health.org/stories/garden"  # falls back to the fetched URL


def test_configured_date_tag_replaces_generic_date_detection():
    source = {**SOURCE, "extraction": {**SOURCE["extraction"], "date_meta": "articlepublishdate"}}
    html = FIXTURE_HTML.replace("<head>", '<head><meta name="articlepublishdate" content="2026-05-20T08:00:00">')
    assert extract_article(html, ARTICLE_URL, source)["published_date"] == "2026-05-20"  # not JSON-LD's 2026-11-20
    # If the configured tag is missing, the date is unknown: no fallback to the generic (untrusted) date.
    assert extract_article(FIXTURE_HTML, ARTICLE_URL, source)["published_date"] is None


def test_site_name_suffix_is_removed_from_titles():
    html = FIXTURE_HTML.replace('content="Example Health Opens AI-Assisted Imaging Center"',
                                'content="Example Health Opens AI-Assisted Imaging Center | Example Health"')
    assert extract_article(html, ARTICLE_URL, SOURCE)["title"] == "Example Health Opens AI-Assisted Imaging Center"


def test_leaked_template_values_and_repeated_headline_are_removed():
    html = FIXTURE_HTML.replace("<h1>", "<div><div>false</div></div><h1>")
    text = extract_article(html, ARTICLE_URL, SOURCE)["text"]
    assert text.startswith("SPRINGFIELD")  # neither "false" nor the headline is left at the start


def test_contact_details_are_not_accepted_as_author():
    html = NO_METADATA_HTML.replace("<head>", '<head><meta name="author" content="Media Contact Jane Doe Tel 555-0100">')
    assert extract_article(html, "https://news.example-health.org/stories/garden", SOURCE)["author"] is None


@pytest.mark.parametrize("html", [
    "",
    "<html><head><title>Just a moment...</title></head><body><p>Checking your browser before accessing the site.</p></body></html>",
    "<html><head><title>A real headline</title></head><body><p>Too short.</p></body></html>",
])
def test_empty_or_invalid_pages_are_rejected(html):
    with pytest.raises(ExtractionError):
        extract_article(html, ARTICLE_URL, SOURCE)


# ---------- Common document format (Pydantic) ----------

def test_build_article_produces_a_valid_excerpt_document():
    doc = sample_document()
    assert doc["schema_version"] == 1
    assert doc["source"] == {"id": "example_health", "name": "Example Health",
                             "domain": "news.example-health.org", "industry": "healthcare"}
    assert doc["content_scope"] == "excerpt"
    assert len(doc["content"]) <= 502
    assert doc["published_date"] == "2026-11-20"
    assert doc["organizations"] == []
    assert "ai" in doc["tags"] and "expansion" in doc["tags"]


def test_full_content_scope_keeps_the_whole_text():
    source = {**SOURCE, "extraction": {**SOURCE["extraction"], "content_scope": "full"}}
    extracted = extract_article(FIXTURE_HTML, ARTICLE_URL, source)
    article = build_article(source, extracted)
    assert article.content_scope == "full"
    assert article.content == extracted["text"]


@pytest.mark.parametrize("field, bad_value", [
    ("title", ""),                       # required field empty
    ("content", "Too short to be an article."),
    ("canonical_url", "/relative/path"),
    ("content_hash", "not-a-sha256"),
    ("content_scope", "everything"),
])
def test_article_model_rejects_invalid_documents(field, bad_value):
    doc = sample_document()
    doc[field] = bad_value
    with pytest.raises(ValidationError):
        Article.model_validate(doc)


def test_article_model_allows_missing_optional_fields():
    doc = sample_document()
    for field in ("description", "published_date", "author", "language"):
        doc[field] = None
    assert Article.model_validate(doc).published_date is None


def test_make_excerpt_cuts_at_a_sentence_boundary():
    text = "First sentence is here. " * 40
    excerpt = make_excerpt(text, max_chars=500)
    assert len(excerpt) <= 500 and excerpt.endswith(".")
    assert make_excerpt("Short text.") == "Short text."


# ---------- Content hash and tags ----------

def test_content_hash_is_consistent():
    base = compute_content_hash("Hospital Opens Clinic", "The clinic opened today.\nIt has 20 rooms.")
    assert base == compute_content_hash("  hospital opens  clinic ", "The clinic   opened today. It has 20 rooms.")
    assert base != compute_content_hash("Hospital Opens Clinic", "The clinic opened today. It has 25 rooms.")
    assert base != compute_content_hash("Hospital Opens New Clinic", "The clinic opened today.\nIt has 20 rooms.")
    assert len(base) == 64


def test_keyword_tags():
    assert find_tags("Health system acquires regional hospital") == ["acquisition"]
    assert "acquisition" not in find_tags("Tumors developed acquired resistance to the drug")  # medical sense
    assert "ai" in find_tags("New AI tool helps doctors")
    assert "ai" not in find_tags("Raising awareness of daily hydration")  # 'ai' inside words is ignored
    assert find_tags("Volunteers plant a community garden") == []          # no forced category


# ---------- MongoDB upsert logic (mocked collection) ----------

def test_new_article_is_inserted_with_all_timestamps():
    collection, doc = MagicMock(), sample_document()
    collection.find_one.return_value = None
    assert save_article(collection, doc, NOW) == "inserted"
    (query, update), options = collection.update_one.call_args
    assert query == {"canonical_url": doc["canonical_url"]} and options["upsert"] is True
    assert update["$setOnInsert"]["first_seen_at"] == NOW
    assert update["$set"]["last_fetched_at"] == NOW
    assert update["$set"]["last_updated_at"] == NOW


def test_changed_content_updates_but_keeps_first_seen_at():
    collection, doc = MagicMock(), sample_document()
    collection.find_one.return_value = {"content_hash": "0" * 64}
    assert save_article(collection, doc, NOW) == "updated"
    (_, update), _ = collection.update_one.call_args
    assert update["$set"]["content_hash"] == doc["content_hash"]
    assert update["$set"]["last_updated_at"] == NOW
    assert "first_seen_at" not in update["$set"]  # only ever written on insert


def test_unchanged_content_only_refreshes_last_fetched_at():
    collection, doc = MagicMock(), sample_document()
    collection.find_one.return_value = {"content_hash": doc["content_hash"]}
    assert save_article(collection, doc, NOW) == "unchanged"
    (_, update), _ = collection.update_one.call_args
    assert update["$set"]["last_fetched_at"] == NOW
    assert "last_updated_at" not in update["$set"]


def test_empty_values_never_overwrite_stored_values():
    collection, doc = MagicMock(), sample_document()
    doc.update(author=None, description=None, tags=[])
    collection.find_one.return_value = {"content_hash": doc["content_hash"]}
    save_article(collection, doc, NOW)
    (_, update), _ = collection.update_one.call_args
    for field in ("author", "description", "tags"):
        assert field not in update["$set"]
        assert field in update["$setOnInsert"]  # a brand-new document still gets the (empty) field


# ---------- HTTP retries ----------

def fake_http(monkeypatch, statuses: list[int]) -> list[str]:
    """Make web.fetch see the given HTTP statuses in order, without network or sleeping."""
    calls = []
    responses = iter(SimpleNamespace(status_code=s, headers={"Content-Type": "text/html; charset=utf-8"}, url=ARTICLE_URL)
                     for s in statuses)
    def fake_get(url, timeout):
        calls.append(url)
        return next(responses)
    allow_everything = RobotFileParser()
    allow_everything.allow_all = True
    monkeypatch.setattr(web, "_robots_for", lambda url: allow_everything)
    monkeypatch.setattr(web.session, "get", fake_get)
    monkeypatch.setattr(web.time, "sleep", lambda seconds: None)
    return calls


def test_fetch_retries_server_errors(monkeypatch):
    calls = fake_http(monkeypatch, [503, 502, 200])
    assert web.fetch(ARTICLE_URL).status_code == 200
    assert len(calls) == 3  # first attempt + at most two retries


def test_fetch_gives_up_after_two_retries(monkeypatch):
    calls = fake_http(monkeypatch, [503, 503, 503, 200])
    with pytest.raises(FetchError):
        web.fetch(ARTICLE_URL)
    assert len(calls) == 3


def test_fetch_does_not_retry_forbidden_or_missing_pages(monkeypatch):
    for status in (403, 404):
        calls = fake_http(monkeypatch, [status, 200])
        with pytest.raises(FetchError) as error:
            web.fetch(ARTICLE_URL)
        assert error.value.status == status and len(calls) == 1


# ---------- Optional: the same upsert rules against a real MongoDB Atlas test collection ----------

@pytest.mark.skipif(os.getenv("SIGNALSCOUT_DB_TESTS") != "1", reason="set SIGNALSCOUT_DB_TESTS=1 to use MongoDB Atlas")
def test_upserts_against_real_mongodb():
    client = get_client()
    collection = client[DB_NAME]["articles_test"]  # separate, clearly named collection; never the real one
    collection.drop()
    try:
        ensure_indexes(collection)
        ensure_indexes(collection)  # running it twice must be harmless
        first, second, third = NOW, NOW + timedelta(days=1), NOW + timedelta(days=2)
        doc = sample_document()

        assert save_article(collection, doc, first) == "inserted"
        assert save_article(collection, doc, second) == "unchanged"
        edited = {**doc, "content": doc["content"] + " A correction was added.", "content_hash": "f" * 64, "author": None}
        assert save_article(collection, edited, third) == "updated"

        assert collection.count_documents({}) == 1  # no duplicates
        stored = collection.find_one({"canonical_url": doc["canonical_url"]})
        assert stored["first_seen_at"] == first.replace(tzinfo=None)  # PyMongo returns naive UTC datetimes
        assert stored["last_fetched_at"] == third.replace(tzinfo=None)
        assert stored["last_updated_at"] == third.replace(tzinfo=None)
        assert stored["content_hash"] == "f" * 64
        assert stored["author"] == "Jordan Lee"  # the empty value from the third run did not erase it
        assert search_articles(collection, "imaging radiologists")[0]["canonical_url"] == doc["canonical_url"]
    finally:
        collection.drop()
        client.close()
