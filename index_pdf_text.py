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
Re-running is cheap: files whose mtime is unchanged are skipped unless
--rebuild is given.

IMPORTANT: the library lives on a single spinning disk. Many parallel workers
cause seek thrashing that starves everything else on the machine (the web app
has been observed stuck in uninterruptible disk sleep because of it). Keep
--workers low (2-4) and run under `ionice -c2 -n7` / `nice -n 19`, as the web
app does. Do NOT use `ionice -c3` (idle): on mq-deadline it is effectively
starved and runs ~25x slower.

Usage:
  ionice -c2 -n7 nice -n 19 python3 index_pdf_text.py \
      --library /mnt/disk5/RPG-Library --db library.db [--workers 3]
      [--rebuild] [--limit N]
"""
import argparse
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

MAX_PAGE_CHARS = 200_000

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--library', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--workers', type=int, default=3)
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    args = ap.parse_args()

    library = os.path.abspath(args.library)
    con = sqlite3.connect(args.db, timeout=60)
    con.executescript(SCHEMA)
    # FTS path columns are unindexed. A companion rowid map avoids scanning
    # every indexed page once per changed PDF during a full rebuild.
    mapped=con.execute("SELECT 1 FROM sqlite_master WHERE name='pdf_page_map'").fetchone()
    con.execute('CREATE TABLE IF NOT EXISTS pdf_page_map(rel_path TEXT NOT NULL, fts_rowid INTEGER PRIMARY KEY)')
    con.execute('CREATE INDEX IF NOT EXISTS pdf_page_path ON pdf_page_map(rel_path)')
    if not mapped:
        con.execute('INSERT INTO pdf_page_map SELECT rel_path,rowid FROM pdf_fts')
    con.commit()

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

    t0 = time.time()
    processed = skipped = failed = 0
    chars_total = 0
    batch = []
    cur_rows = []
    cur_fts = []

    def flush():
        nonlocal batch, cur_rows, cur_fts
        if not batch:
            return
        con.executemany('DELETE FROM pdf_fts WHERE rowid IN (SELECT fts_rowid FROM pdf_page_map WHERE rel_path=?)', [(r,) for r in batch])
        con.executemany('DELETE FROM pdf_page_map WHERE rel_path=?',[(r,) for r in batch])
        con.executemany(
            'INSERT OR REPLACE INTO pdf_status VALUES (?,?,?,?,?)', cur_rows)
        for rel, page, text in cur_fts:
            cursor=con.execute('INSERT INTO pdf_fts (rel_path,page,text) VALUES (?,?,?)',(rel,page,text))
            con.execute('INSERT INTO pdf_page_map VALUES (?,?)',(rel,cursor.lastrowid))
        con.commit()
        batch, cur_rows, cur_fts = [], [], []

    with Pool(args.workers) as pool:
        for n, res in enumerate(pool.imap_unordered(extract, jobs, chunksize=1), 1):
            rel_path, mtime, pages, chars, page_rows, err = res
            if err:
                failed += 1
                print(f"  FAIL {rel_path}: {err}", flush=True)
                continue
            if not page_rows:
                skipped += 1
            chars_total += chars
            cur_rows.append((rel_path, mtime, pages, chars, time.time()))
            for page, txt in page_rows:
                cur_fts.append((rel_path, page, txt))
            batch.append(rel_path)
            processed += 1
            if len(batch) >= 40:
                flush()
            if n % 250 == 0 or n == len(jobs):
                el = time.time() - t0
                print(f"  [{n}/{len(jobs)}] {el:.0f}s  "
                      f"{chars_total/1e6:.1f}M chars", flush=True)
    flush()

    el = time.time() - t0
    idx = con.execute('SELECT COUNT(*) FROM pdf_status').fetchone()[0]

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
    con.execute('INSERT INTO pdf_fts(pdf_fts) VALUES(\'optimize\')')
    con.commit()
    con.close()
    return 1 if failed else 0


if __name__ == '__main__':
    from jobs import run
    sys.exit(run('index', lambda fd: main()))
