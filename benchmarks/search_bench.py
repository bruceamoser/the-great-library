#!/usr/bin/env python3
"""
Offline benchmark for `/search`.

Search is the slowest surface in the app and, on the live catalogue, the slowest
query of all is the *total* rather than the page of results: a content count over
309,790 indexed pages took 16 s on the host's spinning disk where the same count
took 70 ms on NVMe. Judging an optimisation from there is guesswork, so this
script builds a catalogue of a chosen size and times the real endpoint.

It writes a synthetic catalogue using the *real* schemas -- `catalog.SCHEMA`,
the `pdf_pages`/`pdf_fts`/`pdf_status` index `index_pdf_text.ensure_schema`
creates, and the tables `classification`/`taxonomy`/`metadata` create -- into a
throwaway SQLite file, then drives `/search` through FastAPI's in-process test
client once per section. The search indexes are filled through the same
functions a rescan uses (`catalog.rebuild_files_fts`,
`index_pdf_text.ensure_schema`), so the benchmark exercises the query paths the
live catalogue takes rather than their empty-index fallbacks. Nothing is read
from the library, no server is started and no socket is opened.

Usage:
  .venv/bin/python benchmarks/search_bench.py
  .venv/bin/python benchmarks/search_bench.py --files 5000 --pages 50000
  .venv/bin/python benchmarks/search_bench.py --runs 10 --query ''
  .venv/bin/python benchmarks/search_bench.py --files 25227 --pages 309790 \
      --collections 763 --runs 3

Timings include template rendering, because that is what the reader waits for.
Only the rendered section computes a total (#3): the other three tabs carry no
count, so a request no longer pays for a query it does not display.

Each section is timed twice.  The `cold` column empties `/search`'s totals cache
before each sample -- three samples, reported as the fastest -- so it pays
whatever query the section genuinely needs: on the live host the content count.
The remaining columns are repeats inside the cache TTL, the cheap case a reader
sees when they page through results; a run that measured only those would report
the cache rather than the query.  `campaigns` is the exception, because it
rebuilds its catalogue on every request and is not behind the totals cache.
"Cold" means the totals cache only: the untimed request before it has already
read the pages, so the operating system's cache is warm and a spinning disk looks
better here than the first request after a restart does.  A streamed response is
not measured -- `/search` returns HTML, not a stream.

`--json` prints one JSON object instead of the table, for a regression check.
"""
import argparse
import json
import math
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SECTIONS = ('collections', 'files', 'content', 'campaigns')
# Cold samples per section. Each one pays the section's count -- 16 s for the
# live content total -- so this is the dial that trades run time for a stable
# number; three is enough for a minimum that does not move between runs.
COLD_RUNS = 3
# The live catalogue holds 25,227 files in 763 collections: ~33 per collection.
FILES_PER_COLLECTION = 33
# One indexed page in this many carries the query word, so that a content
# search has a hit count worth timing. 20 puts ~2,500 matches in 50,000 pages.
HITS_IN = 20

SHELVES = ('01-DnD-and-d20', '02-OSR-and-Old-School', '03-Indie-and-Story',
           '04-SciFi-and-Cyberpunk', '05-Horror-and-Modern', '06-Licensed-IP',
           '07-Universal-Systems', '08-Warhammer-and-Grimdark',
           '09-Fantasy-and-Adventure', '11-DriveThruRPG')

FAMILIES = ('Pathfinder Second Edition', 'Dungeons & Dragons', 'Shadowdark',
            'Blades in the Dark', 'Traveller', 'Vaesen', 'Mörk Borg',
            'Call of Cthulhu', 'Dungeon Crawl Classics', 'Forbidden Lands')

PUBLISHERS = ('Paizo', 'Wizards of the Coast', 'Free League', 'Goodman Games',
              'Pelgrane Press', 'Chaosium', 'Schwalb Entertainment', 'Indie')

TITLES = ('Abomination Vaults', 'Curse of the Crimson Throne', 'Rules Cyclopedia',
          'Bestiary', 'Player Core', 'Gamemaster Screen', 'Into the Wilds',
          'Tome of Beasts', 'Solo Rules', 'Campaign Setting', 'Starter Set',
          'Book of Random Tables')

CAMPAIGNS = ('Abomination Vaults', 'Curse of the Crimson Throne', 'Kingmaker',
             'Rise of the Runelords', 'Enemy Within', 'Masks of Nyarlathotep')

TAGS = ('osr', 'fantasy', 'sci-fi', 'horror', '5e', 'pbta', 'solo', 'maps',
        'official', 'third-party')

BOOK_SUBCATEGORIES = ('core-rules', 'expansion', 'campaign', 'sheets')
ASSET_KINDS = (('map', 'maps', '.pdf'), ('map', 'maps', '.jpg'),
               ('art', 'art', '.png'), ('token', 'art', '.png'),
               ('audio', 'audio', '.mp3'), ('vtt-data', 'project', '.json'))

# Prose-shaped filler. Repeats are fine: FTS indexes tokens, not meaning.
WORDS = (
    'the a of and to in is are was were for with on at by from this that these '
    'those it its as or not but if then than when while after before under over '
    'between across through during without within against among around because '
    'book page rule rules chapter table roll dice player players character '
    'characters game games dungeon dungeon master campaign adventure scene city '
    'forest mountain river castle tower keep temple shrine village road path '
    'night day dawn dusk winter summer spring autumn storm rain wind fire stone '
    'iron steel copper silver gold ancient ruined secret hidden lost forgotten '
    'sword shield spell spells magic magical ritual creature creatures monster '
    'monsters dragon dragons giant goblin knight wizard cleric thief ranger bard '
    'encounter encounter combat damage armour weapon armoury potion scroll tome '
    'lore history legend map maps region kingdom empire guild order cult temple'
).split()


# ------------------------------------------------------------------ building ---

def _word_text(rng, keyword, hit_rate):
    words = rng.choices(WORDS, k=110)
    if keyword and rng.randrange(hit_rate) == 0:
        words[0] = keyword
    return ' '.join(words)


def _split(total, parts):
    """Split `total` rows across `parts` groups, front-loading the remainder."""
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def build_catalogue(db_path, files, pages, collections, query='', seed=1234):
    """Create the real schema at `db_path` and fill it with plausible rows.

    Returns a dict of the counts actually written, so the caller can report the
    shape of the catalogue it timed.
    """
    import catalog
    import classification
    import index_pdf_text
    import metadata
    import taxonomy

    rng = random.Random(seed)
    con = sqlite3.connect(str(db_path), timeout=30)
    try:
        con.executescript(catalog.SCHEMA)          # collections, files, search
        index_pdf_text.ensure_schema(con)          # pdf_pages, pdf_fts, pdf_status
        # The tables /search reads when the query is not empty: the per-file
        # classification filter and the campaign cards.
        classification.initialize(con)
        taxonomy.initialize(con)
        metadata.initialize(con)

        per_collection = _split(files, collections)
        collection_rows, file_rows, search_rows = [], [], []
        classified, pdf_files, collection_tag_rows = [], [], []
        tag_ids = {}
        fid = 0
        for cid, count in enumerate(per_collection, start=1):
            shelf = SHELVES[(cid - 1) % len(SHELVES)]
            family = FAMILIES[rng.randrange(len(FAMILIES))]
            title = TITLES[rng.randrange(len(TITLES))]
            publisher = PUBLISHERS[rng.randrange(len(PUBLISHERS))]
            name = f'{title} {cid}'
            rel_path = f'{shelf}/{name}'
            names, n_bytes = [], 0
            for i in range(count):
                fid += 1
                # The first file is always an indexed PDF, so even a tiny
                # catalogue can exercise the content section.
                if fid == 1 or rng.random() < 0.75:
                    ext = '.pdf' if fid == 1 else rng.choice(
                        ('.pdf', '.pdf', '.pdf', '.epub'))
                    kind = 'rulebook' if i == 0 else 'document'
                    category = 'book'
                    subcategory = BOOK_SUBCATEGORIES[
                        rng.randrange(len(BOOK_SUBCATEGORIES))]
                else:
                    kind, subcategory, ext = ASSET_KINDS[
                        rng.randrange(len(ASSET_KINDS))]
                    category = 'asset'
                filename = f'{title} {fid:05d}{ext}'
                size = min(300_000_000,
                           max(50_000, int(rng.lognormvariate(13, 1.4))))
                n_bytes += size
                file_rel = f'{rel_path}/{filename}'
                file_rows.append((fid, cid, file_rel, filename, ext, kind,
                                  category, subcategory, size,
                                  1_760_000_000.0 + fid * 60))
                names.append(filename)
                if ext == '.pdf':
                    pdf_files.append(file_rel)
                    classified.append((
                        file_rel, family, rng.choice(('', '', '2e')), '',
                        CAMPAIGNS[rng.randrange(len(CAMPAIGNS))]
                        if rng.random() < 0.12 else '',
                        None,
                        'core-rules' if i == 0 else rng.choice(
                            ('adventure', 'supplement', 'map', 'handout')),
                        'high', 'synthetic', 1))
            collection_rows.append((
                cid, shelf, 'book', name, name, name.casefold(), family,
                publisher, rng.choice(('fantasy', 'sci-fi', 'horror', 'modern')),
                'takeout', rel_path, count, n_bytes))
            tags = [TAGS[rng.randrange(len(TAGS))]
                    for _ in range(rng.randrange(3))]
            search_rows.append((cid, name, shelf, ' '.join(sorted(set(tags))),
                                ' '.join(names)))
            for tag in sorted(set(tags)):
                tag_ids.setdefault(tag, len(tag_ids) + 1)
                collection_tag_rows.append((cid, tag_ids[tag]))

        con.executemany('INSERT INTO collections VALUES '
                        '(?,?,?,?,?,?,?,?,?,?,?,?,?)', collection_rows)
        con.executemany('INSERT INTO files VALUES (?,?,?,?,?,?,?,?,?,?)',
                        file_rows)
        con.executemany('INSERT INTO search(collection_id,name,shelf,tags,'
                        'filenames) VALUES (?,?,?,?,?)', search_rows)
        con.executemany('INSERT INTO tags(id,name) VALUES (?,?)',
                        [(i, t) for t, i in tag_ids.items()])
        con.executemany('INSERT INTO collection_tags VALUES (?,?)',
                        collection_tag_rows)
        con.executemany('INSERT INTO book_classification VALUES '
                        '(?,?,?,?,?,?,?,?,?,?)', classified)

        # One page in HITS_IN carries the query word, which gives the content
        # section a hit count worth timing without turning every page into a
        # match. Pages are spread evenly over the files, so page numbers stay
        # contiguous per file the way the real indexer writes them.
        keyword = ''
        terms = re.findall(r"[\w'-]+", query or '')
        if terms:
            keyword = re.sub(r'\W', '', terms[0]).lower()
        written = 0
        batch = []
        for i in range(pages):
            batch.append((pdf_files[i % len(pdf_files)],
                          i // len(pdf_files) + 1,
                          _word_text(rng, keyword, HITS_IN)))
            if len(batch) >= 2000:
                con.executemany('INSERT INTO pdf_pages(rel_path,page,text) '
                                'VALUES (?,?,?)', batch)
                written += len(batch)
                batch = []
        if batch:
            con.executemany('INSERT INTO pdf_pages(rel_path,page,text) '
                            'VALUES (?,?,?)', batch)
            written += len(batch)
        con.commit()
        # catalog.py fills files_fts at the end of a scan. Leaving it empty
        # would send a 3+ character query down the FTS path with no rows to
        # find, so the files section would time an empty result set.
        files_indexed = catalog.rebuild_files_fts(con)
        con.commit()
        con.execute('PRAGMA optimize')
    finally:
        con.close()
    return {'files': files, 'pages': written, 'collections': collections,
            'pdf_files': len(pdf_files), 'files_indexed': files_indexed}


# --------------------------------------------------------------- measuring ---

def percentile(values, fraction):
    """Linear-interpolated percentile, the definition numpy uses."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low, high = math.floor(position), math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def measure(db_path, query, sections, runs, warmup):
    """Time `/search` per section: cold cache-miss requests, then `runs` repeats.

    Returns ``{section: {'cold': ms, 'cold_samples': [ms, ...],
    'samples': [ms, ...]}}``.  `/search` caches a section's total for
    ``app.SEARCH_TOTAL_TTL`` seconds (#3), so the repeat timings alone would
    never include a count: the cache is emptied before each cold request, which
    then pays the total that primes the repeats.

    `cold` is the fastest of ``COLD_RUNS`` cold samples, not a single one.
    Timing noise only ever adds time, so the minimum is the usual estimator for
    what a request genuinely costs; a lone sample sat beside the percentiles and
    moved by several percent between runs of the same catalogue.

    `campaigns` is the exception: it rebuilds ``campaigns.catalog`` on every
    request and is not behind the totals cache, so there `cold` is only the
    fastest of three samples of the same work rather than a cache effect.
    """
    import app
    from fastapi.testclient import TestClient

    def search(client, params, section):
        response = client.get('/search', params=params)
        if response.status_code != 200:
            raise SystemExit(f'/search?section={section} returned '
                             f'{response.status_code}\n{response.text[:2000]}')
        return response

    def timed(client, params, section):
        started = time.perf_counter()
        search(client, params, section)
        return (time.perf_counter() - started) * 1000

    previous_db = app.DB
    app.DB = str(db_path)
    client = TestClient(app.app)
    results = {}
    try:
        for section in sections:
            params = {'section': section}
            if query:
                params['q'] = query
            # One untimed request: module import, template compilation and the
            # first connection are paid once, so `cold` measures the section's
            # query rather than the process starting.
            search(client, params, section)
            # Empty the cache before each cold sample, so every one of them
            # pays the total instead of reading the previous sample's work.
            cold_samples = []
            for _ in range(COLD_RUNS):
                app.clear_search_totals()
                cold_samples.append(timed(client, params, section))
            for _ in range(warmup):
                search(client, params, section)
            samples = [timed(client, params, section) for _ in range(runs)]
            results[section] = {'cold': min(cold_samples),
                                'cold_samples': cold_samples,
                                'samples': samples}
    finally:
        client.close()
        # Leave the module as we found it: `measure` is called in-process by the
        # tests, and a stale DB pointing into a deleted temp dir would outlive us.
        app.DB = previous_db
    return results


# -------------------------------------------------------------------- main ---

def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Offline /search benchmark against a synthetic catalogue.')
    parser.add_argument('--files', type=int, default=5000,
                        help='files in the synthetic catalogue (default 5000)')
    parser.add_argument('--pages', type=int, default=50_000,
                        help='indexed PDF pages (default 50000)')
    parser.add_argument('--collections', type=int, default=0,
                        help='collections; default is --files / '
                             f'{FILES_PER_COLLECTION}')
    parser.add_argument('--query', default='dragon',
                        help="search term; '' benchmarks filter-only mode")
    parser.add_argument('--sections', default=','.join(SECTIONS),
                        help='comma-separated subset of ' + ','.join(SECTIONS))
    parser.add_argument('--runs', type=int, default=5,
                        help='timed requests per section (default 5)')
    parser.add_argument('--warmup', type=int, default=1,
                        help='untimed requests per section after the cold '
                             'samples (default 1)')
    parser.add_argument('--db', default='',
                        help='write the synthetic catalogue here instead of a '
                             'temp dir; the path must not exist yet')
    parser.add_argument('--keep', action='store_true',
                        help='keep the temp catalogue instead of deleting it')
    parser.add_argument('--json', action='store_true',
                        help='print one JSON object instead of a table')
    parser.add_argument('--seed', type=int, default=1234,
                        help='RNG seed for the synthetic rows (default 1234)')
    args = parser.parse_args(argv)

    if args.files < 1:
        parser.error('--files must be at least 1')
    if args.pages < 0:
        parser.error('--pages cannot be negative')
    if args.runs < 1:
        parser.error('--runs must be at least 1')
    if args.warmup < 0:
        parser.error('--warmup cannot be negative')
    collections = args.collections or max(
        1, math.ceil(args.files / FILES_PER_COLLECTION))
    if collections < 1 or collections > args.files:
        parser.error('--collections must be between 1 and --files')
    sections = [s.strip() for s in args.sections.split(',') if s.strip()]
    unknown = [s for s in sections if s not in SECTIONS]
    if unknown or not sections:
        parser.error('--sections must be a subset of ' + ','.join(SECTIONS))

    tempdir = ''
    if args.db:
        db_path = Path(args.db).expanduser()
        if db_path.exists():
            parser.error(f'{db_path} already exists; refusing to overwrite it. '
                         'Point --db at a new path.')
    else:
        tempdir = tempfile.mkdtemp(prefix='rpg-search-bench-')
        db_path = Path(tempdir) / 'bench.db'

    try:
        started = time.perf_counter()
        shape = build_catalogue(db_path, args.files, args.pages, collections,
                                args.query, args.seed)
        build_seconds = time.perf_counter() - started
        results = measure(db_path, args.query, sections, args.runs, args.warmup)
    finally:
        if tempdir and not args.keep:
            shutil.rmtree(tempdir, ignore_errors=True)

    stats = {section: {'cold': result['cold'],
                       'cold_samples': result['cold_samples'],
                       'p50': percentile(result['samples'], 0.50),
                       'p95': percentile(result['samples'], 0.95),
                       'min': min(result['samples']),
                       'max': max(result['samples']),
                       'mean': (sum(result['samples'])
                                / len(result['samples'])),
                       'samples': result['samples']}
             for section, result in results.items()}

    if args.json:
        print(json.dumps({'catalogue': shape,
                          'db': str(db_path) if (args.db or args.keep) else '',
                          'query': args.query, 'runs': args.runs,
                          'build_seconds': build_seconds, 'sections': stats},
                         indent=2))
        return 0

    print(f"synthetic catalogue  {shape['files']:,} files  "
          f"{shape['pages']:,} pages  {shape['collections']:,} collections"
          f"  ({shape['files_indexed']:,} in files_fts, "
          f"built in {build_seconds:.1f} s)")
    print(f"query                {args.query!r}   "
          f"{args.runs} run(s), {args.warmup} warm-up")
    print(f"db                   {db_path}"
          f"{'' if (args.db or args.keep) else ' (deleted)'}")
    print()
    print(f"{'section':<12}{'cold ms':>10}{'p50 ms':>10}{'p95 ms':>10}"
          f"{'min ms':>10}{'max ms':>10}")
    for section in sections:
        s = stats[section]
        print(f"{section:<12}{s['cold']:>10.1f}{s['p50']:>10.1f}{s['p95']:>10.1f}"
              f"{s['min']:>10.1f}{s['max']:>10.1f}")
    print()
    print('Timings are full in-process /search requests. "cold" is the fastest '
          'of three\nrequests with the totals cache emptied, so it pays the '
          'query the section needs.\nOnly the rendered section computes a '
          'total; the other columns are repeats inside\nthe 60 s cache that '
          'holds it, except campaigns, which is rebuilt on every request.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
