"""MongoDB Atlas helpers.

Write side, used only by ingest.py:  ensure_indexes, save_article.
Read side, used by search.py and the MCP server:  search_articles, find_by_tags, find_by_source,
find_published_since, count_without_date, source_stats. These functions only read.
"""
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pymongo import DESCENDING, TEXT, MongoClient
from pymongo.collection import Collection

DB_NAME = "signalscout"
COLLECTION_NAME = "articles"
ENV_FILE = Path(__file__).parent / ".env"  # explicit path so cron jobs work from any directory; optional on Render
QUERY_TIMEOUT_MS = 5_000  # server-side time limit (maxTimeMS) for every read query
# The fields a reader needs. MongoDB ids, hashes and ingestion timestamps stay in the database.
ARTICLE_FIELDS = {"_id": 0, "title": 1, "source.id": 1, "source.name": 1, "published_date": 1,
                  "description": 1, "content": 1, "content_scope": 1, "tags": 1, "canonical_url": 1}


def get_client() -> MongoClient:
    """Create a MongoDB client from MONGODB_URI (the URI is never printed).

    Locally the value comes from .env; on Render it is a normal environment variable and no
    .env file exists (load_dotenv just does nothing). A real environment variable wins over .env.
    """
    load_dotenv(ENV_FILE)
    uri = os.getenv("MONGODB_URI")
    if not uri:
        raise RuntimeError("MONGODB_URI is not set. Copy .env.example to .env and fill it in.")
    # Fail fast (10 s) instead of hanging for the 30 s default when Atlas is unreachable.
    return MongoClient(uri, serverSelectionTimeoutMS=10_000, connectTimeoutMS=10_000, appname="signalscout")


def get_collection(client: MongoClient | None = None) -> Collection:
    """The articles collection. MongoDB creates the database and collection on first write."""
    client = client or get_client()
    return client[DB_NAME][COLLECTION_NAME]


def ensure_indexes(collection: Collection) -> None:
    """Create indexes. Safe to run every time: an existing identical index is left alone."""
    collection.create_index("canonical_url", unique=True, name="canonical_url_unique")
    collection.create_index([("published_date", DESCENDING)], name="published_date")
    collection.create_index("source.id", name="source_id")
    # Keyword search. Description is included because most sources only allow storing an excerpt.
    collection.create_index(
        [("title", TEXT), ("description", TEXT), ("content", TEXT)],
        weights={"title": 3, "description": 2, "content": 1},
        default_language="english",
        name="text_search",
    )


def save_article(collection: Collection, article: dict, now: datetime) -> str:
    """Insert or update one article, keyed by canonical_url.

    Returns "inserted", "updated" (content hash changed) or "unchanged".
    - first_seen_at is only written when the document is created ($setOnInsert).
    - last_fetched_at is refreshed on every run; last_updated_at only when the content changes.
    - Empty values (None, "", []) never overwrite stored data, so a run where the page's
      author or date could not be read does not erase what an earlier run found.
    """
    url = article["canonical_url"]
    existing = collection.find_one({"canonical_url": url}, {"content_hash": 1})
    if existing is None:
        status = "inserted"
    elif existing.get("content_hash") != article["content_hash"]:
        status = "updated"
    else:
        status = "unchanged"

    filled = {key: value for key, value in article.items() if value not in (None, "", [], {})}
    empty = {key: value for key, value in article.items() if key not in filled}
    update = {
        "$set": {**filled, "last_fetched_at": now},
        "$setOnInsert": {**empty, "first_seen_at": now},  # new documents still get every field
    }
    if status != "unchanged":
        update["$set"]["last_updated_at"] = now
    collection.update_one({"canonical_url": url}, update, upsert=True)
    return status


# --- Read side -------------------------------------------------------------------------------

def search_articles(collection: Collection, query: str, limit: int = 5) -> list[dict]:
    """Keyword search with the text index (title, description, excerpt), best matches first."""
    projection = {**ARTICLE_FIELDS, "score": {"$meta": "textScore"}}
    cursor = collection.find({"$text": {"$search": query}}, projection)
    return list(cursor.sort([("score", {"$meta": "textScore"})]).limit(limit).max_time_ms(QUERY_TIMEOUT_MS))


def find_by_tags(collection: Collection, tags: list[str], limit: int) -> list[dict]:
    """Articles carrying any of the given keyword tags, newest publication date first."""
    cursor = collection.find({"tags": {"$in": tags}}, ARTICLE_FIELDS)
    return list(cursor.sort([("published_date", DESCENDING)]).limit(limit).max_time_ms(QUERY_TIMEOUT_MS))


def find_by_source(collection: Collection, source_id: str, limit: int) -> list[dict]:
    """One source's articles, newest publication date first (articles without a date sort last)."""
    cursor = collection.find({"source.id": source_id}, ARTICLE_FIELDS)
    return list(cursor.sort([("published_date", DESCENDING)]).limit(limit).max_time_ms(QUERY_TIMEOUT_MS))


def find_published_since(collection: Collection, since: str, limit: int) -> list[dict]:
    """Articles published on or after `since` (YYYY-MM-DD), newest first.

    Dates are stored as "YYYY-MM-DD" strings, which compare correctly as text. Articles with an
    unknown (null) publication date never match a string comparison, so they are excluded.
    """
    cursor = collection.find({"published_date": {"$gte": since}}, ARTICLE_FIELDS)
    return list(cursor.sort([("published_date", DESCENDING)]).limit(limit).max_time_ms(QUERY_TIMEOUT_MS))


def count_without_date(collection: Collection) -> int:
    """Number of articles whose publication date is unknown."""
    return collection.count_documents({"published_date": None}, maxTimeMS=QUERY_TIMEOUT_MS)


def source_stats(collection: Collection) -> list[dict]:
    """Per-source article counts, publication-date range and collection times, largest source first.

    `_id` is the source id. $min/$max ignore articles without a publication date.
    """
    pipeline = [
        {"$group": {
            "_id": "$source.id",
            "organization": {"$first": "$source.name"},
            "article_count": {"$sum": 1},
            "earliest_published_date": {"$min": "$published_date"},
            "latest_published_date": {"$max": "$published_date"},
            "first_collected_at": {"$min": "$first_seen_at"},
            "latest_fetch_at": {"$max": "$last_fetched_at"},
        }},
        {"$sort": {"article_count": -1, "_id": 1}},
    ]
    return list(collection.aggregate(pipeline, maxTimeMS=QUERY_TIMEOUT_MS))
