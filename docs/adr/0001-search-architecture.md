# ADR 0001: Search stays on SQLite FTS5

- **Status:** Accepted
- **Date:** 2026-11-04
- **Issue:** #9
- **Supersedes:** —

## Context

The library is a LAN-only app for one household, served by FastAPI from a single
SQLite file (`library.db`) on one host. Search has two surfaces:

| Surface | What answers it today | Matches |
|---|---|---|
| collections | `MATCH` on `search` (FTS5: collection name, shelf, tags, filenames) **plus** `LIKE '%q%'` on `collections.name`, `publisher`, `family` | titles, publishers, families, tags |
| files | `LIKE '%q%'` on `files.filename` and `book_classification.system/series/campaign` — a leading wildcard no index can serve | filenames, system, series, campaign |
| campaigns | `campaigns.catalog()` scans `files` for adventure-path names on every request | campaign names |
| content | `MATCH` on `pdf_fts` (FTS5 over the text of every PDF page) | the inside of the books |

`/search` computes **all four totals** before rendering, whichever section is
selected — so the default view, which needs none of them, pays for the most
expensive one.

Live catalogue, measured on 2026-11-04: **763 collections**, **25,227 files**,
**309,790 indexed pages**, `library.db` **3.66 GB**. Building that index from
scratch took 5,064 PDFs in 4,341 s (72 min) at 3 workers, ~73 pages/s.

The live host stores the database on a **spinning disk** (WD Caviar Black,
~120 MB/s sequential). That is where the numbers that motivated this decision
came from:

| Query (live catalogue) | On the HDD | On NVMe |
|---|---|---|
| content total — `COUNT(*)` over `pdf_fts MATCH`, 26,634 hits | **16,006 ms** | **70 ms** |
| content page — 36 results | 439 ms | — |
| files `filename LIKE '%…%'` count | 2,779 ms | — |
| collections `LIKE` count | 72 ms | — |

Two things follow from that table, and they are the whole point of this ADR:

1. **The 16 s figure is not a limit of SQLite.** The same database copied to
   NVMe answers the same count in 70 ms. Almost all of the 16 s is random reads
   of the *unindexed* payload columns `pdf_fts(rel_path UNINDEXED, page
   UNINDEXED, text)` on a disk with millisecond-scale seeks.
2. **The worst query is the total, not the page.** `COUNT(*)` over a `MATCH`
   walks every hit to produce a number the default view does not even show; the
   36 rows the reader actually sees cost 439 ms. Fixing the count is worth more
   than replacing the engine.

The candidate engines were SQLite FTS5 (current), Meilisearch/Typesense,
PostgreSQL full-text search, and Elasticsearch/OpenSearch. At 310k indexed
pages with a handful of readers, the data already fits the current engine;
every alternative adds a second service, a second copy of the index and a
sync problem, and none of them addresses the dominant cost — where the file
lives and which columns are indexed.

## Decision

1. **Stay on SQLite FTS5** for both collection/file search and PDF content
   search. There is one database, one file to back up, and one thing to deploy.
2. **Put `library.db` on NVMe and keep the media on the HDD.** Seek latency on
   the database file is the single largest factor in search time. The PDFs are
   read sequentially while indexing and streamed to the browser afterwards, so
   what matters for them is bulk sequential throughput — which is what the HDD
   is good at and the SSD is not needed for.
3. **Fix the index and the query shapes rather than the engine:**
   - compute search totals lazily — only for the active section — and cache
     them briefly (#3);
   - add an FTS table for filenames and classification instead of
     leading-wildcard `LIKE` scans (#4);
   - tune per-connection pragmas (`cache_size`, `mmap_size`, `temp_store`) and
     use a read-only connection for GETs (#5);
   - rebuild the PDF index with integer keys and external content so the
     payload columns behind a `MATCH` are a compact `pdf_pages` table instead
     of duplicated paths (#6);
   - keep a benchmark and a CI regression check so an eager count or a full
     scan cannot come back unnoticed (#2, #8).
4. **Do not add a search server yet.** Reopen this decision if any of these
   hold — these are the thresholds, not a permanent answer:

   - **more than 2,000,000 indexed pages** (today: 309,790), where a full-text
     rebuild and a single-file backup stop being comfortable;
   - **more than a handful of concurrent readers** — FTS5 has a single writer
     and the app serialises maintenance against it;
   - **a requirement FTS5 cannot express**: typo tolerance / fuzzy matching,
     synonyms, per-field ranking weights exposed to the UI, or faceted
     aggregations over the corpus;
   - search results must be served **from more than one host**, so the index
     can no longer live in one local file.

## Consequences

### Positive

- No new service to install, supervise, expose on the LAN, or back up. The
  catalogue and the index are one file, which `deploy.sh` already treats as
  untouchable library data and `backup.py` already snapshots consistently.
- Search behaviour stays identical between the web UI and `mcp_server.py`,
  because both go through `app.py`'s single implementation. A separate engine
  would need its own client in the MCP server and would drift.
- Ranking is FTS5's `bm25` over the real page text, with `snippet()` producing
  `<<match>>` context and page links for free — no snippet re-implementation.
- The 70 ms NVMe measurement shows the target is reachable without changing
  engines, so the work stays small and reversible.

### Negative / accepted risks

- **No typo tolerance.** A misspelt query returns nothing; FTS5 has no fuzzy or
  phonetic matching. Acceptable for a household searching its own shelf by
  title or rules term; it would not be acceptable for a public search box.
- **Single writer.** An index rebuild and a rescan cannot overlap, and the app
  enforces that with 409s rather than a queue. Bulk reindexing still takes an
  hour-plus and is a maintenance window, not a background nicety.
- **Ranking control is limited.** `bm25` weights and column weights are
  configurable but coarse; no per-query boosting, no relevance tuning UI.
- **The index is a local file.** It cannot be shared between hosts, and its
  health is only as good as the mtime-based skip logic in
  `index_pdf_text.py`.
- **The HDD cannot be fixed by SQLite.** As long as the database lives on the
  spinning disk, the 16 s count (and any future unbounded scan) returns. Moving
  the database is part of the decision, not an optimisation detail.
- **Stale-index drift is possible.** Restructuring or renaming the library tree
  leaves `pdf_fts` describing paths that no longer exist; only a run that
  reaches the stale-row prune repairs it. And `library.db` must never be
  deleted by hand — that destroys the index along with the catalogue.

### Neutral

- FTS5 is compile-time optional in SQLite, but the host's Python build has it,
  and `app.py`'s search route already degrades gracefully if `pdf_fts` is
  missing (`sqlite3.OperationalError` → `index_available = False`, and the
  search page shows a "not indexed yet" warning instead of a 500).
- The synthetic benchmark measures a proxy corpus, not the real one; absolute
  milliseconds from it are only comparable to another run at the same size.

## Alternatives considered

| Option | Why not now |
|---|---|
| **Meilisearch / Typesense** | Would give typo tolerance and fast faceting, but adds a daemon, a second index to keep in sync with every rescan and inbox import, and another thing to expose on the LAN. The measured 70 ms on NVMe says the corpus does not need it yet. |
| **PostgreSQL full-text** | Strong engine and `ts_rank` is more expressive, but it means replacing the catalogue database — a migration of 25k files, 310k pages and the classification/override tables to buy latency that a disk move already provides. |
| **Elasticsearch / OpenSearch** | Best raw capability and the standard answer at scale, but it is a JVM cluster with gigabytes of heap for a single-user LAN app. Wrong order of magnitude for 3.66 GB of pages. |

All three stay viable if one of the reopen thresholds above is crossed; this
ADR records why they are not worth their operational cost today, not that they
are wrong in general.

## References

- `README.md` §6 — building and running the PDF index, `ionice`/`nice` guidance.
- `catalog.py` — `search` FTS5 schema and catalogue rebuild.
- `index_pdf_text.py` — `pdf_fts` / `pdf_status` schema, mtime skip, workers.
- `app.py` — `search()` route: filters, totals, content `MATCH` and snippets.
