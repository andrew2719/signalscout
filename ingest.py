"""SignalScout ingestion pipeline.

  discover URLs -> fetch pages -> extract -> validate (Pydantic) -> export JSON -> save to MongoDB

Usage:
  conda run -n ml python ingest.py --dry-run --limit 5                 # no database, JSON export only
  conda run -n ml python ingest.py --source cleveland_clinic --dry-run --limit 5
  conda run -n ml python ingest.py                                      # daily run (20 per source)
  conda run -n ml python ingest.py --from-json output/articles.json     # load an export into MongoDB
"""
import argparse
import json
import logging
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from pymongo.errors import ConnectionFailure, PyMongoError

from database import ensure_indexes, get_collection, save_article
from discover import DiscoveryError, discover_urls
from extract import ExtractionError, extract_article
from models import Article, build_article
from web import FetchError, fetch, is_allowed_domain

PROJECT_DIR = Path(__file__).parent
SOURCES_FILE = PROJECT_DIR / "sources.json"
DEFAULT_OUTPUT = PROJECT_DIR / "output" / "articles.json"
DEFAULT_LIMIT = 20

log = logging.getLogger("ingest")


def load_sources(source_id: str | None = None) -> list[dict]:
    """Enabled sources from sources.json, or only the one requested with --source."""
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
    if source_id is None:
        return [s for s in sources if s.get("enabled", True)]
    for source in sources:
        if source["id"] == source_id:
            if not source.get("enabled", True):
                raise SystemExit(f"Source {source_id!r} is disabled in sources.json: {source.get('notes', '')}")
            return [source]
    raise SystemExit(f"Unknown source {source_id!r}. Known sources: {', '.join(s['id'] for s in sources)}")


def collect_source(source: dict, limit: int, seen_urls: set[str], counts: Counter) -> list[dict]:
    """Discover, fetch, extract and validate up to `limit` articles from one source."""
    sid = source["id"]
    try:
        urls = discover_urls(source, limit)
    except (FetchError, DiscoveryError) as error:
        counts["source_errors"] += 1
        log.error("[%s] discovery failed: %s", sid, error)
        return []
    except Exception:  # unexpected page structure or a bug: log it and move on to the next source
        counts["source_errors"] += 1
        log.exception("[%s] unexpected discovery error", sid)
        return []

    counts["urls_discovered"] += len(urls)
    if urls:
        log.info("[%s] discovered %d article URLs", sid, len(urls))
    else:
        log.warning("[%s] discovery returned zero article URLs (has the site layout changed?)", sid)

    documents = []
    for url in urls:
        try:
            article = fetch_and_extract(url, source)
        except FetchError as error:
            counts["extraction_failed"] += 1
            log.warning("[%s] could not fetch %s: %s", sid, url, error)
            if error.status in (403, 429):
                log.warning("[%s] the site is blocking or rate-limiting us, skipping its remaining URLs", sid)
                break
            continue
        except (ExtractionError, ValidationError) as error:
            counts["extraction_failed"] += 1
            log.warning("[%s] skipped %s: %s", sid, url, describe(error))
            continue
        except Exception:
            counts["extraction_failed"] += 1
            log.exception("[%s] unexpected error for %s", sid, url)
            continue

        if article.canonical_url in seen_urls:
            counts["duplicates"] += 1
            log.info("[%s] duplicate of an article already collected: %s", sid, article.canonical_url)
            continue
        seen_urls.add(article.canonical_url)
        counts["valid"] += 1
        documents.append(article.model_dump(mode="json"))
        log.info("[%s] ok: %s", sid, article.title[:90])
    return documents


def fetch_and_extract(url: str, source: dict) -> Article:
    response = fetch(url)
    if not is_allowed_domain(response.url, source["allowed_domains"]):
        raise ExtractionError(f"redirected outside the allowed domains to {response.url}")
    content_type = response.headers.get("Content-Type", "")
    if "html" not in content_type.lower():
        raise ExtractionError(f"not an HTML page ({content_type or 'no Content-Type'})")
    extracted = extract_article(response.text, response.url, source)
    return build_article(source, extracted)


def save_to_mongodb(documents: list[dict], counts: Counter) -> bool:
    """Upsert every document. Returns False if MongoDB could not be reached at all."""
    try:
        collection = get_collection()
        collection.database.client.admin.command("ping")
        ensure_indexes(collection)
    except (PyMongoError, RuntimeError) as error:
        counts["database_failures"] += len(documents)
        log.error("MongoDB unavailable (%s): nothing was saved, but the %d documents are in the JSON file. "
                  "Run test_connection.py for a diagnosis.", type(error).__name__, len(documents))
        return False

    for index, document in enumerate(documents):
        try:
            counts[save_article(collection, document, datetime.now(timezone.utc))] += 1
        except ConnectionFailure as error:  # the connection dropped: don't wait 10 s for every remaining write
            not_saved = len(documents) - index
            counts["database_failures"] += not_saved
            log.error("Lost the MongoDB connection (%s); %d documents not saved but kept in the JSON export.",
                      type(error).__name__, not_saved)
            break
        except PyMongoError as error:
            counts["database_failures"] += 1
            log.error("MongoDB write failed for %s: %s", document["canonical_url"], str(error)[:200])
    collection.database.client.close()
    return True


def load_json_documents(path: Path, counts: Counter) -> list[dict]:
    """Re-validate documents from a JSON export (used by --from-json)."""
    documents = []
    for item in json.loads(path.read_text(encoding="utf-8")):
        try:
            documents.append(Article.model_validate(item).model_dump(mode="json"))
        except ValidationError as error:
            counts["invalid_in_file"] += 1
            log.warning("Skipping invalid document in %s: %s", path, describe(error))
    counts["valid"] = len(documents)
    return documents


def write_json(documents: list[dict], path: Path) -> None:
    """Write the export atomically, so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(documents, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def describe(error: Exception) -> str:
    """One-line error description (Pydantic's default message spans several lines)."""
    if isinstance(error, ValidationError):
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors())
    return str(error)


def print_summary(counts: Counter, per_source: dict[str, Counter], mode: str, seconds: float) -> None:
    title = {"dry-run": " (dry run: MongoDB not used)", "from-json": " (loaded from JSON export)"}.get(mode, "")
    print(f"\nSignalScout ingestion completed{title}\n")
    if mode == "from-json":
        rows = [("Valid documents", counts["valid"]), ("Invalid in file", counts["invalid_in_file"])]
    else:
        rows = [("Sources processed", counts["sources"]), ("URLs discovered", counts["urls_discovered"]),
                ("Valid documents", counts["valid"])]
    if mode != "dry-run":
        rows += [("Inserted", counts["inserted"]), ("Updated", counts["updated"]), ("Unchanged", counts["unchanged"])]
    if mode != "from-json":
        rows += [("Duplicates skipped", counts["duplicates"]), ("Extraction failed", counts["extraction_failed"])]
    if mode != "dry-run":
        rows.append(("Database failures", counts["database_failures"]))
    if counts["source_errors"]:
        rows.append(("Sources with errors", counts["source_errors"]))
    for label, value in rows:
        print(f"{label + ':':<21}{value:>5}")
    if per_source:
        print(f"\n{'Per source':<26}{'discovered':>10}{'valid':>7}{'failed':>8}")
        for source_id, c in per_source.items():
            print(f"  {source_id:<24}{c['urls_discovered']:>10}{c['valid']:>7}{c['extraction_failed']:>8}")
    print(f"\nFinished in {seconds:.0f} s")


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    for noisy in ("trafilatura", "htmldate", "courlan", "urllib3", "charset_normalizer", "pymongo"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SignalScout ingestion pipeline")
    parser.add_argument("--dry-run", action="store_true", help="discover and extract, write the JSON export, skip MongoDB")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"max articles per source (default {DEFAULT_LIMIT})")
    parser.add_argument("--source", help="run only this source id from sources.json")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="JSON export path (default output/articles.json)")
    parser.add_argument("--from-json", type=Path, metavar="FILE", help="skip scraping: validate a JSON export and save it to MongoDB")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    if args.from_json and args.dry_run:
        parser.error("--from-json writes to MongoDB, so it cannot be combined with --dry-run")
    setup_logging(args.verbose)
    started = time.monotonic()
    counts: Counter = Counter()

    if args.from_json:
        documents = load_json_documents(args.from_json, counts)
        database_ok = save_to_mongodb(documents, counts) if documents else False
        print_summary(counts, {}, "from-json", time.monotonic() - started)
        return 0 if database_ok else 1

    sources = load_sources(args.source)
    counts["sources"] = len(sources)
    per_source: dict[str, Counter] = {}
    seen_urls: set[str] = set()
    documents: list[dict] = []
    for source in sources:
        log.info("[%s] starting: %s discovery, limit %d", source["id"], source["discovery"]["method"], args.limit)
        source_counts: Counter = Counter()
        documents += collect_source(source, args.limit, seen_urls, source_counts)
        per_source[source["id"]] = source_counts
        counts.update(source_counts)

    if documents:
        write_json(documents, args.output)
        log.info("Wrote %d documents to %s", len(documents), args.output)
    else:
        log.warning("No valid documents this run; the previous JSON export (if any) was left untouched")

    database_ok = True
    if documents and not args.dry_run:
        database_ok = save_to_mongodb(documents, counts)
    print_summary(counts, per_source, "dry-run" if args.dry_run else "full", time.monotonic() - started)
    return 0 if documents and database_ok else 1


if __name__ == "__main__":
    sys.exit(main())
