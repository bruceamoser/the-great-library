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
statement per *batch* instead of two per page. During `--rebuild` the
connection also switches to `journal_mode=MEMORY`, `synchronous=OFF` and a
larger cache: the index is rebuildable, so crash durability is not worth
paying for while it is being rebuilt. The previous pragmas are put back
afterwards (`journal_mode` is persistent, so leaving it in MEMORY would make
ordinary writes to the live catalogue crash-unsafe).

Re-running is cheap: files whose mtime is unchanged are skipped unless
--rebuild is given. Every commit is a resume point, so an interrupted run
leaves a consistent index that the next run continues from — Ctrl-C flushes the
open batch first, and killing the process outright only loses the batch that
had not been committed yet.

IMPORTANT: the library lives on a single spinning disk. Many parallel workers
cause seek thrashing that starves everything else on the machine (the web app
has been observed stuck in uninterruptible disk sleep because of it). Keep
--workers low (2-4) and run under `ionice -c2 -n7` / `nice -n 19`, as the web
app does. Do NOT use `ionice -c3` (idle): on mq-deadline it is effectively
starved and runs ~25x slower.

Usage:
  ionice -c2 -n7 nice -n 19 python3 index_pdf_text.py \
      --library /mnt/disk5/RPG-Library --db library.db [--workers 3]
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
    """Rebuild-only pragmas; returns the previous settings so they can be undone.

    Only for `--rebuild`: the index is rebuildable, so losing the open
    transaction to a crash is acceptable while it is being rebuilt, and not
    paying for durability is most of the win. An incremental run keeps the
    catalogue's normal (WAL) durability.
    """
    prior = {
        'journal_mode': con.execute('PRAGMA journal_mode').fetchone()[0],
        'synchronous': con.execute('PRAGMA synchronous').fetchone()[0],
        'cache_size': con.execute('PRAGMA cache_size').fetchone()[0],
    }
    mode = prior['journal_mode']
    try:
        mode = con.execute('PRAGMA journal_mode = MEMORY').fetchone()[0]
    except sqlite3.OperationalError as exc:
        # The web app holds the database open in WAL mode and SQLite will not
        # switch a database that another connection is using.
        print(f"  note: kept journal_mode={mode} ({exc})")
    con.execute('PRAGMA synchronous = OFF')
    con.execute(f'PRAGMA cache_size = -{BULK_CACHE_MB * 1024}')
    print(f"rebuild pragmas : journal_mode={mode} synchronous=OFF "
          f"cache={BULK_CACHE_MB}MB")
    return prior


def restore_pragmas(con, prior):
    """Put back what tune_for_bulk() changed, so the live catalogue is safe."""
    con.execute(f"PRAGMA cache_size = {prior['cache_size']}")
    con.execute(f"PRAGMA synchronous = {prior['synchronous']}")
    want = prior['journal_mode'].lower()
    if con.execute('PRAGMA journal_mode').fetchone()[0].lower() == want:
        return
    # Leaving a live catalogue in journal_mode=MEMORY would make ordinary
    # writes crash-unsafe, so retry briefly in case the web app happens to be
    # mid-transaction, then say so loudly rather than failing silently.
    for attempt in range(5):
        try:
            con.execute(f'PRAGMA journal_mode = {want}')
        except sqlite3.OperationalError:
            pass
        else:
            if con.execute('PRAGMA journal_mode').fetchone()[0].lower() == want:
                return
        if attempt < 4:
            time.sleep(1)
    print(f"  WARNING: could not restore journal_mode={want}; run "
          f"`PRAGMA journal_mode={want};` on the catalogue once the app is "
          f"idle (a MEMORY journal is not crash-safe)", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--library', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--batch-mb', type=float, default=BATCH_MB,
                    help=f'commit once this much page text is buffered '
                         f'(default {BATCH_MB}); lower loses less work to a '
                         f'kill, higher writes faster')
    args = ap.parse_args()

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
    prior = None
    try:
        if args.rebuild:
            prior = tune_for_bulk(con)
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
        if prior is not None:
            restore_pragmas(con, prior)

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
