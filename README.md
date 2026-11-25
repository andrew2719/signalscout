# SignalScout

SignalScout is a small **commercial-intelligence pipeline with a Claude connector**:

1. An **ingestion pipeline** (runs daily on a laptop) collects recent news articles from organizations'
   public newsrooms, turns each one into the same validated document format, and stores it in MongoDB
   Atlas without creating duplicates.
2. A read-only **MCP server** (hosted on Render) lets Claude search those articles through four tools and cite
   the original sources.

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

## Architecture

```text
 Laptop (daily cron)                     MongoDB Atlas                     Render (free web service)
┌──────────────────────┐   write user   ┌──────────────────────┐  read-only  ┌───────────────────────┐
│ ingest.py            │ ─────────────► │ signalscout.articles │ ◄────────── │ server.py (MCP)       │
│ discover → fetch →   │  upsert by     │ 79 articles, indexes │    user     │ 4 read-only tools     │
│ extract → validate   │  canonical_url └──────────────────────┘             │ /mcp  and  /health    │
└──────────────────────┘                                                     └───────────▲───────────┘
                                                                                         │ HTTPS (Streamable HTTP)
                                                                             ┌───────────┴───────────┐
                                                                             │ Claude (connector)    │
                                                                             │ summarises, reasons,  │
                                                                             │ cites source URLs     │
                                                                             └───────────────────────┘
```

The two halves only meet in the database. The laptop holds the write credentials and runs ingestion; the
MCP server only reads, with a separate read-only database user, and never runs ingestion. Claude does the
summarising and commercial reasoning; the server only retrieves data.

The ingestion pipeline in detail:

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
| `database.py` | MongoDB connection, plus a write side used only by ingestion (idempotent indexes, insert/update logic) and a read side used by the MCP server (search, tag, source, date and coverage queries, each with a 5 s time limit). |
| `ingest.py` | The command-line pipeline: runs everything above, writes the JSON export, saves to MongoDB and prints a summary. |
| `search.py` | Command-line keyword search (plain MongoDB text search) and per-source counts. |
| `server.py` | The MCP server: four read-only tools, the `/health` endpoint and the Render settings (port, host validation). |
| `check_mcp.py` | Calls every MCP tool through a real MCP client, against the local or the deployed server. |
| `test_connection.py` | Pings MongoDB Atlas and explains common failures (DNS, authentication, IP access list). |
| `tests/test_pipeline.py` | pytest tests for the pipeline (offline), plus one opt-in test against a separate Atlas collection. |
| `tests/test_server.py` | pytest tests for the MCP server (offline, through an in-memory MCP client), plus two opt-in tests with real data. |
| `.python-version` | The Python version Render uses (3.12). |

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

### Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `MONGODB_URI` | yes | Atlas connection string, `mongodb+srv://<username>:<password>@<cluster>.mongodb.net/`. Locally it comes from `.env` (the read-write user, for ingestion); on Render it is set in the dashboard (the read-only user). A real environment variable takes priority over `.env`. |
| `PORT` | no | Port for `server.py`; default 8000. Render sets it. |
| `RENDER_EXTERNAL_HOSTNAME` | no | Set by Render to the service's `onrender.com` hostname; `server.py` adds it to the allowed hosts. |
| `SIGNALSCOUT_DB_TESTS` | no | Set to `1` to run the opt-in tests against Atlas. |
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

## The MCP server (Claude connector)

`server.py` exposes four **read-only** tools over MCP's Streamable HTTP transport at `/mcp`, built with the
official MCP Python SDK (`mcp==2.3.0`, where the high-level server class is `MCPServer`):

| Tool | What it returns |
|---|---|
| `search_articles(query, limit=10)` | Articles matching keywords or a topic: MongoDB text search over titles, descriptions and excerpts **plus** the keyword tags assigned at ingestion, with simple synonyms (AI / artificial intelligence / machine learning, expansion / expand, partnership / partner / joint venture, acquisition / merger, investment / funding). |
| `get_organization_news(organization, limit=10)` | One organization's newest articles. Accepts a name, source id or initials in any case (`Cleveland Clinic`, `cleveland_clinic`, `MGB`); an unknown name returns the list of available organizations. |
| `get_recent_articles(days=30, limit=10)` | Articles **published** in the last N days across all sources, by publication date (not ingestion time). Articles without a known date are excluded. |
| `get_data_coverage()` | Total articles, organizations with per-source counts and date ranges, the latest publication date, the latest successful fetch, and a plain statement of what the data does not cover. |

Every article comes back in the same JSON shape, always with its source URL so Claude can cite it
(fictional example):

```json
{
  "title": "Example Health Opens AI-Assisted Imaging Center",
  "organization": "Example Health",
  "source_id": "example_health",
  "published_date": "2026-11-20",
  "description": "The new center uses machine learning tools to help radiologists review urgent scans faster.",
  "excerpt": "SPRINGFIELD — Example Health today opened a new imaging center ...",
  "content_scope": "excerpt",
  "tags": ["ai", "expansion"],
  "url": "https://news.example-health.org/2026/11/20/ai-imaging-center",
  "matched_by": ["text", "tags"]
}
```

`matched_by` appears only in search results. MongoDB ids, content hashes and ingestion timestamps are never
returned. Each response is both structured content and a JSON text copy.

**How search works.** The query is reduced to plain words and matched against a small synonym table to find
topics. Then two queries run: a MongoDB text search for the words plus single-word synonyms (`expansion` also
searches `expand`, which the stemmer treats as a different word), and a tag query for the topics (which finds
AI articles even when the stored excerpt never says "AI"). Text matches come first, in MongoDB's own relevance
order, then tag-only matches. Duplicates are removed by URL, `matched_by` says why each article was returned,
and no relevance scores are invented. Measured on the real data (79 articles, limit 20):

| Query | Plain MongoDB text search | `search_articles` tool |
|---|---|---|
| AI | 0 | 2 |
| artificial intelligence | 1 | 2 |
| expansion | 0 | 15 |
| partnership | 3 | 7 |
| investment | 3 | 11 |
| acquisition | 1 | 2 |
| automation | 0 | 0 (no article mentions it) |

**Safety and limits.**

- **Read-only.** The server only runs `find`, `aggregate` and `count_documents`. It never inserts, updates,
  deletes or creates indexes (a test records every command it sends to MongoDB), and it is meant to run with a
  read-only database user.
- **No authentication.** This is a public demonstration endpoint serving permitted public information
  (titles, short excerpts, links). Anyone who knows the URL can call the tools. Don't point it at private data.
- **User input never becomes a MongoDB query.** Search text is reduced to plain words (quotes, minus signs and
  operators removed), organization names are matched against the known list, and numbers are validated (limit
  1–20, days 1–365, query up to 200 characters). Every database query has a 5-second time limit (`maxTimeMS`).
- **Webpage content is untrusted.** Every response, and the server's MCP instructions, tell Claude to treat
  titles, descriptions and excerpts as data to analyse and cite, not as instructions.
- **Errors don't leak internals.** A database problem returns "The article database is temporarily
  unavailable"; connection details stay in the server log, and credentials are never logged.
- **Host validation.** `/mcp` only accepts requests addressed to localhost or the Render hostname
  (`RENDER_EXTERNAL_HOSTNAME`, set by Render); anything else gets HTTP 421 (protection against DNS rebinding).
  `/health` has no such check, so Render's health checker always reaches it.
- **Resilient startup.** One MongoDB client per process, created at the first tool call. A MongoDB outage never
  stops the HTTP server from starting, and the next tool call retries.

**Run it locally.**

```bash
conda run -n ml python server.py      # MCP endpoint http://localhost:8000/mcp, MONGODB_URI from .env
curl http://localhost:8000/health     # {"status":"ok"}
conda run -n ml python check_mcp.py   # in a second terminal: lists and calls all four tools
```

`conda run` shows the server log only when it stops; use `conda run --no-capture-output ...` to see it live.
The server listens on `0.0.0.0` (Render needs that), but host validation only accepts requests addressed to
localhost.

To try the read-only user locally before deploying (the connection string is not echoed or saved in your shell
history; an environment variable takes priority over `.env`):

```bash
read -rs MONGODB_URI && export MONGODB_URI     # paste the read-only connection string, press Enter
conda run -n ml python server.py
```

## Deploying to Render

### 1. Create a read-only MongoDB user

In Atlas: **Security → Database & Network Access → Database Users → Add New Database User**.

- Authentication method **Password**; username for example `signalscout_reader`; **Autogenerate Secure Password**.
- Privileges: expand **Specific Privileges**, add role **`read`** on database **`signalscout`**, and remove the
  default built-in role. (If your cluster doesn't offer specific privileges, use the built-in role
  **Only read any database**.)
- Optionally **Restrict Access to Specific Clusters** to this cluster.

Its connection string is the one from **Connect → Drivers**, with this user's name and password. The laptop's
`.env` keeps the existing read-write user for ingestion.

### 2. Create the web service

Render dashboard → **New → Web Service** → connect the GitHub repository:

| Setting | Value |
|---|---|
| Service type | Web Service |
| Repository / branch | `andrew2719/signalscout` / `main` |
| Runtime | Python (version from `.python-version`: 3.12) |
| Instance type | Free |
| Build command | `pip install -r requirements.txt` |
| Start command | `python server.py` |
| Health check path | `/health` |
| Environment variable | `MONGODB_URI` = the read-only connection string |

Render sets `PORT` (the server listens on it; local default 8000) and `RENDER_EXTERNAL_HOSTNAME` (added to the
allowed hosts) by itself. No `.env` file, disk, Docker image or background worker is used, and Render never
runs `ingest.py`. `requirements.txt` pins the versions tested locally; a pip dry run confirmed that all of them
install on Linux with Python 3.12 from prebuilt wheels.

### 3. Allow Render's outbound IPs in Atlas

Atlas only accepts connections from listed addresses. In Render, open the service → **Connect** (top right) →
**Outbound** tab and copy the IP ranges (they are shared by all Render services in the region). In Atlas →
**Security → Database & Network Access → IP Access List → Add IP Address**, add each range. Allowing
`0.0.0.0/0` (anywhere) also works but lets any address attempt to log in; only consider it with the read-only
user.

### 4. Verify the deployment

```bash
curl https://<your-service>.onrender.com/health
conda run -n ml python check_mcp.py https://<your-service>.onrender.com/mcp
```

If a tool says "temporarily unavailable", check the service's logs on Render: usually the Atlas IP access list
or the `MONGODB_URI` value.

**Free tier.** The service sleeps after 15 minutes without traffic and takes about a minute to wake, so the
first request after a pause is slow (open `/health` first) and may time out once. Free instances share a
monthly allowance of 750 instance hours per workspace.

## Connecting Claude

**Claude (web and desktop):** Customize → Connectors → **+ Add** → **Add custom connector** → name
`SignalScout`, URL `https://<your-service>.onrender.com/mcp` → Authentication **No sign in** → Add, then enable
it in a chat. Custom connectors work on Free (one connector), Pro, Max, Team and Enterprise plans; on Team and
Enterprise an owner adds it under Organization settings → Connectors. Claude connects from Anthropic's cloud,
so the server must be public, which it is on Render.

**Claude Code:**

```bash
claude mcp add --transport http signalscout https://<your-service>.onrender.com/mcp
claude mcp list    # shows ✔ Connected when it works
```

**Example commercial-intelligence questions:**

- "Using SignalScout, which healthcare organizations announced partnerships or joint ventures recently? Cite
  the sources."
- "What has Cleveland Clinic announced in the last month that a medical-technology vendor should know about?"
- "Find news about AI adoption at these health systems and summarise who is doing what."
- "Which organizations are expanding (new facilities, acquisitions, investments)? List them with dates and links."
- "What does the SignalScout database cover, and how recent is it?"

## Testing

`conda run -n ml python -m pytest -v` runs 75 offline test cases (no network, no database; the 3 opt-in tests
below show as skipped):

- `tests/test_pipeline.py` (43): URL normalization, domain checks, duplicate URLs, RSS/HTML/sitemap discovery,
  extraction from a saved HTML fixture, site-specific date tags, missing optional metadata, empty/bot-check
  pages, Pydantic validation, excerpts, content-hash consistency, keyword tags, the MongoDB insert/update logic
  (with a mocked collection) and the HTTP retry rules. The fixture in `tests/fixtures/article.html` is
  fictional, so no copyrighted page is stored in the repo.
- `tests/test_server.py` (32): query cleaning, synonym/topic detection, merging and de-duplication, the article
  shape (JSON-serializable, no internal fields), organization matching, all four tools through a real in-memory
  MCP client (results, empty results, invalid arguments, a database outage without leaked details), read-only
  database access, the `/health` endpoint and host validation.

Three opt-in tests use MongoDB Atlas:

```bash
SIGNALSCOUT_DB_TESTS=1 conda run -n ml python -m pytest -v
```

- the insert/update rules on a separate `articles_test` collection that is dropped afterwards;
- a real `server.py` process queried by a real MCP client over HTTP: real records, source URLs, tags beating
  plain text search, empty results, and a collection snapshot that is identical before and after;
- a recorder of every MongoDB command the four tools send, which must contain no writes.

### MCP server checks (2026-11-24, local server, real Atlas data)

| Check | Result |
|---|---|
| Real MCP client (`check_mcp.py`, Streamable HTTP) | Lists 4 tools; all calls succeed with real articles, each with its source URL. |
| Coverage | 79 articles, 5 organizations, published 2026-06-09 to 2026-11-24, latest fetch 2026-11-25 02:29 UTC. |
| Tags and synonyms | See the table above: `AI` 0 → 2 and `expansion` 0 → 15 compared with plain text search. |
| Edge cases | Blank query, `limit` 50 and `days` 0 return clear tool errors; an unknown organization lists the available ones; `MGB` resolves to Mass General Brigham; a nonsense query returns 0 articles with a hint. |
| No writes | The command recorder saw only `find`, `aggregate` and the login handshake; the collection snapshot (every document's hash and fetch time, plus the index list) was unchanged. |
| HTTP | `/health` returns 200 for any Host header; `/mcp` returns 200 for `localhost` and the Render hostname and 421 for a foreign Host. |
| Render dependencies | `pip install --dry-run` for Linux / CPython 3.12 resolves every pinned package from prebuilt wheels. |

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
  "expands" into *expand*, so plain text search (`search.py`) for `expansion` doesn't find "expands". The MCP
  tool covers this with a small, fixed synonym table and the tags, but it has no real semantic understanding,
  and a query with several topics returns articles matching any of them (each result lists its tags).
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
- **Small dataset.** 79 articles from 5 healthcare newsrooms, collected since 2026-11-24, growing only as fast
  as the daily runs on the laptop. `get_data_coverage` states this so Claude doesn't overgeneralise.
- **Public MCP endpoint without authentication.** Anyone with the URL can query it. That's acceptable only
  because it serves public, permitted information through a read-only database user.
- **Free-tier hosting.** Cold starts of about a minute after 15 idle minutes, a single instance, and a monthly
  hour allowance. Render's outbound IP ranges are shared, so the Atlas allowlist admits all Render services
  in that region (they still need the password).
- **Local Python vs. Render Python.** Everything was tested in the `ml` environment (Python 3.10); Render runs
  Python 3.12. The code uses nothing version-specific and the pinned dependencies resolve for 3.12, but the
  deployment itself has to be verified once it's live (Deploying to Render → step 4).
