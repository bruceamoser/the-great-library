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
  tables, and `pdf_fts` / `pdf_status` must survive a rescan.
- Generated caches (`covers/`, `thumbnails/`) are safe to delete and are not
  source.
