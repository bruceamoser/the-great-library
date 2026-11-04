#!/usr/bin/env python3
"""Pre-generate thumbnails for the whole library.

File lists ask for one 160px thumbnail per row and the hover snapshot asks for
320px. Generating those on demand *works*, but it is slow in aggregate: the
library holds 16,916 images averaging 2 MB with 359 over 20 MB, and a 500-row
art page is 12.5 GB of source images. Decoding, not downloading, is the cost --
about 0.3 s per image even at 2-way concurrency -- so a cold list page takes
roughly half an hour to fill in.

This walks the catalog once and builds both sizes from a single decode per
file. After it has run, every list and every hover is instant.

    ./.venv/bin/python thumbnails.py --library /mnt/disk5/RPG-Library \
        --db ./library.db

It is resumable: anything already in the cache is skipped, so stopping and
restarting is fine, and re-running after adding files only does the new ones.

Ordered **largest-first**, because list pages are sorted by size descending --
so the rows at the top of every list are the largest files, and doing those
first is what actually makes the pages you look at feel fast. The 359 files
over 20 MB are a small fraction of the count but the slowest to decode.

Run it detached, with idle-ish I/O priority, alongside anything else:

    setsid nohup ionice -c2 -n7 nice -n 19 ./.venv/bin/python thumbnails.py \
        --library /mnt/disk5/RPG-Library --db ./library.db \
        > thumbnails.log 2>&1 < /dev/null &
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as A                                    # noqa: E402

# 160 for list rows, 320 for the hover snapshot. The 1600 popup size stays
# on demand: it is one request at a time and much too big to cache in bulk.
WIDTHS = (160, 320)

# What we can actually decode. webp/gif/avif/svg are listed by media_of() but
# PyMuPDF cannot open them, so they are excluded rather than logged as failures.
THUMBABLE = ('.jpg', '.jpeg', '.png', '.bmp', '.tif', '.tiff', '.pdf')


def already_cached(prefix: str) -> bool:
    for width in WIDTHS:
        if not any(os.path.exists(f'{prefix}-{width}.{ext}')
                   for ext in ('jpg', 'png')):
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--library', default=A.LIBRARY)
    ap.add_argument('--db', default=A.DB)
    ap.add_argument('--limit', type=int, default=0,
                    help='stop after N files (0 = all)')
    ap.add_argument('--report-every', type=int, default=250)
    args = ap.parse_args()

    A.LIBRARY = args.library
    A.DB = args.db
    os.makedirs(A.THUMBS_DIR, exist_ok=True)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    marks = ','.join('?' * len(THUMBABLE))
    rows = con.execute(
        f'SELECT id, rel_path, filename, ext, size FROM files'
        f' WHERE ext IN ({marks}) ORDER BY size DESC', THUMBABLE).fetchall()
    con.close()

    total = len(rows)
    print(f'thumbnails: {total:,} candidate files, widths {", ".join(map(str, WIDTHS))}',
          flush=True)

    from jobs import record
    record('thumbnails', total=total, built=0, cached=0, failed=0)
    made = skipped = failed = missing = 0
    bytes_in = 0
    started = time.time()
    failures: list[str] = []

    for i, row in enumerate(rows, 1):
        if args.limit and made >= args.limit:
            break
        path = os.path.join(A.LIBRARY, row['rel_path'])
        if not os.path.isfile(path):
            missing += 1
            continue
        try:
            prefix = os.path.join(
                A.THUMBS_DIR, f"{row['id']}-{int(os.path.getmtime(path))}")
        except OSError:
            missing += 1
            continue

        if already_cached(prefix):
            skipped += 1
            continue

        try:
            with A._thumbnail_slot():          # same memory guard as the live route
                A._build_thumbs(path, prefix, WIDTHS)
            made += 1
            bytes_in += row['size'] or 0
        except Exception as exc:        # noqa: BLE001
            failed += 1
            if len(failures) < 25:
                failures.append(f'{row["filename"][:60]}: {exc}')

        if i % args.report_every == 0:
            record('thumbnails', completed=i, built=made, cached=skipped, failed=failed)
            done = made + skipped
            rate = made / max(time.time() - started, 1e-6)
            left = (total - i) / rate if rate else 0
            print(f'  {i:>6,}/{total:,}  built {made:,}  cached {skipped:,}  '
                  f'failed {failed}  {rate:.1f}/s  eta {left / 60:.0f}m',
                  flush=True)

    elapsed = time.time() - started
    print(f'\ndone in {elapsed / 60:.1f} min', flush=True)
    print(f'  built   : {made:,}', flush=True)
    print(f'  cached  : {skipped:,}', flush=True)
    print(f'  failed  : {failed:,}', flush=True)
    if missing:
        print(f'  missing : {missing:,} (not on disk)', flush=True)
    if bytes_in:
        print(f'  decoded : {bytes_in / 1e9:.1f} GB of source images', flush=True)
    if failures:
        print('\nfirst failures:', flush=True)
        for line in failures:
            print(f'  {line}', flush=True)
    record('thumbnails', completed=made+skipped+failed+missing, built=made, cached=skipped, failed=failed, errors=failures)
    return 1 if failed else 0


if __name__ == '__main__':
    from jobs import run
    sys.exit(run('thumbnails', lambda fd: main()))
