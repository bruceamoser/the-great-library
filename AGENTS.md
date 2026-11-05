# The Great Library — working notes

A LAN-only FastAPI application for browsing, searching and reading a large
tabletop RPG collection. It serves HTML with Jinja2 templates and keeps its
catalogue in SQLite.

## What is in the repository

Application code only. The library itself — `library.db`, `covers/`,
`thumbnails/`, `inbox-originals/`, `provenance.json` — lives on the host and is
gitignored. Never add book files, images or the database to the repository.

## Setup

```sh
uv venv .venv
uv pip install -r requirements.txt
```

`requirements.txt` is pinned to the versions the host runs. Add or upgrade a
dependency deliberately, not as a side effect.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests
```

The suite needs no catalogue and no library data; it builds temporary databases.
CI runs the same command on every push and pull request.

`tests/test_performance.py` is the search regression gate. It builds a synthetic
catalogue, records every statement `/search` executes and the plan SQLite picks,
and fails when a full scan or an eagerly paid count comes back. Every rule is
enforced today and `PENDING` is empty. A rule the code cannot satisfy yet goes
in `PENDING` with the issue that has to land first: while such a rule fails the
test is skipped with that issue named, so set `RPG_PERF_STRICT=1` to enforce it
anyway and delete the entry once it passes (the test warns when that happens).
CI sets `RPG_PERF_STRICT=1`, so a pending rule does not skip the gate there.
`RPG_PERF_LATENCY_MS` overrides the 250 ms per-section budget.

## Measuring search

That gate answers "did this change make a query scan?"; it does not say how long
a section takes. `benchmarks/search_bench.py` does: it builds a synthetic
catalogue from the real schemas and times the actual `/search` route per
section, offline.

```sh
.venv/bin/python benchmarks/search_bench.py --files 5000 --pages 50000
```

It refuses to write over an existing file, so `--db` is safe to point at a
scratch path; without it the catalogue is built in a temp dir and deleted. Judge
a change against the same size and query as the run before it — the default
query matches about 1 page in 20, the scale at which a content count hurts.

Each section is reported twice: `cold`, the fastest of three requests with the
totals cache emptied first, and the percentiles of repeats inside the 60 s TTL
that holds each total. Only the rendered section computes a total, so `cold` is
the expensive column — on a small catalogue the two are close, and on the live
one they are not. `campaigns` is the exception: it rebuilds its catalogue on
every request and is not behind the cache, so its `cold` column is simply the
fastest of three samples of the same uncached work.

Any request slower than `RPG_SLOW_LOG_MS` (default 250 ms, `0` turns it off)
writes one stderr line from the `rpg.slow` logger:

```
slow GET /search section=content 16006ms status=200
```

Of the three that is the only one that measures the real thing in production, so
it is what shows a change helped on the disk that serves the library. On the
host that disk is spinning: a content total over 309,790 pages takes 16 s there
and 70 ms on NVMe for the same database. Measure there before believing an
optimisation helped.

## Running locally

```sh
./run.sh          # http://127.0.0.1:8765
```

`RPG_LIBRARY` and `RPG_DB` point at the collection and catalogue. Without a
catalogue the UI starts but every browse page is empty; that is expected.

## Deploying

```sh
./deploy.sh --dry-run
./deploy.sh
```

This syncs code to `bruce@192.168.44.50:/mnt/disk5/rpg-app`, backs up the
previous code, restarts the service through its own endpoint and health-checks
it. Run it from this checkout, not from the host copy. It never touches library
data.

## GitHub access

This repository belongs to the personal account. Use
`/home/bmoser/.local/bin/gh-personal` for every `gh` command: it pins
`bruceamoser` for that process. Plain `gh` is authenticated as the work account,
cannot write here, and switching accounts would disrupt unrelated work. The
checkout's local git config already routes pushes through the same helper.

## Conventions

- `app.py` holds the FastAPI routes; `catalog.py` builds the catalogue;
  `index_pdf_text.py` owns the PDF full-text index; `taxonomy.py` holds the
  classification rules.
- Mutating endpoints require the `X-Library-Request: 1` header.
- Maintenance endpoints are loopback-only unless `RPG_ADMIN_TOKEN` is set.
- Do not delete `library.db`: `catalog.py` drops and recreates only the catalogue
  tables, and `pdf_pages` / `pdf_fts` / `pdf_status` must survive a rescan.
  `pdf_fts` is external-content over `pdf_pages`; maintain it through the
  triggers, never by inserting into it directly.
- Generated caches (`covers/`, `thumbnails/`) are safe to delete and are not
  source.
