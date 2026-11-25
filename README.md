# SignalScout

SignalScout is a small **commercial-intelligence ingestion pipeline**. Every day it collects recent news
articles from organizations' public newsrooms, turns each one into the same validated document format,
and stores it in MongoDB Atlas without creating duplicates. The next step (not built yet) is a lightweight
MCP server so Claude can search these documents.

It's a student portfolio project: plain synchronous Python, a handful of files, no frameworks.
Working, simple and explainable came before clever.

## The problem

Teams that sell to or support organizations want to know what those organizations are doing: new
partnerships, technology adoption (for example AI), acquisitions, expansions, investments and leadership
changes. That information is spread across dozens of newsrooms, and each one has a different layout.
SignalScout:

1. **discovers** recent article URLs on each configured site,
2. **fetches** the pages politely (robots.txt, delays, limited retries),
3. **extracts** the title, text and metadata with site-independent tools,
4. **normalizes and validates** everything into one schema (Pydantic),
5. **exports** the documents to `output/articles.json` for inspection,
6. **upserts** them into MongoDB, keyed by canonical URL, so daily runs never create duplicates.

Healthcare organizations are the demo dataset, but nothing in the code is healthcare-specific.
A new website is usually just a new entry in `sources.json`.

## How the pipeline works

```text
sources.json      per-site settings: start URL, discovery method, selectors, content scope
     │
     ▼
discover.py       RSS feed / HTML listing page / XML sitemap  ->  list of article URLs
     │
     ▼
web.fetch()       robots.txt check, per-host delay, at most 2 retries  ->  HTML
     │
     ▼
extract.py        Trafilatura + BeautifulSoup  ->  title, text, date, author, canonical URL, ...
     │
     ▼
models.py         Pydantic Article: excerpt, SHA-256 content hash, keyword tags
     │
     ├──────────► output/articles.json   (local export, always written first)
     ▼
database.py       upsert by canonical_url into MongoDB Atlas (signalscout.articles)
```

| File | What it does |
|---|---|
| `sources.json` | One entry per website: start URL, allowed domains, discovery method, optional CSS selectors, content scope, optional date tag (`date_meta`), notes from inspecting the site. |
| `web.py` | The only code that downloads pages. Checks robots.txt, waits between requests to the same host (honouring `Crawl-delay`), retries timeouts/429/5xx at most twice, respects `Retry-After`, never retries 403/404. Also URL normalization and domain checks. |
| `discover.py` | Turns a source's start page into a list of article URLs: `rss` (feedparser), `html` (links on a listing page, optional CSS selector) or `sitemap` (newest `lastmod` first). Filters by domain, URL pattern and file type, removes duplicates, applies the per-source limit. |
| `extract.py` | Turns article HTML into title, text, description, date, author, language and canonical URL. Trafilatura does the generic work; BeautifulSoup handles canonical URL, language, fallbacks and per-site `remove_selectors`. Rejects empty pages, bot-check pages and stubs. |
| `models.py` | The `Article` Pydantic model (the common format), plus helpers: excerpt creation, SHA-256 content hash and simple keyword tags. |
| `database.py` | MongoDB connection, idempotent index creation, the insert/update logic and keyword search. |
| `ingest.py` | The command-line pipeline: runs everything above, writes the JSON export, saves to MongoDB and prints a summary. |
| `search.py` | Keyword search and per-source counts over the stored documents (the query a future MCP tool would run). |
| `test_connection.py` | Pings MongoDB Atlas and explains common failures (DNS, authentication, IP access list). |
| `tests/test_pipeline.py` | pytest tests (offline), plus one opt-in test against a separate Atlas collection. |

## Supported sources

All sites were inspected by hand first: HTML, RSS feeds, sitemaps, robots.txt and terms of use.

| Source | Discovery | Why this method |
|---|---|---|
| Cleveland Clinic | `html`: `newsroom.clevelandclinic.org/news-releases` | No RSS feed (404); the listing is server-rendered. robots.txt asks for a 10 s crawl delay. |
| Stanford Medicine | `html`: `med.stanford.edu/news.html`, selector `a.cmp-teaser__link` | No feed or sitemap; `all-news.html` is filled in by JavaScript, but `news.html` has a server-rendered "Latest Articles" list. 7 s crawl delay. |
| Mass General Brigham | `sitemap`: `news.massgeneralbrigham.org/en/sitemap.xml` | The homepage list is built by JavaScript, but robots.txt advertises a sitemap and article pages are server-rendered. Dates come from the page's `articlepublishdate` tag (see the document format section). |
| Intermountain Health | `rss`: `news.intermountainhealth.org/feed/` | Public RSS feed allowed by robots.txt (5 s crawl delay). |
| Vanderbilt University Medical Center | `rss`: `news.vumc.org/feed/` | Public RSS feed allowed by robots.txt. |

Three candidate sites block automated access. They stay in `sources.json` with `"enabled": false` and a note
explaining why, and **no attempt was made to get around the blocks**:

- **Mayo Clinic**: HTTP 403 "Access Denied" for every URL, including feeds and robots.txt.
- **Johns Hopkins Medicine**: Cloudflare JavaScript challenge ("Just a moment...").
- **UNC Health**: the RSS feed is open, but article pages return a 403 bot challenge.

Intermountain Health and VUMC were chosen as permitted replacements.

### Full text vs. excerpt

Every enabled site's terms of use restrict reproducing its content (for example "personal, non-commercial
use only", or "cannot be reused without permission"). So each source is set to `"content_scope": "excerpt"`.
SignalScout stores the title, the site's own meta description, a short lead excerpt (about 500 characters),
the metadata and the link to the original. The full text is only used in memory to compute the content hash,
the tags and the word count. A source whose terms allow it (for example a public-domain government site) can
be set to `"full"`. This is my reading of the terms for a learning project, not legal advice.

## Setup

The project uses the existing conda environment `ml` (Python 3.10). The missing packages were installed with
pip, constrained so that **no package already in the environment was upgraded or changed**:

```bash
conda run -n ml python -m pip install -r requirements.txt
```

### Environment variables (`.env`)

| Variable | Required | Meaning |
|---|---|---|
| `MONGODB_URI` | yes | Atlas connection string, `mongodb+srv://<username>:<password>@<cluster>.mongodb.net/`. The only variable the code reads. |
| `MONGODB_USERNAME`, `MONGODB_PASSWORD` | no | Created by the Atlas onboarding download. Not used, since the URI already contains them. |

`.env` is listed in `.gitignore` and must never be committed; `.env.example` shows the format.
In Atlas, your machine's public IP must be on the **Network Access** list. Otherwise the TLS handshake is
rejected and `test_connection.py` reports a timeout.

## Usage

```bash
# 1. Check the MongoDB connection (prints only success/failure, never the connection string)
conda run -n ml python test_connection.py

# 2. Dry run: discover + extract + validate, write output/articles.json, do NOT touch MongoDB
conda run -n ml python ingest.py --dry-run --limit 5

# 3. Same, for one source only
conda run -n ml python ingest.py --source cleveland_clinic --dry-run --limit 5

# 4. Full daily run: everything above, then upsert into MongoDB (default 20 articles per source)
conda run -n ml python ingest.py

# 5. Load a JSON export into MongoDB without scraping again (e.g. after a database outage)
conda run -n ml python ingest.py --from-json output/articles.json

# 6. Keyword search and per-source counts
conda run -n ml python search.py "artificial intelligence"
conda run -n ml python search.py --stats

# 7. Tests
conda run -n ml python -m pytest -v
```

`conda run` prints the output when the command finishes. Add `--no-capture-output` after `conda run` to see
log lines live.

Summary printed by the first real run (2026-11-24, default limit of 20 per source):

```text
SignalScout ingestion completed

Sources processed:       5
URLs discovered:        78
Valid documents:        78
Inserted:               78
Updated:                 0
Unchanged:               0
Duplicates skipped:      0
Extraction failed:       0
Database failures:       0

Per source                discovered  valid  failed
  cleveland_clinic                10     10       0
  stanford_medicine                8      8       0
  mass_general_brigham            20     20       0
  intermountain_health            20     20       0
  vanderbilt_health               20     20       0

Finished in 369 s
```

Cleveland Clinic and Stanford show fewer than 20 because their listing pages only contain 10 and 8 articles.

## The document format

Every document has the same shape, whichever website it came from (this example is the fictional test fixture):

```json
{
  "schema_version": 1,
  "canonical_url": "https://news.example-health.org/2026/11/20/ai-imaging-center",
  "source": {"id": "example_health", "name": "Example Health", "domain": "news.example-health.org", "industry": "healthcare"},
  "document_type": "article",
  "title": "Example Health Opens AI-Assisted Imaging Center",
  "description": "The new center uses machine learning tools to help radiologists review urgent scans faster.",
  "content": "SPRINGFIELD — Example Health today opened a new imaging center that uses artificial intelligence ...",
  "content_scope": "excerpt",
  "published_date": "2026-11-20",
  "author": "Jordan Lee",
  "language": "en",
  "tags": ["ai", "expansion"],
  "organizations": [],
  "content_hash": "70631d786da09ff33b2ebff6df20dc7a629747ee534c9e45a2f006bd1ce1520e",
  "metadata": {"extraction_method": "trafilatura", "discovery_method": "html",
               "fetched_url": "https://news.example-health.org/2026/11/20/ai-imaging-center", "word_count": 167}
}
```

- Required: `canonical_url`, `source`, `title` and `content` (at least 200 characters). Everything else may be `null`.
- `published_date` comes only from the page's markup (meta tags, JSON-LD, `<time>`) or URL. It is never
  guessed and never set to today. Trafilatura's default "extensive" date search was switched off after it
  returned `2000-01-01` for a bot-check page.
- A sitemap's `lastmod` is only used to decide which URLs to fetch. It is never stored as a date.
- If a site's generic date tag is wrong, its `sources.json` entry can name the one meta tag that holds the real
  publication date (`"date_meta"`). MGB needs this: its `published-time` tag is the CMS re-publish time (for
  example 2026-09-03 for an article written on 2026-05-20), so MGB dates come from `articlepublishdate`, and
  pages without that tag get `null`.
- `tags` come from simple keyword rules applied to the headline, description and lead only. An empty list is
  normal. `organizations` is reserved for future entity extraction.
- MongoDB adds `first_seen_at`, `last_fetched_at` and `last_updated_at` (UTC). They're kept out of the model
  so extraction and storage stay separate.
- `schema_version` and the free-form `metadata` dict let fields be added later without rewriting old documents.

## Deduplication and updates

**Identity = normalized canonical URL.** The page's `<link rel="canonical">` (or `og:url`) is used when it's
trustworthy: an http(s) URL on the source's allowed domains that isn't the home or listing page. Otherwise the
URL we were redirected to is used. Normalization lowercases the host, drops `#fragments`, default ports and
tracking parameters (`utm_*`, `gclid`, `fbclid`, ...), and keeps every other query parameter and trailing slash.
MongoDB enforces it with a unique index on `canonical_url`.

**Change detection = content hash.** SHA-256 of the normalized (lowercase, whitespace-collapsed) title and full
article text. Each run does a `find_one` by URL, then one `update_one(..., upsert=True)`:

| Case | Result |
|---|---|
| URL not stored yet | **inserted**: all fields + `first_seen_at`, `last_fetched_at`, `last_updated_at` |
| URL stored, hash differs | **updated**: new fields and hash, `last_updated_at` and `last_fetched_at` refreshed, `first_seen_at` kept |
| URL stored, hash equal | **unchanged**: `last_fetched_at` refreshed; other metadata (e.g. a fixed description) updated |

- `first_seen_at` is written with `$setOnInsert`, so it can never change after insertion.
- Empty values (`null`, `""`, `[]`) are never written over stored values. A run where a page's author couldn't
  be read doesn't erase the author an earlier run found.
- Nothing is ever deleted automatically. Two different URLs with identical content are both kept, since the
  hash is only used to detect changes, not to merge documents.
- Inside a single run, two URLs that resolve to the same canonical URL are collapsed ("Duplicates skipped").

Indexes (created idempotently on every run): unique `canonical_url`, `published_date`, `source.id`, and a
text index over `title` (weight 3), `description` (2) and `content` (1). Description is included because most
sources only allow storing an excerpt.

## Error handling

| Problem | What happens |
|---|---|
| Timeout / connection error / HTTP 429 / 5xx | Retried at most twice, honouring `Retry-After` (gives up if asked to wait > 120 s). |
| HTTP 403 / 404 / other 4xx | Not retried. On 403 or 429 the rest of that source is skipped for this run. |
| robots.txt disallows a URL | Not fetched. If robots.txt itself is forbidden/unreachable, the site is skipped. |
| Redirect to another domain, non-HTML response | Skipped and logged. |
| Empty page, bot-check page, too little text | Logged as "Extraction failed" and skipped. |
| Missing date/author/description | Stored as `null`, never invented. |
| A site returns zero article URLs | Warning in the log ("has the site layout changed?"); other sources continue. |
| One article or source crashes unexpectedly | Logged with a traceback; the run continues. |
| MongoDB unreachable / a write fails | Logged clearly and counted under "Database failures". The documents remain in `output/articles.json` and can be loaded later with `--from-json`. |

## Adding a new website

1. Inspect it: is there an RSS feed (`<link type="application/rss+xml">`, `/feed`)? Is the listing page's HTML
   server-rendered? Does robots.txt allow it, and does it list a sitemap? What do the terms of use allow?
2. Add an entry to `sources.json`:

   ```json
   {
     "id": "example_org",
     "name": "Example Organization",
     "industry": "healthcare",
     "start_url": "https://news.example.org/feed/",
     "allowed_domains": ["news.example.org"],
     "discovery": {"method": "rss", "link_selector": null, "url_pattern": null},
     "extraction": {"method": "auto", "content_scope": "excerpt", "remove_selectors": []},
     "enabled": true,
     "notes": "What you found while inspecting the site."
   }
   ```

   Prefer `rss`, then `html` (add a `url_pattern` regex and/or a `link_selector` if the page has many
   unrelated links), then `sitemap`. Only add `remove_selectors` if generic extraction keeps boilerplate. And
   check that it removes only boilerplate: on MGB, the obvious caption selector also deleted real body text.
   If the extracted dates don't match the dates the site shows, add `"date_meta": "<meta tag name>"` to
   `extraction`.
3. Try it: `conda run -n ml python ingest.py --source example_org --dry-run --limit 5`, then compare
   `output/articles.json` with the real pages.

## Testing

`conda run -n ml python -m pytest -v` runs 43 offline test cases (no network, no database; the opt-in Atlas
test below shows as skipped) covering URL normalization, domain checks, duplicate URLs, RSS/HTML/sitemap
discovery, extraction from a saved HTML fixture, site-specific date tags, missing optional metadata,
empty/bot-check pages, Pydantic validation, excerpts, content-hash
consistency, keyword tags, the MongoDB insert/update logic (with a mocked collection) and the HTTP retry rules.
The fixture in `tests/fixtures/article.html` is fictional, so no copyrighted page is stored in the repo.

One opt-in test runs the same insert/update rules against Atlas, using a separate collection
(`articles_test`) that it drops afterwards:

```bash
SIGNALSCOUT_DB_TESTS=1 conda run -n ml python -m pytest -v -k real_mongodb
```

### Integration checks on the real sites and Atlas (2026-11-24)

| Check | Result |
|---|---|
| Dry run, 5 per source | 25/25 valid, 0 failures. Records compared with the live pages (title, date, author, description, canonical URL). |
| First full run (20 per source) | 78 URLs discovered, 78 valid, 78 inserted, 0 extraction or database failures. |
| Second full run | 0 inserted, 0 updated, **78 unchanged**. Still 78 documents and 78 distinct canonical URLs. |
| `first_seen_at` | Unchanged for all 78 documents after the second run; `last_fetched_at` advanced for all 78. |
| Changed content | An extraction fix changed one MGB article's text, so the next MGB run reported **1 updated, 19 unchanged**. That document kept its `first_seen_at` and got a new `last_updated_at`. |
| Keyword search | `search.py partnership` returned the Cleveland Clinic–UTEC partnership, the AdventHealth–Intermountain joint-venture letter of intent and a Cleveland Clinic acquisition. |
| MongoDB outage (fake URI) | One clear error line, "Database failures: 20", exit code 1, documents still in the JSON file, no credentials printed. |
| Opt-in Atlas test | Passed (insert → unchanged → updated on `articles_test`, which is then dropped). |
| Final verification run | 78 discovered, **1 inserted** (an MGB article published that day), 77 unchanged, 0 failures. 79 documents and 79 distinct canonical URLs; `first_seen_at` unchanged for all 78 older documents. |
| Publication dates | Match the date in the URL for Cleveland Clinic 10/10, VUMC 20/20, Stanford 8/8 (year-month). Intermountain matches its RSS feed 19/20 (the other is the timezone case in Known limitations). MGB matches `articlepublishdate` 21/21 and never the sitemap `lastmod`. |
| Keyword search topics | `partnership` and `investment` return relevant articles. `expansion` returns nothing because of stemming (`expand` finds 9). `AI` returns nothing because no stored excerpt contains the word (`"artificial intelligence"` finds 1; 2 documents are tagged `ai`). No article mentions automation. |
| Data completeness | 79 documents: 76 have a published date (3 MGB pages declare none), 79 a language, 74 a description, 37 an author (MGB and Intermountain pages don't declare authors in their metadata). |

### Problems found by checking real output (and fixed)

- Trafilatura's default date search returned **`2000-01-01`** for a bot-check page, so dates now come from the
  page markup only.
- On one Stanford article with no declared author, the **media-contact box** ("Media Contact ... Tel ...") was
  taken as the author. Fixed with a `remove_selectors` entry plus a generic guard against contact details.
- MGB titles ended in **"| Mass General Brigham"**. Site-name suffixes are now stripped generically.
- An MGB page leaked a site-builder config value (a bare **`false`**) into the text. Such lines are now dropped.
- Keyword false positives ("**acquired** resistance" → acquisition, "holiday **gifts**" → investment) led to
  tighter rules.
- A tempting MGB selector for image captions turned out to **delete real body text** (word counts fell from
  897 to 593), so it was reverted. Comparing before/after word counts caught it.
- **MGB dates were re-publish dates, not publication dates.** The page's `published-time` tag was used, but it
  changes whenever the page is re-published (for example 2026-09-03 for an article written on 2026-05-20). MGB
  now uses `articlepublishdate` (`date_meta`). Because stored values are never overwritten with `null`, the
  20 already-stored MGB dates were reset once, and the next run filled in the correct ones.

## Scheduling a daily run

Run it by hand until you're happy, then schedule it. The scheduler is not set up automatically.

**Linux/macOS (cron).** `crontab -e` and add (runs at 07:00 every day; replace `/path/to/signalscout`
and check where your conda lives with `which conda`):

```cron
0 7 * * * cd /path/to/signalscout && mkdir -p logs && $HOME/miniconda3/bin/conda run -n ml python ingest.py >> logs/ingest.log 2>&1
```

Use the full path to `conda` because cron runs with a minimal `PATH`.

**Windows (Task Scheduler).** Create Basic Task → Daily → Action "Start a program":
Program `C:\Users\<you>\miniconda3\Scripts\conda.exe`, arguments `run -n ml python ingest.py`,
"Start in" = the project folder.

**Missed runs.** cron doesn't run jobs while the computer is off or asleep. The next run still picks up
whatever is currently in each source's window (about 20–25 items in the RSS feeds, about 8–10 on the HTML
listing pages, the 20 most recently modified sitemap entries), so a gap of a few days is usually covered, but
SignalScout can't guarantee complete history after a long gap. (`anacron` or a systemd timer with
`Persistent=true` can catch up missed runs.)

## Known limitations

- **Small window per site.** No pagination: each run sees only what is on the first listing page/feed. The
  Cleveland Clinic and Stanford listings show only about 8–10 articles.
- **Excerpts only.** Because of the sites' terms, keyword search covers the title, description and excerpt,
  not the full article. Tags are computed from a longer lead, so a document can be tagged `ai` without the word
  appearing in its stored text. To find topics, query the `tags` field as well as the text.
- **Keyword search is stemmed, not semantic.** MongoDB's English stemmer turns "expansion" into *expans* and
  "expands" into *expand*, so a search for `expansion` doesn't find "expands" (search `expand` instead).
  Synonyms (AI vs. "machine learning") aren't matched either.
- **Dates are taken as the page writes them.** Intermountain's JSON-LD writes times with a +01:00 offset, so a
  post from 17:30 Mountain Time on Oct 27 is stored as Oct 28 (1 of 20 Intermountain articles checked against
  the RSS feed). MGB articles without an `articlepublishdate` tag have no date.
- **Site-specific settings can break.** CSS selectors (`a.cmp-teaser__link`, `p.italic.text-rem13px`) depend on
  the sites' current HTML. If a layout changes, discovery may return zero URLs (logged as a warning) or an
  excerpt may contain a caption.
- **Heuristics are heuristics.** Keyword tags have false positives and negatives. Authors come from page
  metadata, and some sites list the organization itself as the author. MGB's sitemap `lastmod` is a
  modification date, so an old article that was edited can be among the URLs fetched as "recent" (its stored
  date is still its real publication date).
- **Titles follow structured metadata.** If a page's `og:title`/JSON-LD headline differs from its visible
  `<h1>` (seen on one Intermountain page), the metadata title is used.
- **robots.txt wildcards.** Python's built-in robots.txt parser ignores `*` wildcards inside paths. None of
  the article URLs used here are affected.
- **Sequential and slow on purpose.** Crawl delays make a full run take several minutes. That's fine for a
  daily job.
- **Not production-grade.** No monitoring, alerting, job queue or concurrency control. Two runs at the same
  time could race (the unique index still prevents duplicate documents).

## Next step: MCP integration

The pipeline is ready to sit behind an MCP server. The plan:

- A small Python MCP server (hosted on Render) that imports `database.py` and exposes a few read-only tools,
  e.g. `search_articles(query, limit)` (already implemented as the MongoDB text search used by `search.py`),
  `recent_articles(source_id, since)` (uses the `source.id` and `published_date` indexes) and
  `get_article(canonical_url)`.
- Claude gets titles, descriptions, excerpts, tags and links, and can cite and open the original article.
- Retrieved text is untrusted web content, so the server should label it as data, not instructions.
- The daily ingestion keeps running separately (cron), and Atlas Network Access must also allow Render's
  outbound IPs.
