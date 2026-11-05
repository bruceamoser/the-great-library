# RPG Library

A LAN-only web app for browsing the tabletop RPG collection on this machine.

> **This repository contains the application only.** The library itself —
> `library.db`, `covers/`, `thumbnails/`, `inbox-originals/` and
> `provenance.json` — is host data and is deliberately untracked. See
> `.gitignore`.

---

## 1. What was done to the archives

Three sources fed the library: a Google Takeout export of a Google Drive folder
(36 zips, ~76 GB compressed), a separate "RPG Assets Zips" set (~43 GB), and a
DriveThruRPG library pull.

| Step | Result |
|---|---|
| Extract 36 Takeout zips | 13,244 files |
| Remove macOS metadata (`.DS_Store`, `._*`, `__MACOSX`) | −1,808 files |
| Extract nested archives (3 levels; RAR via `unar`) | +4,700 files |
| Consolidate `name (2)` extraction artifacts | 110 folders merged, 1,810 files reforldered |
| Extract "RPG Assets Zips" (22 zips) | 14,085 files, 43 GB |
| DriveThruRPG sync + merge into shelves | 1,606 files, 30 GB |
| De-duplicate by SHA-256 | −14,726 files / **48.15 GB**, then −3,793 / **10.52 GB** |
| Remove software / VTT modules / binaries | shelf removed entirely |
| Restructure into `Books/` + `Assets/` | 61 moves |
| Final macOS sweep of the merged library | −884 files, −30 dirs |

Final library: **`/mnt/disk5/RPG-Library`** — 121 GB, 23,452 files, 2,626 dirs,
**758 collections**, `sqlite3` catalog in `rpg-app/library.db`.

Sources on disk: **599 collections from DriveThruRPG**, 158 from Google Takeout,
1 inbox — all in one tree, distinguished by metadata rather than by folder.

The original zips are archived, **untouched**, at:

```
/mnt/disk5/archive/roleplaying-games-takeout-20261001T175919Z/   36 zips, 71 GB
/mnt/disk5/archive/rpg-assets-zips/                             22 zips, 43 GB
```

Both folders carry a `README.md` with restore instructions and a `SHA256SUMS`,
so every step above is reproducible from scratch. `/mnt/disk5` now holds only
`RPG-Library/`, `archive/`, `rpg-app/` and `rpg-tools/`.

> A note on idempotency: the extractor **skips** a step whose output already
> exists and is non-empty. Early on, kept archives were re-extracted on a
> second pass and produced 50 `(2)` folders; that is fixed, and the merge tool
> cleans up any that remain.

### Layout

```
RPG-Library/
  Books/                       rulebooks, supplements, adventures, sheets
    01-DnD-and-d20/  02-OSR-and-Old-School/  03-Indie-and-Story/
    04-SciFi-and-Cyberpunk/  05-Horror-and-Modern/  06-Licensed-IP/
    07-Universal-Systems/  08-Warhammer-and-Grimdark/
  Assets/                      maps, art, audio, video, tool projects
    Maps/  VTT-Assets/  tokens/  fantasy-tokens/  Dynamic Dungeons Maps/
    The Misty Vale/  Loke BattleMats/  ...
  _Inbox/                      drop new files here
  _Unsorted/                   strays
```

### Two categories, many sources

The library is **one library with several sources**. DriveThruRPG is a
*provenance*, recorded in `provenance.json` and surfaced as
`collections.source` — not a folder tree. Adding another source (a Humble
bundle, a publisher store) means adding entries to that registry.

Everything is either a **Book** or an **Asset**:

| Category | Subcategories |
|---|---|
| Book | `core-rules`, `expansion`, `campaign`, `sheets` |
| Asset | `maps`, `art`, `audio`, `video`, `project` |

Classification is **per file** (`files.category` / `files.subcategory`), not per
folder — so a game's maps are categorised as assets while still sitting next to
its rulebook on disk. Browsing a game as a whole keeps working; the categories
drive navigation and filtering in the app.

**Software is not tracked at all.** `07_remove_software.py` removes VTT
modules (found by their `module.json`/`system.json` marker, not by guessing at
folder names) and binaries; those files are excluded from the catalog entirely.

---

## 2. Running the app

```sh
cd /mnt/disk5/rpg-app
./run.sh                 # http://<this-host>:8765
```

Environment overrides:

| Variable | Default |
|---|---|
| `RPG_LIBRARY` | `/mnt/disk5/RPG-Library` |
| `RPG_DB` | `./library.db` |
| `PORT` | `8765` |

### Database connection settings

The catalogue is 3.66 GB and SQLite's defaults are tuned for a small file: a
2 MB page cache, no memory map, temp tables on disk. Every connection
therefore sets its own pragmas, in `sqlite_settings()` in `app.py`:

| Pragma | Default | Meaning | Environment |
|---|---|---|---|
| `cache_size` | `-65536` | 64 MB page cache (negative values are KiB, not pages) | `RPG_SQLITE_CACHE_MB` (64) |
| `mmap_size` | `268435456` | map up to 256 MB of the file into memory, 0 disables it | `RPG_SQLITE_MMAP_MB` (256) |
| `temp_store` | `MEMORY` | sort and temp tables in RAM, not in `/tmp` | `RPG_SQLITE_TEMP_STORE` (`MEMORY`, `FILE` or `DEFAULT`) |
| `busy_timeout` | `5000` | milliseconds to wait for a lock before failing | `RPG_SQLITE_BUSY_TIMEOUT_MS` (5000) |

Unparseable values fall back to the default, and `temp_store` only accepts the
three keywords above, so a typo in the unit file cannot inject SQL or leave the
app without a working cache. Writes are serialised by the maintenance lease
anyway, which is why the default busy timeout is shorter than the old 30 s.

The page cache is per connection, and a request holds one connection at a time,
so the ceiling on a busy LAN is roughly *cache size × concurrent requests* —
lower `RPG_SQLITE_CACHE_MB` if the host ever runs short of memory. The mmap is
shared by the OS page cache rather than copied per connection.

Reads run on a **read-only** connection (`file:…?mode=ro`). Only the two routes
that change the catalogue — `POST /api/metadata/{cid}` and
`POST /api/book-metadata/{fid}` — open a writable handle with `db(write=True)`;
a write attempted on a read connection fails with `attempt to write a readonly
database` rather than silently succeeding. `GET /classification` creates its
audit table through a writable handle only if an old catalogue is missing it.

### Reaching it from another device on the LAN

The server already listens on `0.0.0.0:8765`, so no code change is needed —
but `ufw` is active and must be told to allow the port:

```sh
sudo ufw allow from 192.168.44.0/24 to any port 8765 proto tcp
```

Then browse to `http://192.168.44.50:8765/` from any machine on the LAN.

There is **no login** — the firewall rule above restricts access to the local
subnet. Do not port-forward this to the internet.

### Run it as a service

A unit file is provided:

```sh
sudo cp /mnt/disk5/rpg-app/rpg-library.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rpg-library
```

### Deploying updates

The repository is the source of truth; the host runs a deployed copy at
`/mnt/disk5/rpg-app`. `deploy.sh` syncs the application code and restarts the
service over SSH:

```sh
./deploy.sh --dry-run     # show what would change
./deploy.sh               # back up, sync, restart, health-check
```

It never touches library data (`library.db`, `covers/`, `thumbnails/`,
`inbox-originals/`, `provenance.json`), takes a code-only backup under
`/mnt/disk4/RPG-Library-Backups/`, and restarts through the app's own endpoint,
so it needs no sudo. `--prune` also deletes files removed from the repository;
`--no-backup` and `--no-restart` skip those steps. Override the target with
`DEPLOY_HOST`, `DEPLOY_DIR`, `DEPLOY_SERVICE`, `DEPLOY_URL` or
`DEPLOY_BACKUP_ROOT`.

### Restarting from the app

The **Refresh** page has a *Restart the library app* button. It calls
`POST /api/restart?confirm=1`, which schedules `os._exit(1)` after ~0.75 s so
the HTTP response gets flushed first. systemd then restarts the unit
(`Restart=always`).

Two guards keep this from being a foot-gun:

* The endpoint returns **400** unless `confirm=1` is passed.
* `_restart_supervisor()` compares `os.getpid()` against
  `systemctl show rpg-library -p MainPID`. If the running process *is* the
  systemd main PID, exiting is safe. Otherwise it refuses — so running the app
  by hand under `manage.sh` can't leave you with nothing listening.

> Do not detect the supervisor via `INVOCATION_ID`; any systemd scope sets it,
> including ones unrelated to this service. The MainPID comparison is reliable.

### Restarting without sudo

The unit lives in `/etc/systemd/system/`, and *system* units are root-owned —
that is the only reason a restart asks for a password. Three ways around it:

**1. sudoers drop-in (recommended — keeps boot autostart, drops the prompt)**

```sh
sudo /mnt/disk5/rpg-app/allow-nopasswd-restart.sh
```

Installs `/etc/sudoers.d/rpg-library` granting exactly
`systemctl {start,stop,restart,status} rpg-library` with no password, validated
with `visudo -cf` before it is kept. Afterwards
`sudo systemctl restart rpg-library` runs without prompting.
Undo with `sudo rm /etc/sudoers.d/rpg-library`.

**2. `manage.sh` (no sudo at all)**

```sh
cd /mnt/disk5/rpg-app
./manage.sh start|stop|restart|status|logs
```

Runs the server detached (`setsid nohup`) so it survives closing the terminal
and VS Code restarting. Trade-off: it does **not** start on boot, and it refuses
to start if the systemd service is already holding the port. To switch over
permanently:

```sh
sudo systemctl disable --now rpg-library
```

**3. systemd user service**

`systemctl --user` needs no sudo, but surviving logout needs a one-time
`sudo loginctl enable-linger bruce`. More moving parts than option 1 for no
extra benefit.

> **Do not** put your password in a file for `sudo -S`. `/mnt/disk5` is a
> guest-readable, password-free Samba share, so that would be strictly worse
> than typing the password.

**Rarely needing a restart at all:** `app.py` is imported once at startup, but
templates and static files are re-read on every request. Running uvicorn with
`--reload` (add it to `manage.sh` or the unit) makes code edits self-applying.


---

## 3. Adding new content

Open **Inbox** in the main navigation (`/inbox`). Upload multiple files or drop them
onto the upload area, then review automatic placement and choose **Import all
arrivals automatically**. Recognized titles and existing catalog shelves guide
placement; related arrivals are grouped, ZIP archives are unpacked, and uncertain
collections go to `Books/_Unsorted` for later classification review. No per-file
metadata forms are required. New arrivals never overwrite existing collections.

The import runs in the background under the maintenance lock and refreshes the
catalog once for the entire batch. The page reports progress, errors, and links
to imported collections. Original inbox items are retained outside the scanned
library in `rpg-app/inbox-originals/<batch-id>/`, with an import manifest.
Uploads are limited to 2 GB per file; ZIP expansion to 20 GB / 20,000 entries.
Archives containing unsafe paths or symbolic links are rejected. PDF full-text
indexing remains a separate maintenance operation.

The older rescan-only workflow below catalogs inbox files without shelving them.


Files dropped into `RPG-Library/_Inbox/` are picked up on the next rescan.
Rebuild the catalog from the terminal:

```sh
cd /mnt/disk5/rpg-app
./rescan.sh            # wraps catalog.py with the right --moves/--provenance
```

…or hit the in-app endpoint:

```sh
curl -X POST http://localhost:8765/api/rescan
```

Run `catalog.py` by hand as:

```sh
./.venv/bin/python catalog.py --library /mnt/disk5/RPG-Library --db library.db \
    --moves /mnt/disk5/rpg-tools/shelf-moves.json --provenance provenance.json
```

`--provenance` is what records each collection's **source**; omit it and the
source attribution is lost. Do not pass a `--db` path that differs from the one
the app uses, and never delete `library.db` by hand (see section 6).

New content lands in `_Inbox/` as a single flat collection. To get a proper
collection (and correct category classification) give each arrival its own
sub-folder inside `_Inbox/`, or file it directly under `Books/<shelf>/<game>`
or `Assets/<collection>`.

Which of Book/Asset something becomes is decided **per file** by
`classify_category()` in `catalog.py`, from the file's kind and name — so a
`map` under `Books/` still counts as an Asset. See section 1.

---

## 4. Adding your own tags / fixing metadata

Human-curated families, publishers, genres and tags live in the `META` dict at
the top of `catalog.py`. Edit it, then rescan — collection tags and the search
index are rebuilt from it.

---

## 5. DriveThruRPG

DriveThruRPG has no *documented* public API, but it does expose **Library App
Keys** (account settings → "Library App Keys"). The
[`drpg`](https://pypi.org/project/drpg/) package uses that key to enumerate your
purchases and download them, and on subsequent runs it fetches **only new and
changed files** — which is exactly the on-demand sync wanted here.

### One-time setup

Generate a key at <https://www.drivethrurpg.com/en/account/settings>, then save
it to a file so it never lands in shell history or a log:

```sh
mkdir -p ~/.config/drpg
printf '%s' 'YOUR_KEY_HERE' > ~/.config/drpg/token
chmod 600 ~/.config/drpg/token
```

### Syncing

```sh
cd /mnt/disk5/rpg-app
./sync-dtrpg.sh --dry-run     # list what would be downloaded, download nothing
./sync-dtrpg.sh               # download new / changed items
./dedupe.sh                   # remove duplicates (curated shelves win)
./merge-dtrpg.sh              # file products into the genre shelves
./rescan.sh                   # refresh the catalog
```

From the web UI, **Sync now** performs the whole sequence automatically:
`drpg` → dedupe → merge → catalog rebuild. `POST /api/dtrpg/sync` accepts
`dedupe=0`, `merge=0` and `rescan=0` to skip individual steps.

Downloads land in `RPG-Library/11-DriveThruRPG/`, which `drpg` organises by
publisher. That folder is a **staging area, not a destination** —
`05_merge_dtrpg.py` then moves each product into the genre shelves using
product-name rules (`KEYWORD_RULES`) with a publisher fallback
(`PUBLISHER_DEFAULT`). Products it cannot map are moved to `03-Indie-and-Story`
and listed in `<log>.unmapped`, so nothing is ever dropped.

Useful `drpg` flags, passed straight through:

| Flag | Effect |
|---|---|
| `--dry-run` | report only, download nothing |
| `--use-checksums` | compare file checksums instead of timestamps (slower, more precise) |
| `--omit-publisher` | drop the publisher folder from the path |
| `--compatibility-mode` | mimic the official DriveThruRPG client's exact naming |

`--library-path` and `--threads` are already set by the script
(`RPG_LIBRARY`, `DRPG_THREADS`).

### Caveats

- It relies on an **undocumented** endpoint, so it can break without warning;
  the `drpg` version number is the year/month it was last verified working.
- The first full sync can be large if you have a lot of purchases — run
  `--dry-run` first.
- Content it downloads may duplicate books already in the other shelves. A
  normal `02_dedupe.py` pass (Section 6) will collapse identical files.

### Alternative: inbox workflow

If `drpg` ever breaks, downloading manually and dropping files into
`RPG-Library/_Inbox/<Product Name>/` then running `./rescan.sh` works just as
well and depends on nothing.


---

## 6. Searching inside PDFs

The default search indexes collection names, shelves, tags and **filenames**.
A separate opt-in index adds the **text inside** every PDF, so a search for
`grappling` returns matching *pages*, not just files whose names contain it.

### Search architecture

Search is **SQLite FTS5 inside the same `library.db` as the catalogue** — there
is no search server. Why that is the right engine here, what would change it,
and what it costs are recorded in
[docs/adr/0001-search-architecture.md](docs/adr/0001-search-architecture.md).

Current live catalogue, measured on 2026-11-04: **763 collections**, **25,227
files**, **309,790 indexed pages**, `library.db` **3.66 GB**. (Older figures
elsewhere in this README describe the October restructure as it stood then.)

| Query on the live catalogue | HDD (live host) | NVMe (copy) |
|---|---|---|
| content total — `COUNT(*)` over `pdf_fts MATCH`, 26,634 hits | 16,006 ms | **70 ms** |
| content page — 36 results | 439 ms | — |
| files `filename LIKE '%…%'` count | 2,779 ms | — |
| collections `LIKE` count | 72 ms | — |

The 16 s figure is **random reads of unindexed FTS columns on a spinning disk,
not a limit of SQLite** — the identical database answers the identical count in
70 ms on NVMe. Note also that the slow part is the *total*, not the page: the 36
rows a reader actually sees cost 439 ms. Hence the current work: keep
`library.db` on NVMe with the media on the HDD, compute totals only for the
active section (#3), search filenames and classification through FTS instead of
`LIKE` scans (#4), tune the connection pragmas (#5), and rebuild the PDF index
with integer keys and external content (#6).

### Build it

From the app: **Sync & Index** → *Build / update index*.

From a terminal (note the idle I/O priority — see below):

```sh
cd /mnt/disk5/rpg-app
ionice -c2 -n7 nice -n 19 ./.venv/bin/python index_pdf_text.py \
    --library /mnt/disk5/RPG-Library --db library.db --workers 3
```

Measured on this machine, full rebuild of the restructured library
(5,066 PDFs):

| | |
|---|---|
| Processed | **5,064** in 4,341 s (72 min) at 3 workers |
| Throughput | **~1.2 files/s, ~73 pages/s** |
| Pages extracted | **317,898** from 997.5 M characters |
| Index size | `library.db` ≈ 1.9 GB (`pdf_pages` = 307,978 rows) |
| Image-only PDFs | **295 (5.8%)** — no text layer, unsearchable without OCR |
| Hard failures | **2** — both pre-existing zero-byte PDFs in the source export |
| Stale rows pruned | 4,896 (correctly removed after the restructure renamed every path) |

That run used the old code, which let MuPDF read each PDF in its own scattered
order. Reading each file as one sequential stream and parsing it from memory is
~3.9–5.3× faster at the same worker count, so the same rebuild should take
**~16–20 min** rather than 72. See *Why three workers, and why streamed reads*
below.

### Upgrade an existing index

Older databases stored the path and page number on every `pdf_fts` row and
joined hits back to `files` on a string. The index is now `pdf_pages` (one row
per page, integer `id`) plus `pdf_fts` as an **external-content** FTS5 table
over `pdf_pages.text`, with triggers keeping the two in sync. Upgrading copies
the text out of the old table, rebuilds the index and drops the obsolete
`pdf_page_map`:

```sh
cd /mnt/disk5/rpg-app
ionice -c2 -n7 nice -n 19 ./.venv/bin/python index_pdf_text.py \
    --library /mnt/disk5/RPG-Library --db library.db --migrate --vacuum
```

`--migrate` is idempotent (a second run does nothing) and every indexing run
also performs it automatically. `--vacuum` rewrites the database and reports
the size before and after; on the host database that is where the ~1.7 GB of
free pages left by earlier delete/insert cycles is reclaimed. No PDF is
re-extracted unless `--rebuild` is also given.

Deploy the new code first: until the migration runs, the app treats the content
index as absent and simply shows no inside-book results. The migration itself
copies every page once, so keep the same `ionice`/`nice` wrapper and allow a
few minutes; `--vacuum` additionally needs temporary free space roughly equal
to the final database size (~2 GB). Run it at a quiet moment: if SQLite reports
the database is locked during `VACUUM`, re-run the same command — the schema
upgrade is already committed and is skipped, and only the VACUUM is retried.

A full rebuild takes tens of minutes because every PDF must be re-extracted;
an incremental run only touches files whose mtime changed.

### Where the 72 minutes actually went

The index is written to `library.db`, which lives on the NVMe system disk,
while the PDFs live on the spinning data disk — so reads and writes do not
compete. Measured cost of each part, for an index the size of the real one
(309,790 pages, 1.25 GB of text):

| Stage | Cost | Share of a 72-min run |
|---|---|---|
| Reading and extracting the PDFs (3 workers) | ~69 min | **~96%** |
| Writing every page into `pdf_pages` | ~35 s | 0.8% |
| Trailing `INSERT INTO pdf_fts(pdf_fts) VALUES('optimize')` | ~5 s | 0.1% |
| Pruning stale rows | ~0.4 s | — |

So batching and durability pragmas were never the lever — they are worth under
a minute in total. The PDF reads are, and the thing that was wrong with them
was not the worker count but the **read pattern**: MuPDF walks each file's
object graph, so its reads arrive scattered across the file, which on one
spinning disk is a stream of seeks.

Writes are nevertheless batched by volume (about 12 MB of page text, or 200
files, per transaction — `--batch-mb` tunes it), and **during a rebuild only**
the connection runs with a 64 MB cache and `synchronous=NORMAL`. Both are
per-connection settings that disappear when the indexer exits, so an
interruption loses at most the batch in flight and the next run resumes from
the last commit, since finished files are already recorded in `pdf_status`.
`NORMAL` in WAL mode cannot corrupt the database — it can only lose the last
commits to a power cut, which the next run redoes — and it costs ~2 s less
than the default across a whole rebuild.

`journal_mode` is deliberately left alone: it is stored in the database file,
so a rebuild killed while it was in `MEMORY` would leave the live catalogue
crash-unsafe for the web app. Incremental runs never change any of this.

### Why three workers, and why streamed reads

The library sits on a **single spinning disk** (WD Caviar Black, 120 MB/s
sequential). MuPDF reads a PDF object by object, in an order that is not
sequential on disk, so on a spinning disk each of those small reads can cost a
seek. `index_pdf_text.py` therefore reads each whole file in one sequential
pass and hands the bytes to MuPDF (`open_pdf()`); files larger than
`--stream-max-mb` (default 256 MB, above the library's 99th percentile) are
opened in place so worker memory stays bounded.

Where the time goes depends on both the read pattern *and* the worker count,
because they interact — scattered reads get worse with parallelism, sequential
reads get better until the disk saturates. Measured on the real library, cold,
with the page cache dropped before every read — 60 random files, 881 MB, 4,022
pages, and identical text extracted in every row:

| Workers | Read pattern | Time | Throughput |
|---|---|---|---|
| 3 | scattered | 57.4 s | 69 pages/s |
| 1 | streamed | 31.4 s | 128 pages/s |
| 2 | streamed | 16.9 s | 238 pages/s |
| **3** | **streamed** | **14.9 s** | **271 pages/s** |
| 4 | streamed | 15.1 s | 266 pages/s |

A second, wider sample (80 files, 6,001 pages) agreed: 3 workers streamed
reached 328 pages/s against 62 pages/s for the old pattern, ~5.3×, and 4 and 6
workers were again slower than 3. So the default is 3 workers *with* streaming,
~3.9× the old code on the narrower sample and better on the wider one.

That also disposes of the old story about this machine: the 72-minute run at
3 workers was not slow because three workers were too many, it was slow because
each of them was issuing scattered reads. `--workers 1` without streaming is
only ~1.8× better, which is why the two changes belong together.

Streaming also makes the run *gentler*, not just shorter: a sequential read is
one seek for megabytes, and finishing in a fifth of the time leaves the disk
idle for far longer. It is still not free, though — at 8 workers the web app
was observed stuck in uninterruptible disk sleep (`State: D`,
`wchan: read_extent_buffer_pages`), unable to serve a single request, purely
because the indexer had saturated the disk queue. Do not add workers past the
measured optimum.

The app therefore launches the indexer as
`ionice -c2 -n7 nice -n 19 ... --workers 3`: best-effort I/O at the lowest
priority, so heavy reads yield to interactive traffic but still get scheduled.

> **Do not use `ionice -c3` (idle class) here.** On the `mq-deadline`
scheduler idle-class requests are only dispatched when the disk queue is
completely empty, which on a normal desktop effectively never happens. It
measured ~25x slower (3.8 MB/s vs ~90 MB/s) and looked like a hang.

Stopping the indexer mid-run is safe — progress is committed in batches and
re-running skips files already done.

Re-running is cheap: files whose mtime is unchanged are skipped. Use *Full
rebuild* to force everything.

### Notes

- Extraction uses **PyMuPDF**; `pypdf` is ~20x slower and stalls on large files.
- The index lives in the same `library.db`, in `pdf_pages` / `pdf_fts` /
  `pdf_status`. Page text is stored once in `pdf_pages`; `pdf_fts` indexes it
  by integer rowid (external content). Everything is keyed on `rel_path`, so a
  catalog rebuild does not invalidate the entries — and `catalog.py` only drops
  and recreates the *catalog* tables, so a rescan now preserves the index.
  > **History:** `catalog.py` used to `os.remove()` the whole database file
  > before rebuilding. That silently destroyed the PDF content index. Fixed.
  > The app also refuses to run a rescan while indexing is in progress.
- Results link to `‎/view/<id>?page=N`, which opens the PDF at that page.
- The ~5% of scanned/image-only PDFs would need OCR. That is a much larger job
  (hours to days) and is not implemented.
- The indexer is a separate process. `GET /api/index/status` scans `/proc` to
  detect an indexer started from a terminal, so the UI reports the true state
  either way; `indexed / total` is accurate throughout.

---

## 7. Tooling reference

Pipeline scripts live in `/mnt/disk5/rpg-tools/`:

| Script | Purpose |
|---|---|
| `01_extract_nested.py` | Recursively extract nested zip/rar/7z. `--keep` archives rather than deleting (auto-on inside `11-DriveThruRPG`); skips when the target already exists and is non-empty |
| `02_merge_N_folders.py` | Merge `name (2)` folders back into `name` |
| `02_dedupe.py` | SHA-256 duplicate removal with keeper preference. `AUX_TOPS` makes curated shelves win over the DriveThruRPG dump |
| `03_shelf.py` | Move collections into shelf folders |
| `04_near_dupes.py` | PDF near-duplicates (page count + extracted text) |
| `05_merge_dtrpg.py` | Merge synced DriveThruRPG products into the shelves by `KEYWORD_RULES`, then `PUBLISHER_DEFAULT`. Unmatched products go to a fallback shelf and are logged to `<log>.unmapped` |
| `06_restructure.py` | Build `Books/` + `Assets/`, folding in a staged asset directory |
| `07_remove_software.py` | Remove VTT modules and binaries. Modules are found by their `module.json`/`system.json` **structural marker**, never by filename |
| `08_sweep_junk.py` | Remove OS metadata (`__MACOSX/`, `._*`, `.DS_Store`, `Thumbs.db`). Dry-run by default; `--apply` to delete |

`08_sweep_junk.py` is not a one-time step. The junk arrives in waves — the later
DriveThruRPG sync and the asset-zip extraction each reintroduced it *after* the
original sweep (30 dirs / 884 files). It is worse than cosmetic: the PDF
indexer tries to open `._Book.pdf` resource forks as real PDFs and logs a
failure for every one. Run it after any bulk import.

`05_merge_dtrpg.py` originally `shutil.rmtree`'d its source after moving only
the products it had mapped, which silently destroyed ~129 files across ~10
publishers. It now routes anything unmatched to a fallback shelf, writes an
`.unmapped` log, and only `rmdir`s the source if it is genuinely empty.

Run `02_dedupe.py` with `--min-bytes 1`: the default 1 MB floor silently skips
small byte-identical files (VTT tokens and minis are typically 300-600 KB). The
size pre-filter keeps it fast, so there is no reason not to.

`catalog.py` must never delete `library.db`. It drops and recreates only the
*catalog* tables; `pdf_pages` / `pdf_fts` / `pdf_status` are owned by
`index_pdf_text.py` and must survive a rescan. Deleting the file used to leave
the indexer writing to a deleted inode and reporting false success.

Logs and manifests are alongside them. The originals live in
`/mnt/disk5/archive/roleplaying-games-takeout-20261001T175919Z/`.

Requires `7z` and `unar` (both present on this machine). There is **no
`sqlite3` CLI** — use `python3 -m sqlite3` or Python's `sqlite3` module.

---

## 8. App layout

```
rpg-app/
  app.py                 FastAPI routes (browse, search, stream, rescan,
                         DriveThruRPG sync, PDF indexing, restart)
  catalog.py             builds library.db from the library tree
  index_pdf_text.py      extracts PDF text into the FTS index
  library.db             SQLite + FTS5 catalog (760 collections)
  provenance.json        source registry (DriveThruRPG, Takeout, assets)
  run.sh                 launcher
  deploy.sh              sync code from a checkout to the host and restart it
  rescan.sh              rebuild catalog
  dedupe.sh              run de-duplication
  merge-dtrpg.sh         merge a DriveThruRPG sync into the shelves
  sync-dtrpg.sh          on-demand DriveThruRPG sync
  manage.sh              start|stop|restart|status|logs (no sudo)
  supervise.sh           supervisor helper
  allow-nopasswd-restart.sh   optional sudoers drop-in
  rpg-library.service    systemd unit
  templates/             Jinja2 templates
  templates/_fileactions.html   per-file buttons (open / view / play)
  templates/shelf.html   the bookshelf view (front page)
  static/style.css       layout primitives (grids, tables, modal positioning)
  static/theme.css       the old-library skin (palette, textures, shelves)
  static/fonts/          self-hosted OFL fonts + fonts.css
  static/media.js        media popup + hover snapshot
  static/pdfviewer.js    PDF.js viewer (page nav, zoom, fit-width)
  static/pdfjs/          bundled PDF.js 4.10.38
  thumbnails/            on-demand image cache (see below)
  covers/                on-demand shelf cover cache
  thumbnails.py          pre-generates the thumbnail cache (see below)
  mcp_server.py          MCP server for AI assistants (see section 11)
  benchmarks/search_bench.py   offline /search benchmark (see below)
  tests/                 unittest suite; needs no library data
  .venv/                 Python 3.14 virtualenv
```

> The venv was built with `python3 -m venv --without-pip` then bootstrapped via
> `get-pip.py`, because `python3.14-venv`/`ensurepip` is not installed here and
> `pip install --user` is blocked by PEP 668.

> PDF.js is bundled because the VS Code embedded browser has no PDF plugin —
> a plain `<iframe src="...pdf">` renders blank. The viewer is used for every
> document, with `data-page` deep-linking from search results.

### File lists and previews

Every file row in a list (`/search`, `/collection/*`) gets an action button
chosen by `media_of()` in `app.py`, which is the *same* mapping the `/view`
route uses — so a file can never be previewable in one list and not the other.

| The file is | The button does |
|---|---|
| a PDF | opens `/view/{id}` in a **new tab** |
| an image | opens a **popup** with the picture (and hovers to a thumbnail) |
| audio | opens a **popup** with a player, and plays immediately |
| video | opens a **popup** with a player, and plays immediately |
| anything else | `open` (raw) — nothing is lost for odd types like `.zip` |

A `↓` download link is always present. `Escape`, the backdrop and the ✕ all
close the popup, and closing **removes the element**, which is what stops
audio/video playback rather than leaving it running invisibly.

Nothing is fetched until you interact: hovering loads one thumbnail, clicking
loads one image or player. A 500-row result page does not pull 500 files.

#### Why thumbnails exist

The library contains 359 images over 20 MB, the largest being a 184 MB,
20064×13082 map scan. Handing that to an `<img>` costs seconds of decode and
hundreds of MB of RAM — the hover snapshot was unusable without help. So
`/thumb/{fid}?w=` generates a cached downscale with PyMuPDF:

* `?w=320` for the hover snapshot, `?w=1600` inside the popup.
* `w` snaps to those two values, to bound the size of the cache.
* Opaque sources cache as **JPEG**; sources with an alpha channel stay **PNG**,
  because many tokens and art pieces are transparent and flattening them onto
  black would ruin them.
* Formats PyMuPDF cannot read (webp, gif, avif, svg) **fall back to the
  original file** rather than erroring — browsers render those natively.
* Generation is capped at **2 concurrent** by a semaphore: one 184 MB scan
  decodes to ~800 MB, so a burst of hovers must not be allowed to run away
  with memory.
* Cached files are keyed `fid-mtime-width.ext` and written via a temp file +
  `os.replace`, so a reader never sees a half-written image.

First hover on a 184 MB scan costs ~4 s; after that it is instant. The cache is
safe to delete at any time.

### API

| Route | Purpose |
|---|---|
| `GET /api/collections?category=&shelf=&q=` | collections as JSON |
| `GET /api/stats` | file counts per category/subcategory and per kind |
| `GET /api/dtrpg/status` | sync job state + log tail |
| `POST /api/dtrpg/sync?dry_run=0&dedupe=1&merge=1&rescan=1` | start DriveThruRPG sync |
| `GET /api/index/status` | PDF index progress |
| `POST /api/index/build?rebuild=0&workers=3` | start PDF text indexing (three workers, each streaming a whole file: fastest measured on the library disk) |
| `POST /api/rescan` | rebuild the catalog |
| `GET /thumb/{fid}?w=320\|1600` | cached downscaled image (see file lists) |
| `GET /cover/{cid}` | cached shelf cover for a collection |
| `POST /api/restart?confirm=1` | exit so systemd restarts the unit; 400 without `confirm=1` |

Mutating endpoints return **409** if the conflicting job is already running:
sync refuses while indexing, and rescan/index refuse while the other runs.
"Already running" covers indexers started from the shell — the app scans `/proc`
for `index_pdf_text.py` rather than relying only on its own job registry.

### Measuring search performance

`/search` is the slowest surface in the app, and on the live catalogue its
slowest query is the **total**, not the page of results: on 2026-11-04, on the
host's spinning disk, a content count over 309,790 indexed pages took 16,006 ms
where the same count on NVMe took 70 ms. Three tools make that measurable instead
of guesswork: a regression gate in the test suite, a benchmark you run by hand,
and a line the server writes in production.

`tests/test_performance.py` is the gate: it records every statement `/search`
executes and the plan SQLite picks, and fails when a full scan or an eagerly paid
count comes back. It runs in CI and answers "did this change make a query scan?",
not "how long does a section take".

**`benchmarks/search_bench.py`** answers the second question. It builds a
synthetic catalogue using the real schemas (`catalog.SCHEMA`,
`index_pdf_text.SCHEMA`, and the tables `classification`/`taxonomy`/`metadata`
create) in a throwaway SQLite file, then drives the actual `/search` route
through FastAPI's in-process test client, once per section. The search indexes
are filled through the same functions a rescan calls
(`catalog.rebuild_files_fts`, `index_pdf_text.ensure_schema`), so the timings
cover the FTS query paths rather than their empty-index fallbacks. No library
data, no server started, no network.

```sh
.venv/bin/python benchmarks/search_bench.py                        # 5,000 files / 50,000 pages
.venv/bin/python benchmarks/search_bench.py --files 25227 --pages 309790 --collections 763
.venv/bin/python benchmarks/search_bench.py --runs 10 --query ''    # filter mode, no query
```

```
synthetic catalogue  5,000 files  50,000 pages  152 collections  (5,000 in files_fts, built in 1.7 s)
query                'dragon'   5 run(s), 1 warm-up
db                   /tmp/rpg-search-bench-y70x7tpd/bench.db (deleted)

section        cold ms    p50 ms    p95 ms    min ms    max ms
collections        3.8       2.9       3.8       2.8       3.9
files              3.8       4.1       4.2       3.7       4.3
content           46.0      27.8      29.9      27.1      30.1
campaigns         32.3      37.1      39.4      33.2      39.4
```

Each number is a whole request, template rendering included, because that is
what the reader waits for. Only the section being displayed computes a total
(#3), so a costly count shows up in one row and in one column: `cold` empties
the totals cache before each sample — three of them, and the fastest is
reported, because timing noise only ever adds time — so it is the only column
that pays the count, while `p50`/`p95`/`min`/`max` are repeats inside the 60 s
cache that holds a total, the cheap case a reader sees when they page through
results. `cold` is cold of that cache and not of the disk: the untimed request
before it has already read the pages, so the file cache is warm. `campaigns` is
the exception to all of it: it rebuilds its catalogue on every request, so
nothing is cached there and its `cold` column is only the fastest of three
samples of the same work. `--json` prints the same measurements as a single
object for a regression check, including every cold sample. `--db PATH` writes
the synthetic catalogue somewhere you keep, and refuses a path that already
exists so it can never overwrite `library.db`. `--runs`/`--warmup` control the
sample, `--seed` fixes the generated rows, and p50/p95 are the
linear-interpolated percentiles.

The timings belong to the machine that produced them and to a synthetic
catalogue; they are comparable to each other — same `--files`, `--pages` and
`--query` — not to the numbers in section 9's neighbours or to a live page. Run
it on the host when the question is about the host's disk.

The default query hits roughly 1 page in 20, which keeps a content search big
enough to be worth timing; `--query ''` benchmarks filter-only browsing.

**Slow-request log.** Any request slower than `RPG_SLOW_LOG_MS` (default 250,
`0` disables) writes one line to stderr with method, path, `section` and
duration:

```
slow GET /search section=content 16006ms status=200
```

Nothing needs configuring: the app attaches a handler to the `rpg.slow` logger
because uvicorn configures only its own. Under systemd (`rpg-library.service`)
the line lands in `journalctl -u rpg-library`; started by `./manage.sh` it goes
to `uvicorn.log`, which `./manage.sh logs` tails. Two limits worth knowing: a
streamed response (a file from `/raw`, a thumbnail) is timed up to its headers
rather than to its last byte, and the log records the `section` parameter but
not the search terms themselves.

---

## 9. Known limitations

- **Source attribution blurs once a product is merged and deduped.** Provenance
  is recorded per collection, keyed by its path in the library. When a
  DriveThruRPG product wins a dedupe against a Takeout copy (or vice versa), the
  survivor carries the source of the *location* it now sits in. 599 collections
  are attributed to DriveThruRPG; the rest correctly fall back to Takeout,
  including cases where the Takeout copy legitimately won. Per-file provenance
  is not tracked.
- **~5% of PDFs are image-only scans** with no text layer, so they are invisible
  to content search. Fixing this means OCR — a much larger job (hours to days)
  that is deliberately not implemented.
- **`provenance.json` keys are library-relative paths.** Any future restructure
  must re-prefix them, or every collection silently falls back to `takeout`.
  This was caught by checking the match count, not by watching the page render
  (which looks fine either way).
- **Single disk, single queue.** Indexing and a large rescan cannot overlap
  without the database contending; the API enforces this but a CLI rescan run by
  hand will block on the indexer's write lock.
- The catalog's `source` column describes the *collection*, while categories are
  computed *per file*. A Pathfinder book collection therefore appears under
  `cat=asset` if any single map lives inside it. That is intended, but it does
  make the collection-level counts in the UI look surprising at first.
- **The first hover on a huge scan is slow (~4 s)** for the same reason the
  thumbnails exist. It is a one-time cost per image; afterwards it is cached.
- **Playwright's `hover()` does not fire DOM mouse events in the VS Code
  embedded browser**, so the hover snapshot cannot be verified by driving it
  that way. Dispatching a synthetic `MouseEvent('mouseover')` does exercise the
  handler correctly. Worth knowing before concluding the feature is broken.
- **Static assets are cache-busted** with `?v={{ asset_v() }}` (newest mtime of
  `style.css`/`theme.css`/`media.js`/`pdfviewer.js`). Without it the browser
  keeps the old `style.css` across a restart and new CSS silently does nothing —
  which is exactly how the missing popup styles presented at first. Covers use a
  separate token, `?v={{ cover_v() }}`, derived from the covers directory, since
  regenerating the shelf rewrites every cover under the same URL.

---

## 10. The bookshelf

`/` and `/shelf` render the library as a wall of bookshelves; `/browse` is the
older stats dashboard. Both carry a view switch between Shelves / Dashboard /
Search, so the look costs no navigation.

Each collection becomes a book, standing on a plank, represented by a cover
rendered from its core book. Clicking opens the collection with its full file
list and per-file actions, and the shelf has its own filter box, so nothing is
reachable only through the decorative view.

### Cover selection

`/cover/{cid}` renders a page of the collection's most representative file
(rulebook > ebook > document for books; an image for asset collections) at
340 px wide, cached in `covers/`. Two details matter:

* **It does not blindly use page 1.** Plenty of PDFs lead with a table of
  contents, a credits page or a licence, which turns a whole shelf into
  identical grey spines. `_cover_page_index()` scores the first six pages on how
  much of each is covered by artwork and how little text it carries, penalises
  front-matter keywords, and takes the best. Page 1 wins ties, so books whose
  first page really is the cover are unaffected.
* **It falls through candidates.** A collection's best-looking file may still be
  unrenderable (PyMuPDF cannot read a webp map), so it tries up to eight and
  skips the ones that fail rather than giving up.

Cold render of all 758 covers takes ~6 minutes; afterwards they are served from
cache in milliseconds. 10 collections have no renderable cover (4 are empty) and
fall back to a blank cloth spine drawn in CSS, not a broken image.

### Result pages

`/search` and the filtered views carry the same visual language, because a
plain text list of a 12 GB image collection tells you nothing:

* **Collection results are cover cards**, not empty parchment boxes — the same
  artwork, the same hover lift, as the shelf.
* **File rows get a 40 px thumbnail** for images and PDFs (the PDF's cover
  page), and a small symbol for anything else.

Both load lazily and are cached, so a page renders immediately and fills in.

### Pre-generating the thumbnail cache

The on-demand path works, but it is slow in aggregate. The library holds 16,916
images averaging 2 MB with 359 over 20 MB, so the 500-row art page is **12.5 GB
of source images** and, at roughly 0.3 thumbnails/second, would take about half
an hour to fill in cold. Decoding is the cost, not downloading.

```sh
setsid nohup ionice -c2 -n7 nice -n 19 ./.venv/bin/python thumbnails.py \
    --library /mnt/disk5/RPG-Library --db ./library.db \
    > thumbnails.log 2>&1 < /dev/null &
```

* Builds **160 px and 320 px from a single decode** per file. Shrinking a
decoded pixmap is nearly free, so the list size and the hover size together
cost barely more than one. `/thumb` does the same thing on request, so a list
row asking for 160 also leaves the 320 hover behind.
* **Largest-first**, because lists are sorted by size descending — the top rows
  of every page are the biggest files, and they are the ones that need to be
  fast. Smallest-first looks better in the progress log and helps the wrong end
  of the page.
* **Resumable.** Cached files are skipped, so it can be stopped and restarted
  freely, and re-running after a sync only handles what is new.

Afterwards every list and hover is instant. The cache is safe to delete, but
the cost of rebuilding it is the reason this tool exists.

### Theme, and the assets it uses

`static/theme.css` is a skin layered over the layout primitives in `style.css`.
It redefines the palette, swaps the typography, adds the wood and parchment
textures and builds the shelf components.

Everything is **CSS or inline SVG data URIs** — wood grain, parchment mottling,
brass edges, shadow and vignette are all gradients and an `feTurbulence` filter.
Nothing is fetched from a CDN, so the look survives the machine being offline.

The only binaries are three self-hosted fonts in `static/fonts/`, all under the
**SIL Open Font License 1.1**, which permits bundling and redistribution:

| Font | Used for | Licence |
|---|---|---|
| Cinzel | headings, shelf titles, buttons, nav | OFL 1.1 |
| EB Garamond | body text | OFL 1.1 |
| IM Fell English | taglines and flavour text | OFL 1.1 |

They are Latin + Latin-Extended subsets only (12 files, 0.59 MB). To refresh
them, re-run the download snippet in the project history or fetch from Google
Fonts with a woff2 user-agent and keep the `/* latin */` blocks.

> **Two surfaces, two ink colours.** The room is dark oak and the panels are
> parchment, so text colour cannot be set once on `body` — light text on the oak
> would be invisible on the parchment. `theme.css` sets ink on each parchment
> component explicitly. Add a new panel and you must give it a colour.

---

## 11. MCP server

`mcp_server.py` exposes the library to an AI assistant over the Model Context
Protocol, so it can search the catalogue, search *inside* the books, and read
pages.

```sh
cd /mnt/disk5/rpg-app
./.venv/bin/python mcp_server.py       # speaks JSON-RPC on stdin/stdout
```

It is registered for this workspace in `/mnt/disk5/.vscode/mcp.json`.

> **stdout is the protocol channel.** Nothing in the server may `print()` — a
> single stray line corrupts the stream. Diagnostics go to stderr.

It **imports `app`** rather than re-implementing search, so ranking and FTS
behaviour are identical to the web UI. There is one search implementation in
this project and this is not a second one.

### Tools

| Tool | Use |
|---|---|
| `library_stats` | totals, breakdowns by category/kind/source, index size |
| `list_shelves` | what is on each shelf |
| `search_library` | find collections by title, publisher, family, shelf, category |
| `get_collection` | one collection's metadata and file list |
| `get_file` | one file's details and its open/thumbnail/raw links |
| `search_inside_books` | **full-text search across ~318,000 indexed pages**, with citations |
| `read_pages` | read the extracted text of a page range |
| `mechanic_across_systems` | count hits per collection, to see which games cover a topic |
| `find_series` | group numbered installments of one title |
| `browse_assets` | maps, art, tokens, audio, video |
| `random_pick` | serendipity — "what should we play" |

Resources: `library://shelves`, `library://stats`,
`library://collection/{cid}`. Prompts: `rules_lookup`, `compare_systems`.

Citations are emitted as full URLs built from `RPG_BASE_URL`
(default `http://192.168.44.50:8765`), so the assistant can hand back links that
open the right page of the right book.

The intended flow for a rules question is
`search_inside_books` → `read_pages` → answer with a page citation, which the
server's own instructions tell the model not to skip. That matters: hit counts
alone are a poor signal, and the server says so explicitly in
`compare_systems`.


## Great Library redesign (October 2026)

See [RELEASE-NOTES.md](RELEASE-NOTES.md) for the redesigned interface, reading desk, classification ledger, persistent annotations, maintenance security, regression tests, and backup/restore instructions. The classification rules are in `taxonomy.py`; manual choices are stored in `metadata_overrides` and survive scans.

See [FOLLOWUP-NOTES.md](FOLLOWUP-NOTES.md) for the October 7 search feedback, search-return navigation, TIFF previews, and per-file classification hierarchy.

Architecture decisions live in [`docs/adr/`](docs/adr/):

- [0001 — Search stays on SQLite FTS5](docs/adr/0001-search-architecture.md)
