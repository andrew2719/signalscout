"""MongoDB Atlas helpers: connect, create indexes, save articles, keyword search."""
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from pymongo import DESCENDING, TEXT, MongoClient
from pymongo.collection import Collection

DB_NAME = "signalscout"
COLLECTION_NAME = "articles"
ENV_FILE = Path(__file__).parent / ".env"  # explicit path so cron jobs work from any directory


def get_client() -> MongoClient:
    """Create a MongoDB client from MONGODB_URI in .env (the URI is never printed)."""
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


def search_articles(collection: Collection, query: str, limit: int = 5) -> list[dict]:
    """Keyword search with the text index, best matches first."""
    projection = {"_id": 0, "score": {"$meta": "textScore"}, "title": 1, "canonical_url": 1,
                  "published_date": 1, "source.name": 1, "description": 1, "tags": 1}
    cursor = collection.find({"$text": {"$search": query}}, projection)
    return list(cursor.sort([("score", {"$meta": "textScore"})]).limit(limit))


def count_by_source(collection: Collection) -> list[dict]:
    """Number of stored documents per source, largest first."""
    pipeline = [{"$group": {"_id": "$source.id", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}]
    return list(collection.aggregate(pipeline))
