#!/usr/bin/env python3
"""
Extract per-page text from every PDF in the library into the catalog, so the
web app can search *inside* documents, not just filenames.

Stores results in `library.db`:

  pdf_fts     virtual FTS5 table (rel_path, page, text)
  pdf_status  one row per indexed file (rel_path, mtime, pages, chars)

Keyed on `rel_path` rather than file id, so rebuilding the catalog with
`catalog.py` (which recreates the `files` table) does not orphan the index.

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

IMPORTANT: the library lives on a single spinning disk, and parallel PDF reads
are effectively random access, so extra workers thrash the disk instead of
going faster. Measured on the real library — 200 PDFs, cold, the same files
each time with the page cache evicted before every read:

    --workers 1    78 s   134 pages/s
    --workers 2   113 s    93 pages/s
    --workers 3   141 s    75 pages/s

One worker is therefore the default: on this disk it is ~1.8x faster than
three and far gentler on everything else using the disk (the web app has been
observed stuck in uninterruptible disk sleep when the indexer saturated it).
Raise --workers only if the library is on SSD. Run under `ionice -c2 -n7` /
`nice -n 19`, as the web app does. Do NOT use `ionice -c3` (idle): on
mq-deadline it is effectively starved and runs ~25x slower.

Usage:
  ionice -c2 -n7 nice -n 19 python3 index_pdf_text.py \
      --library /mnt/disk5/RPG-Library --db library.db [--workers 1]
      [--rebuild] [--limit N] [--batch-mb 12]
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
# One worker: on the library's spinning disk extra readers thrash instead of
# helping (measured 134 pages/s at 1 worker vs 75 at 3 — see the module
# docstring). Only raise this for a library on SSD.
DEFAULT_WORKERS = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS pdf_status (
  rel_path   TEXT PRIMARY KEY,
  mtime      REAL,
  pages      INTEGER,
  chars      INTEGER,
  indexed_at REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS pdf_fts USING fts5(
  rel_path UNINDEXED,
  page UNINDEXED,
  text,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""


def ensure_schema(con):
    """Create the index tables; returns the connection ready for writing."""
    con.executescript(SCHEMA)
    # FTS path columns are unindexed. A companion rowid map avoids scanning
    # every indexed page once per changed PDF during a full rebuild.
    mapped = con.execute(
        "SELECT 1 FROM sqlite_master WHERE name='pdf_page_map'").fetchone()
    con.execute('CREATE TABLE IF NOT EXISTS pdf_page_map'
                '(rel_path TEXT NOT NULL, fts_rowid INTEGER PRIMARY KEY)')
    con.execute('CREATE INDEX IF NOT EXISTS pdf_page_path '
                'ON pdf_page_map(rel_path)')
    if not mapped:
        con.execute('INSERT INTO pdf_page_map SELECT rel_path,rowid FROM pdf_fts')
    con.commit()
    return con


def extract(job):
    """Worker: return (rel_path, mtime, pages, chars, [(page, text), ...])."""
    rel_path, abs_path, mtime = job
    try:
        import pymupdf
        doc = pymupdf.open(abs_path)
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

    Row ids are handed out here rather than read back from SQLite, which lets
    `pdf_fts` and its companion rowid map be filled with two `executemany`
    calls per batch instead of one INSERT plus one `lastrowid` lookup per page.
    """

    def __init__(self, con, next_rowid, batch_mb=BATCH_MB,
                 batch_files=BATCH_FILES):
        self.con = con
        self.next_rowid = next_rowid
        self.batch_bytes = max(1, int(batch_mb * 1_000_000))
        self.batch_files_limit = batch_files
        self.files = 0              # files buffered since the last commit
        self.pages = 0              # pages written during this run
        self.commits = 0
        self._bytes = 0
        self._paths = []
        self._status = []
        self._pages = []
        self._map = []

    def add(self, rel_path, mtime, page_count, chars, page_rows):
        """Buffer one file; True when the batch is big enough to commit."""
        self._paths.append(rel_path)
        self._status.append((rel_path, mtime, page_count, chars, time.time()))
        for page, text in page_rows:
            rowid = self.next_rowid
            self.next_rowid += 1
            self._pages.append((rowid, rel_path, page, text))
            self._map.append((rel_path, rowid))
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
        old = self.con.execute(
            'SELECT fts_rowid FROM pdf_page_map WHERE rel_path IN (%s)'
            % marks, self._paths).fetchall()
        self.con.execute(
            'DELETE FROM pdf_page_map WHERE rel_path IN (%s)' % marks,
            self._paths)
        if old:
            self.con.executemany('DELETE FROM pdf_fts WHERE rowid = ?', old)
        self.con.executemany(
            'INSERT OR REPLACE INTO pdf_status VALUES (?,?,?,?,?)', self._status)
        if self._pages:
            self.con.executemany(
                'INSERT INTO pdf_fts (rowid, rel_path, page, text) VALUES (?,?,?,?)',
                self._pages)
            self.con.executemany(
                'INSERT INTO pdf_page_map (rel_path, fts_rowid) VALUES (?,?)',
                self._map)
        self.con.commit()
        self.commits += 1
        self._paths, self._status, self._pages, self._map = [], [], [], []
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
                         f'more than one thrashes the library disk)')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--batch-mb', type=float, default=BATCH_MB,
                    help=f'commit once this much page text is buffered '
                         f'(default {BATCH_MB}); lower loses less work to a '
                         f'kill, higher writes faster')
    return ap


def main():
    args = build_parser().parse_args()

    library = os.path.abspath(args.library)
    con = ensure_schema(sqlite3.connect(args.db, timeout=60))

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
        jobs.append((rel_path, abs_path, mtime))

    if args.limit:
        jobs = jobs[:args.limit]

    total_pdfs = len(rows)
    print(f"PDFs in catalog : {total_pdfs}")
    print(f"already indexed : {len(done)}")
    print(f"to process      : {len(jobs)}")
    if not jobs:
        print("nothing to do")
        return 0

    next_rowid = con.execute(
        'SELECT COALESCE(MAX(rowid), 0) + 1 FROM pdf_fts').fetchone()[0]
    writer = IndexWriter(con, next_rowid, batch_mb=args.batch_mb)
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
        # Self-heal: drop index rows whose file no longer exists in the catalog
        # (moving files changes rel_path and would otherwise orphan them).
        try:
            stale = con.execute(
                'SELECT rowid FROM pdf_fts WHERE rel_path NOT IN '
                '(SELECT rel_path FROM files)').fetchall()
            if stale:
                con.executemany('DELETE FROM pdf_fts WHERE rowid = ?', stale)
            n_stale = con.execute(
                'DELETE FROM pdf_status WHERE rel_path NOT IN '
                '(SELECT rel_path FROM files)').rowcount
            con.execute('DELETE FROM pdf_page_map WHERE rel_path NOT IN (SELECT rel_path FROM files)')
            con.commit()
            if n_stale or stale:
                print(f"pruned stale  : {n_stale} files, {len(stale)} pages")
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
