"""Keyword search over the stored articles (MongoDB text index only).

The MCP server's search_articles tool builds on the same query and adds synonyms and tag matching.

  conda run -n ml python search.py "artificial intelligence"
  conda run -n ml python search.py partnership --limit 10
  conda run -n ml python search.py --stats
"""
import argparse
import sys

from pymongo.errors import PyMongoError

from database import get_collection, search_articles, source_stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Search SignalScout articles stored in MongoDB")
    parser.add_argument("query", nargs="?", help="keywords; use quotes for a phrase, e.g. '\"heart failure\"'")
    parser.add_argument("--limit", type=int, default=5, help="number of results (default 5)")
    parser.add_argument("--stats", action="store_true", help="show the number of stored documents per source")
    args = parser.parse_args()
    if not args.query and not args.stats:
        parser.error("give a search query or --stats")

    try:
        collection = get_collection()
        if args.stats:
            rows = source_stats(collection)
            for row in rows:
                print(f"{row['_id']:<26}{row['article_count']:>5}")
            print(f"{'total':<26}{sum(row['article_count'] for row in rows):>5}")
        if args.query:
            results = search_articles(collection, args.query, args.limit)
            print(f"{len(results)} result(s) for {args.query!r}\n")
            for doc in results:
                print(f"[{doc['score']:.2f}] {doc.get('published_date') or 'no date'} | {doc['source']['name']}")
                print(f"  {doc['title']}")
                print(f"  {doc['canonical_url']}")
                print(f"  tags: {', '.join(doc.get('tags', [])) or '-'}\n")
    except (PyMongoError, RuntimeError) as error:
        print(f"MongoDB error ({type(error).__name__}). Run test_connection.py for a diagnosis.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
