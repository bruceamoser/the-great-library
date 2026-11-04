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
      --library /mnt/disk5/RPG-Library --db library.db [--workers 3] \
      [--rebuild] [--limit N] [--migrate [--vacuum]]
"""
import argparse
import os
import sqlite3
import sys
import time
from multiprocessing import Pool

MAX_PAGE_CHARS = 200_000

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
    ap.add_argument('--migrate', action='store_true',
                    help='upgrade the index schema, then exit without extracting')
    ap.add_argument('--vacuum', action='store_true',
                    help='VACUUM the database after --migrate')
    args = ap.parse_args()

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
        jobs.append((rel_path, abs_path, mtime))

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

    t0 = time.time()
    processed = skipped = failed = 0
    chars_total = 0
    batch = []
    cur_rows = []
    cur_pages = []

    def flush():
        nonlocal batch, cur_rows, cur_pages
        if not batch:
            return
        # Deleting the content rows fires the FTS delete triggers; inserting
        # them fires the insert triggers. The index is never written directly.
        con.executemany('DELETE FROM pdf_pages WHERE rel_path=?',
                        [(r,) for r in batch])
        con.executemany(
            'INSERT OR REPLACE INTO pdf_status VALUES (?,?,?,?,?)', cur_rows)
        con.executemany(
            'INSERT INTO pdf_pages(rel_path, page, text) VALUES (?,?,?)', cur_pages)
        con.commit()
        batch, cur_rows, cur_pages = [], [], []

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
                cur_pages.append((rel_path, page, txt))
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
        cur = con.execute(
            'DELETE FROM pdf_pages WHERE rel_path NOT IN '
            '(SELECT rel_path FROM files)')
        n_stale_pages = cur.rowcount
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
    con.execute('INSERT INTO pdf_fts(pdf_fts) VALUES(\'optimize\')')
    con.commit()
    con.close()
    return 1 if failed else 0


if __name__ == '__main__':
    from jobs import run
    sys.exit(run('index', lambda fd: main()))
