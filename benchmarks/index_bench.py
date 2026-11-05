#!/usr/bin/env python3
"""
Offline benchmark for PDF text extraction -- the part of a rebuild that costs
the time.

`index_pdf_text.py` reads every PDF and pulls the text out of every page; on the
real library that is ~96% of a full rebuild, and the rest (writing pages into
SQLite) is under a minute. Whether a change helps therefore depends almost
entirely on extraction throughput, which in turn depends on the *read pattern*
and the worker count, not on the database. This script measures just that, so
the two can be compared without rebuilding a 1.9 GB index each time.

It runs the real `index_pdf_text.extract()` through a real
`multiprocessing.Pool`, `chunksize=1`, exactly as `main()` does. Nothing is
re-implemented here: `open_pdf()` decides how each file is read, so `--patterns
streamed` measures the shipped path and `inplace` measures what the code did
before streamed reads existed (`--stream-max-mb 0`). **No database is opened or
written** -- it only reads PDFs -- so it is safe to point at the live library
while the app is serving.

The catch, and the reason this script exists rather than a one-liner, is the
page cache. The host has 30 GB of RAM against a 67 GB library, so a run that
follows another run is partly warm and can report two to three times the real
throughput. Every measured run therefore evicts the sample's clean pages with
`posix_fadvise(POSIX_FADV_DONTNEED)` first, the configurations are interleaved
round by round so any drift in disk or CPU state hits all of them equally, and
the extracted page and character counts are checked to be identical -- a
"faster" configuration that quietly extracted less text would otherwise look
like a win. `--no-evict` runs warm on purpose, which is the control that shows
eviction is doing something: on a spinning disk scattered reads collapse
without cache, while streamed reads only slow down.

Usage:
  .venv/bin/python benchmarks/index_bench.py --library /mnt/disk5/RPG-Library
  .venv/bin/python benchmarks/index_bench.py --library ... --sample 120 --seed 7
  .venv/bin/python benchmarks/index_bench.py --library ... --workers 3,4
  .venv/bin/python benchmarks/index_bench.py --library ... --no-evict   # control

On the host, run it under the same idle I/O priority as a rebuild:

  ionice -c2 -n7 nice -n 19 ./.venv/bin/python benchmarks/index_bench.py \
      --library /mnt/disk5/RPG-Library --sample 120 --rounds 2

Measure a change against the same `--sample`/`--seed` and the same idle machine.
`--json` prints one object instead of the table, for a regression check.
"""
import argparse
import json
import multiprocessing
import os
import random
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import index_pdf_text  # noqa: E402  (needs ROOT on the path first)

PATTERNS = ('streamed', 'inplace')

# Human-readable output goes here; --json redirects it to stderr so that
# stdout carries nothing but the JSON object.
_OUT = sys.stdout


def say(*args):
    print(*args, file=_OUT)


def pick_sample(library, sample, seed):
    """Return a stable, sorted list of PDFs under `library`."""
    found = []
    for root, _dirs, names in os.walk(library):
        for name in names:
            if name.lower().endswith('.pdf'):
                found.append(os.path.join(root, name))
    found.sort()
    if sample and sample < len(found):
        found = sorted(random.Random(seed).sample(found, sample))
    return found


def library_mib(library):
    """Total size of every PDF, for projecting a full rebuild."""
    total = 0
    for root, _dirs, names in os.walk(library):
        for name in names:
            if name.lower().endswith('.pdf'):
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
    return total / 2 ** 20


def evict(paths):
    """Drop clean page-cache pages so the next run reads from the disk."""
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        except OSError:
            pass
        finally:
            os.close(fd)


def run_once(jobs, workers):
    """Extract `jobs` with a pool shaped like the real one; return the totals."""
    started = time.perf_counter()
    pages = chars = files = failed = 0
    with multiprocessing.Pool(workers) as pool:
        for _rel, _mtime, n_pages, n_chars, _rows, err in pool.imap_unordered(
                index_pdf_text.extract, jobs, chunksize=1):
            if err:
                failed += 1
                continue
            files += 1
            pages += n_pages
            chars += n_chars
    return time.perf_counter() - started, files, pages, chars, failed


def build_jobs(paths, library, stream_max_mb):
    stream_bytes = int(stream_max_mb * 2 ** 20)
    return [(os.path.relpath(path, library), path, os.path.getmtime(path),
             stream_bytes) for path in paths]


def measure(paths, library, workers, patterns, stream_max_mb, rounds, evict_cache):
    """Run every (workers, pattern) combination once per round, interleaved."""
    results = {}
    expected = None
    say(f'sample        : {len(paths):,} PDFs, '
          f'{sum(os.path.getsize(p) for p in paths) / 2 ** 20:,.0f} MiB')
    say(f'rounds        : {rounds}, configurations interleaved, cache '
          f'{"left warm" if not evict_cache else "evicted before each run"}')
    say()
    for rnd in range(rounds):
        for count in workers:
            for pattern in patterns:
                jobs = build_jobs(
                    paths, library,
                    0 if pattern == 'inplace' else stream_max_mb)
                if evict_cache:
                    evict(paths)
                elapsed, files, pages, chars, failed = run_once(jobs, count)
                label = f'{count}w {pattern}'
                results.setdefault(label, []).append(
                    (elapsed, files, pages, chars, failed))
                if expected is None:
                    expected = (pages, chars)
                elif (pages, chars) != expected:
                    sys.exit(f'{label} extracted {pages} pages / {chars} '
                             f'chars, expected {expected[0]} / {expected[1]}: '
                             f'configurations are not measuring the same work')
                say(f'round {rnd + 1}  {label:16}{elapsed:8.2f} s  '
                      f'{pages / elapsed:6.0f} pages/s  '
                      f'{files / elapsed:6.2f} files/s  failed={failed}')
    say()
    return results, expected


def report(results, expected, sample_mib):
    base = '3w streamed' if '3w streamed' in results else next(iter(results))
    baseline = statistics.median(t for t, *_ in results[base])
    header = (f'{"configuration":18}{"best s":>9}{"median s":>10}'
              f'{"pages/s":>10}{"MiB/s":>8}{"files/s":>9}{"vs base":>10}')
    say(header)
    say('-' * len(header))
    rows = []
    for label, runs in results.items():
        times = [r[0] for r in runs]
        best, median = min(times), statistics.median(times)
        pages = runs[0][2]
        row = {'configuration': label, 'best_s': round(best, 2),
               'median_s': round(median, 2),
               'pages_s': round(pages / median, 1),
               'mib_s': round(sample_mib / median, 1),
               'files_s': round(runs[0][1] / median, 2),
               'vs_base': round(baseline / median, 2)}
        rows.append(row)
        say(f'{label:18}{best:9.2f}{median:10.2f}{row["pages_s"]:10.0f}'
              f'{row["mib_s"]:8.1f}{row["files_s"]:9.2f}'
              f'{row["vs_base"]:9.2f}x')
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Time PDF text extraction on a real library. Read-only: '
                    'no database is opened.')
    parser.add_argument('--library', required=True,
                        help='library root holding the PDFs (read-only)')
    parser.add_argument('--sample', type=int, default=60,
                        help='PDFs to sample; 0 uses every file (default 60)')
    parser.add_argument('--seed', type=int, default=1234,
                        help='sample seed, so two runs measure the same files')
    parser.add_argument('--rounds', type=int, default=2,
                        help='rounds over every configuration (default 2)')
    parser.add_argument('--workers', default='1,2,3,4',
                        help='worker counts to compare (default 1,2,3,4)')
    parser.add_argument('--patterns', default=','.join(PATTERNS),
                        help='read patterns to compare: streamed, inplace')
    parser.add_argument('--stream-max-mb', type=float,
                        default=index_pdf_text.STREAM_MAX_MB,
                        help='the streaming size cap (default %(default)s, '
                             'the shipped value)')
    parser.add_argument('--no-evict', dest='evict', action='store_false',
                        help='leave the page cache warm; a control, not a '
                             'measurement of a full rebuild')
    parser.add_argument('--no-project', dest='project', action='store_false',
                        help='skip walking the library for a full-rebuild '
                             'projection')
    parser.add_argument('--json', action='store_true',
                        help='print one JSON object instead of the table')
    args = parser.parse_args(argv)

    global _OUT
    if args.json:
        _OUT = sys.stderr

    library = os.path.abspath(args.library)
    if not os.path.isdir(library):
        sys.exit(f'not a directory: {library}')

    paths = pick_sample(library, args.sample, args.seed)
    if not paths:
        sys.exit(f'no PDFs found under {library}')
    workers = [int(w) for w in args.workers.split(',') if w.strip()]
    patterns = [p.strip() for p in args.patterns.split(',') if p.strip()]
    unknown = [p for p in patterns if p not in PATTERNS]
    if unknown:
        sys.exit(f'unknown pattern(s): {", ".join(unknown)}')

    sample_mib = sum(os.path.getsize(p) for p in paths) / 2 ** 20
    results, expected = measure(paths, library, workers, patterns,
                                args.stream_max_mb, args.rounds, args.evict)
    rows = report(results, expected, sample_mib)

    payload = {'library': library, 'sample_files': len(paths),
               'sample_mib': round(sample_mib, 1), 'seed': args.seed,
               'pages': expected[0], 'chars': expected[1],
               'evicted': args.evict, 'configurations': rows}

    if args.project:
        total = library_mib(library)
        best = min(rows, key=lambda r: r['median_s'])
        if sample_mib > 0:
            seconds = total / best['mib_s'] if best['mib_s'] else 0
            minutes = seconds / 60
            payload['projection'] = {
                'configuration': best['configuration'],
                'library_mib': round(total, 3),
                'extraction_seconds': round(seconds, 3),
                'extraction_minutes': round(minutes, 1)}
            if not args.json:
                say()
                say(f'library: {total:,.0f} MiB of PDFs in total. At the '
                      f'fastest rate here ({best["mib_s"]:.1f} MiB/s, '
                      f'{best["configuration"]}) extraction alone is '
                      f'~{minutes:.0f} min, plus under a minute of writes.')
                say('This is extraction only. Confirm it with one real '
                      'rebuild before quoting it.')

    if args.json:
        print(json.dumps(payload))
    return 0


if __name__ == '__main__':
    sys.exit(main())
