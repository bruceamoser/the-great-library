#!/usr/bin/env python3
"""
Extract per-page text from every PDF in the library into the catalog, so the
web app can search *inside* documents, not just filenames.

Stores results in `library.db`:

  pdf_pages   one row per page: id, rel_path, page, text
  pdf_fts     FTS5 index over pdf_pages.text (external content, content_rowid=id)
  pdf_status  one row per indexed file (rel_path, mtime, pages, chars)

The text lives once, in `pdf_pages`; `pdf_fts` indexes it by integer rowid and
reads the text back from `pdf_pages` (external content). The old layout kept
the text in the FTS table's own content table, stored the full relative path
and page number on every one of ~310k FTS rows, and joined hits back to
`files` on a string.

`pdf_pages` is keyed on `rel_path` rather than file id, so rebuilding the
catalog with `catalog.py` (which recreates the `files` table) does not orphan
the index. It is not a catalog table and survives a rescan.

Upgrading an existing database: `--migrate` copies the text out of the old
`pdf_fts` layout, rebuilds the index against `pdf_pages`, drops the obsolete
`pdf_page_map` and (optionally) VACUUMs. It is idempotent and is also run
automatically at the start of every indexing pass. `--rebuild` still means
"re-extract every PDF"; the migration is not a rebuild.

Runs the extraction across a process pool; only the parent writes to SQLite.
The parent buffers page text and writes it with `executemany` in transactions
sized by volume (about 12 MB of text, or 200 files, per commit), so a rebuild
costs one commit per few hundred files instead of one per 40 files, and one
statement per *batch* instead of two per page.

Writing is not what makes a rebuild slow. Measured against a full-size index
(309,790 pages, 1.25 GB of text): writing every page costs about 35 s and the
terminating FTS `optimize` about 5 s, against roughly 70 min of reading PDFs
off the disk. So `--rebuild` only relaxes *per-connection* settings — a 64 MB
cache and `synchronous=NORMAL`, both of which disappear when the connection
closes. That is safe in WAL mode: it can lose the last few commits to a power
cut (they are redone next run) but cannot corrupt the file, which also holds
the catalogue. `journal_mode` is deliberately left alone: it is stored in the
database file, so switching it to MEMORY and being killed before switching
back would leave the live catalogue crash-unsafe for the web app.

Re-running is cheap: files whose mtime is unchanged are skipped unless
--rebuild is given. Every commit is a resume point, so an interrupted run
leaves a consistent index that the next run continues from — Ctrl-C flushes the
open batch first, and killing the process outright only loses the batch that
had not been committed yet.

READING is what makes a rebuild slow, and the read *pattern* was the problem.
Left to itself MuPDF walks each file's object graph and issues many small,
scattered reads, which on a single spinning disk becomes a stream of seeks.
Reading each file in one sequential pass first (`open_pdf()`, whole file into
memory) and parsing it from there removes that.

Measured on the real library, cold — the page cache evicted before every read,
60 random files, 881 MB, 4,022 pages, identical text extracted in every row:

    workers  read pattern    time    throughput
       3     scattered      57.4 s    69 pages/s
       1     sequential     31.4 s   128 pages/s
       2     sequential     16.9 s   238 pages/s
       3     sequential     14.9 s   271 pages/s
       4     sequential     15.1 s   266 pages/s

The worker count only matters together with the read pattern: with scattered
reads more workers *hurt*, with sequential reads they help until the disk
saturates (three, here) — hence DEFAULT_WORKERS = 3. Three workers streaming is
about 3.9x the old configuration, and finishes sooner, so the disk is busy for
far less time overall. Files above --stream-max-mb (default 256 MB, above the
library's 99th percentile) are opened in place, so worker memory stays bounded.

Run under `ionice -c2 -n7` / `nice -n 19`, as the web app does. Do NOT use
`ionice -c3` (idle): on mq-deadline it is effectively starved and runs ~25x
slower.

Usage:
  ionice -c2 -n7 nice -n 19 python3 index_pdf_text.py \
      --library /mnt/disk5/RPG-Library --db library.db [--workers 3] \
      [--rebuild] [--limit N] [--migrate [--vacuum]] \
      [--batch-mb 12] [--stream-max-mb 256]
"""
import argparse
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

MAX_PAGE_CHARS = 200_000

# Commit sizing. Both limits apply: stop at 12 MB of page text *or* 200 files
# so memory stays flat and a kill loses at most one batch.
BATCH_MB = 12
BATCH_FILES = 200
BULK_CACHE_MB = 64
# Read each PDF as one sequential run instead of letting MuPDF issue its own
# scattered small reads; see open_pdf(). Files above the cap are opened in
# place, so worker memory stays bounded (the library's p99 file is 122 MB).
STREAM_MAX_MB = 256
# Three workers, each reading one whole file sequentially, is the fastest
# configuration measured on the library's spinning disk: 3.86x the throughput
# of the same worker count reading scattered (see the module docstring). Both
# fewer and more workers were slower.
DEFAULT_WORKERS = 3

STATUS_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_status (
  rel_path   TEXT PRIMARY KEY,
  mtime      REAL,
  pages      INTEGER,
  chars      INTEGER,
  indexed_at REAL
);
"""

PAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_pages (
  id       INTEGER PRIMARY KEY,
  rel_path TEXT NOT NULL,
  page     INTEGER,
  text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pdf_pages_path_page ON pdf_pages(rel_path, page);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE pdf_fts USING fts5(
  text,
  content = 'pdf_pages',
  content_rowid = 'id',
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Keep pdf_fts in sync with its content table. The explicit 'delete' command
# with the old text is what FTS5 external-content tables require; a plain
# DELETE against pdf_fts would leave the index claiming tokens that are gone.
TRIGGER_SCHEMA = """
CREATE TRIGGER IF NOT EXISTS pdf_pages_ai AFTER INSERT ON pdf_pages BEGIN
  INSERT INTO pdf_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS pdf_pages_ad AFTER DELETE ON pdf_pages BEGIN
  INSERT INTO pdf_fts(pdf_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
CREATE TRIGGER IF NOT EXISTS pdf_pages_au AFTER UPDATE ON pdf_pages BEGIN
  INSERT INTO pdf_fts(pdf_fts, rowid, text) VALUES ('delete', old.id, old.text);
  INSERT INTO pdf_fts(rowid, text) VALUES (new.id, new.text);
END;
"""

TRIGGERS = ('pdf_pages_ai', 'pdf_pages_ad', 'pdf_pages_au')


def _table_exists(con, name):
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE name = ? AND type IN ('table', 'view')",
        (name,)).fetchone() is not None


def _columns(con, name):
    return [row[1] for row in con.execute(f'PRAGMA table_info("{name}")')]


def _triggers_present(con):
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name IN (?,?,?)",
        TRIGGERS).fetchall()
    return len(rows) == len(TRIGGERS)


def migrate(con, log=print):
    """Upgrade the PDF page index to integer keys + external content.

    Idempotent: a database already on the new layout is left untouched (no
    rebuild), so this is cheap to call before every indexing pass. An
    interrupted migration is detected by the missing triggers and redone; the
    text is never lost before the new index has been committed.
    """
    con.executescript(STATUS_SCHEMA)
    has_fts = _table_exists(con, 'pdf_fts')
    columns = _columns(con, 'pdf_fts') if has_fts else []
    old_layout = 'rel_path' in columns
    has_pages = _table_exists(con, 'pdf_pages')

    if has_pages and not old_layout and _triggers_present(con):
        # Already current. The index statement is IF NOT EXISTS and covers the
        # case where somebody dropped just the b-tree.
        con.executescript(PAGES_SCHEMA)
        return False

    if old_layout:
        for trigger in TRIGGERS:
            con.execute(f'DROP TRIGGER IF EXISTS {trigger}')
        con.executescript(PAGES_SCHEMA)
        # Clear a partial copy from an interrupted earlier attempt before
        # recopying; the old index is still intact at this point.
        con.execute('DELETE FROM pdf_pages')
        copied = con.execute(
            "INSERT INTO pdf_pages(rel_path, page, text) "
            "SELECT rel_path, CAST(page AS INTEGER), text FROM pdf_fts "
            "WHERE text IS NOT NULL").rowcount
        con.commit()
        log(f'  copied {copied:,} pages from the old pdf_fts')
        con.execute('DROP TABLE pdf_fts')
        con.execute('DROP TABLE IF EXISTS pdf_page_map')
        con.commit()
    else:
        con.executescript(PAGES_SCHEMA)

    # Build a fresh index against the content table. A leftover pdf_fts from an
    # interrupted migration is dropped first so its segments cannot disagree
    # with pdf_pages.
    con.execute('DROP TABLE IF EXISTS pdf_fts')
    con.executescript(FTS_SCHEMA)
    pages = con.execute('SELECT COUNT(*) FROM pdf_pages').fetchone()[0]
    log(f'  rebuilding the FTS index over {pages:,} pages ...')
    con.execute("INSERT INTO pdf_fts(pdf_fts) VALUES('rebuild')")
    con.commit()
    con.executescript(TRIGGER_SCHEMA)
    con.commit()
    log('  index schema: pdf_pages + pdf_fts (external content)')
    return True


def _bytes(n):
    return f'{n / 1e9:.2f} GB' if n >= 1e9 else f'{n / 1e6:.1f} MB'


def _report_sizes(con, path, before):
    """Print the on-disk size and freelist, before and after a VACUUM."""
    page_size = con.execute('PRAGMA page_size').fetchone()[0]
    free = con.execute('PRAGMA freelist_count').fetchone()[0]
    con.commit()
    con.execute('VACUUM')
    con.commit()
    # Fold the WAL back into the main file so the size we print is real.
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    after = os.path.getsize(path) if os.path.exists(path) else 0
    print(f'database      : {_bytes(before)} -> {_bytes(after)} '
          f'({_bytes(max(0, before - after))} reclaimed)')
    print(f'freelist      : {free:,} pages ({_bytes(free * page_size)}) before VACUUM')


def ensure_schema(con):
    """Create or upgrade the index; returns the connection ready for writing."""
    migrate(con, log=lambda _message: None)
    return con


def open_pdf(pymupdf, abs_path, stream_max_bytes):
    """Open a PDF, reading files small enough to fit in one sequential pass.

    MuPDF walks a file's object graph as it parses, so its reads arrive in an
    order that is not sequential on disk. On the library's spinning disk that
    is what made a full rebuild slow: the same workers reading whole files
    sequentially run 3.86x faster, and *gain* from running in parallel instead
    of losing, because one large read costs one seek. Files above
    `stream_max_bytes` are opened in place, so worker memory stays bounded.
    """
    if stream_max_bytes > 0:
        try:
            size = os.path.getsize(abs_path)
        except OSError:
            size = None
        if size is not None and size <= stream_max_bytes:
            try:
                with open(abs_path, 'rb') as fh:
                    data = fh.read()
            except OSError:
                data = None
            if data is not None:
                try:
                    return pymupdf.open(stream=data, filetype='pdf')
                except Exception:
                    # Forcing the PDF handler can fail where sniffing would
                    # succeed (a mislabelled file), so fall back to the path.
                    pass
    return pymupdf.open(abs_path)


def extract(job):
    """Worker: return (rel_path, mtime, pages, chars, [(page, text), ...])."""
    rel_path, abs_path, mtime, stream_max_bytes = job
    try:
        import pymupdf
        doc = open_pdf(pymupdf, abs_path, stream_max_bytes)

        rows = []
        total = 0
        for i in range(doc.page_count):
            try:
                txt = doc.load_page(i).get_text() or ''
            except Exception:
                txt = ''
            if len(txt) > MAX_PAGE_CHARS:
                txt = txt[:MAX_PAGE_CHARS]
            if txt.strip():
                rows.append((i + 1, txt))
                total += len(txt)
        pages = doc.page_count
        doc.close()
        return (rel_path, mtime, pages, total, rows, None)
    except Exception as exc:  # noqa: BLE001
        return (rel_path, mtime, 0, 0, [], str(exc)[:200])


class IndexWriter:
    """Buffer extracted pages and write them in a few large transactions.

    Rows go into `pdf_pages`, the content table; the triggers created by
    `migrate()` feed the external-content `pdf_fts` index, so the writer never
    touches the index directly and never needs to know a rowid. One
    `executemany` per table per batch is what keeps a rebuild fast.
    """

    def __init__(self, con, batch_mb=BATCH_MB, batch_files=BATCH_FILES):
        self.con = con
        self.batch_bytes = max(1, int(batch_mb * 1_000_000))
        self.batch_files_limit = batch_files
        self.files = 0              # files buffered since the last commit
        self.pages = 0              # pages written during this run
        self.commits = 0
        self._bytes = 0
        self._paths = []
        self._status = []
        self._pages = []

    def add(self, rel_path, mtime, page_count, chars, page_rows):
        """Buffer one file; True when the batch is big enough to commit."""
        self._paths.append(rel_path)
        self._status.append((rel_path, mtime, page_count, chars, time.time()))
        for page, text in page_rows:
            self._pages.append((rel_path, page, text))
            # Book text is overwhelmingly ASCII, so the character count tracks
            # the byte size closely enough to size a transaction.
            self._bytes += len(text)
        self.files += 1
        self.pages += len(page_rows)
        return self._bytes >= self.batch_bytes or self.files >= self.batch_files_limit

    def flush(self):
        """Write the buffered batch in one transaction. Safe to call twice."""
        if not self._paths:
            return
        marks = ','.join('?' * len(self._paths))
        # Deleting the content rows fires the FTS delete triggers; inserting
        # the new ones fires the insert triggers. The index is never written
        # directly.
        self.con.execute(
            'DELETE FROM pdf_pages WHERE rel_path IN (%s)' % marks,
            self._paths)
        self.con.executemany(
            'INSERT OR REPLACE INTO pdf_status VALUES (?,?,?,?,?)', self._status)
        if self._pages:
            self.con.executemany(
                'INSERT INTO pdf_pages (rel_path, page, text) VALUES (?,?,?)',
                self._pages)
        self.con.commit()
        self.commits += 1
        self._paths, self._status, self._pages = [], [], []
        self._bytes = 0
        self.files = 0


def tune_for_bulk(con):
    """Rebuild-only pragmas for *this connection*.

    Both settings are per-connection and vanish when the connection closes, so
    nothing weaker can leak into the live catalogue. `journal_mode` is
    deliberately not touched: it is persistent in the database file, and a
    rebuild killed between switching it to MEMORY and switching it back would
    leave the web app writing to a crash-unsafe database.

    `synchronous=NORMAL` rather than `OFF`: in WAL mode NORMAL cannot corrupt
    the database (it can only lose the last transactions after a power cut,
    which for a rebuild just means redoing them), while `OFF` can corrupt it —
    and this file also holds the catalogue. Measured over 60,000 pages the
    difference is 6.0 s vs 5.7 s, about 2 s across a whole rebuild, so there
    is nothing to buy by taking that risk.
    """
    con.execute('PRAGMA synchronous = NORMAL')
    con.execute(f'PRAGMA cache_size = -{BULK_CACHE_MB * 1024}')
    print(f"rebuild pragmas : journal_mode="
          f"{con.execute('PRAGMA journal_mode').fetchone()[0]} "
          f"synchronous=NORMAL cache={BULK_CACHE_MB}MB")


def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument('--library', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS,
                    help=f'extraction processes (default {DEFAULT_WORKERS}: '
                         f'each worker reads one file sequentially, which is '
                         f'the fastest measured on the library disk; try 1 on '
                         f'a slow or heavily used machine)')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--migrate', action='store_true',
                    help='upgrade the index schema, then exit without extracting')
    ap.add_argument('--vacuum', action='store_true',
                    help='VACUUM the database after --migrate')
    ap.add_argument('--stream-max-mb', type=float, default=STREAM_MAX_MB,
                    help=f'read PDFs up to this size into memory before '
                         f'parsing (default {STREAM_MAX_MB}); 0 disables and '
                         f'lets MuPDF read the file itself, which is much '
                         f'slower on a spinning disk')
    ap.add_argument('--batch-mb', type=float, default=BATCH_MB,
                    help=f'commit once this much page text is buffered '
                         f'(default {BATCH_MB}); lower loses less work to a '
                         f'kill, higher writes faster')
    return ap


def main():
    args = build_parser().parse_args()

    library = os.path.abspath(args.library)
    existed = os.path.exists(args.db)
    before = os.path.getsize(args.db) if existed else 0
    con = sqlite3.connect(args.db, timeout=60)
    migrate(con)
    if args.migrate:
        if args.vacuum:
            _report_sizes(con, args.db, before)
        con.close()
        return 0

    done = {}
    if not args.rebuild:
        for rel, mtime in con.execute('SELECT rel_path, mtime FROM pdf_status'):
            done[rel] = mtime

    rows = con.execute(
        "SELECT rel_path, size FROM files WHERE ext = '.pdf'").fetchall()
    jobs = []
    for rel_path, _size in rows:
        abs_path = os.path.join(library, rel_path)
        if not os.path.exists(abs_path):
            continue
        mtime = os.path.getmtime(abs_path)
        if done.get(rel_path) == mtime:
            continue
        jobs.append((rel_path, abs_path, mtime,
                     int(args.stream_max_mb * 1024 * 1024)))

    if args.limit:
        jobs = jobs[:args.limit]

    total_pdfs = len(rows)
    print(f"PDFs in catalog : {total_pdfs}")
    print(f"already indexed : {len(done)}")
    print(f"to process      : {len(jobs)}")
    if not jobs:
        print("nothing to do")
        con.close()
        return 0

    writer = IndexWriter(con, batch_mb=args.batch_mb)
    t0 = time.time()
    processed = skipped = failed = 0
    chars_total = 0
    interrupted = False
    try:
        if args.rebuild:
            tune_for_bulk(con)
        try:
            with Pool(args.workers) as pool:
                for n, res in enumerate(
                        pool.imap_unordered(extract, jobs, chunksize=1), 1):
                    rel_path, mtime, pages, chars, page_rows, err = res
                    if err:
                        failed += 1
                        print(f"  FAIL {rel_path}: {err}", flush=True)
                        continue
                    if not page_rows:
                        skipped += 1
                    chars_total += chars
                    processed += 1
                    if writer.add(rel_path, mtime, pages, chars, page_rows):
                        writer.flush()
                    if n % 250 == 0 or n == len(jobs):
                        el = time.time() - t0
                        print(f"  [{n}/{len(jobs)}] {el:.0f}s  "
                              f"{chars_total/1e6:.1f}M chars  "
                              f"{writer.pages/max(el, 1e-6):.0f} pages/s",
                              flush=True)
        except KeyboardInterrupt:
            # Keep what is finished: every commit is a resume point, and the
            # final flush below makes even the open batch durable.
            interrupted = True
            print("\ninterrupted: committing finished work so the next run "
                  "resumes", flush=True)
    finally:
        writer.flush()

    el = time.time() - t0
    idx = con.execute('SELECT COUNT(*) FROM pdf_status').fetchone()[0]

    if not interrupted:
        # Self-heal: drop content rows whose file no longer exists in the
        # catalog (moving files changes rel_path and would otherwise orphan
        # them). Deleting from pdf_pages fires the FTS delete triggers.
        try:
            n_stale_pages = con.execute(
                'DELETE FROM pdf_pages WHERE rel_path NOT IN '
                '(SELECT rel_path FROM files)').rowcount
            n_stale = con.execute(
                'DELETE FROM pdf_status WHERE rel_path NOT IN '
                '(SELECT rel_path FROM files)').rowcount
            con.commit()
            if n_stale or n_stale_pages:
                print(f"pruned stale  : {n_stale} files, {n_stale_pages} pages")
        except sqlite3.OperationalError:
            pass

    print()
    print(f"processed      : {processed}  ({el:.0f}s)")
    print(f"  no text layer: {skipped}  (image-only, needs OCR)")
    print(f"  failed       : {failed}")
    print(f"text extracted : {chars_total/1e6:.1f} M chars")
    print(f"files indexed  : {idx}")
    print(f"throughput     : {processed/max(el, 1e-6):.2f} files/s, "
          f"{writer.pages/max(el, 1e-6):.0f} pages/s  ({writer.commits} commits)")
    if interrupted:
        print("interrupted    : index is consistent; rerun to continue")
        con.close()
        return 130

    con.execute('INSERT INTO pdf_fts(pdf_fts) VALUES(\'optimize\')')
    con.commit()
    con.close()
    return 1 if failed else 0


if __name__ == '__main__':
    from jobs import run
    sys.exit(run('index', lambda fd: main()))
