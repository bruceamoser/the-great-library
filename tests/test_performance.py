"""Performance regression checks for the `/search` request path.

The 16 s search was a full scan plus an eager count, and nothing in the suite
noticed when they appeared.  These checks drive the real `/search` route over a
synthetic catalogue, record every statement the route executes with its
parameters, and ask SQLite for the plan it picks.  A reintroduced full scan or
an eagerly paid count fails here instead of on the host's spinning disk.

The rules
---------

1. ``no_content_count`` - no ``COUNT(*)`` over ``pdf_fts`` unless the request
   renders the content section.  The live content total is the 16 s query.
2. ``no_like_scan`` - no ``LIKE '%...'`` (leading wildcard, so no index) against
   a column of the ``files`` table.  The live files total is a 2.8 s query.
3. ``no_files_scan`` - no plan step that scans ``files`` end to end, for the
   sections that have an index path.  ``section=files`` is exempt: it pages
   through matching files, so it walks the table by design.
4. Latency - each section answers well inside a generous budget on the
   synthetic set.  This is a backstop against collapse, not a benchmark: the
   structural rules above are exact and deterministic.

Rules that are not enforced yet
-------------------------------

All four rules are hard gates: ``PENDING`` below is empty, because the work they
were waiting on has landed (#3 made the section totals lazy and cached, #4 moved
the filename and classification searches onto ``files_fts``).  The mechanism is
kept for the next rule that has to land in a later change - a rule listed in
``PENDING`` with the issue that removes the offending statement is *skipped*
with that issue named instead of failing, its test raises a ``UserWarning`` once
the rule starts passing, and deleting the entry turns the check into a hard gate.
``RPG_PERF_STRICT=1`` (set in CI) enforces any pending rule immediately even
before its issue lands; ``RPG_PERF_LATENCY_MS`` overrides the latency budget.

Keeping the fixture honest
--------------------------

The catalogue is built from the production schemas - ``catalog.SCHEMA``,
``classification``/``taxonomy``/``metadata`` and the ``pdf_pages`` +
``pdf_fts`` index ``index_pdf_text.ensure_schema`` creates - so a schema change
shows up here as a failure to build rather than as a test with nothing to
measure.
"""

import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import catalog
import classification
import index_pdf_text
import metadata
import taxonomy

COLLECTIONS = 40
FILES_PER_COLLECTION = 50
PDF_PAGES = 6000
QUERY = 'dragon'
PDF_TEXT = 'dragon wisdom hoard treasure gryphon'

LATENCY_BUDGET_MS = float(os.environ.get('RPG_PERF_LATENCY_MS') or 250)
TIMING_REPEATS = 3
STRICT = os.environ.get('RPG_PERF_STRICT') == '1'

# rule name -> the open issue(s) whose fix removes the offending statement.
# Empty: every rule passes today, so every rule is enforced.  Add an entry only
# while the rule fails and an issue is open to fix it.
PENDING = {}
RULES = {
    'default_does_not_count_content',
    'default_does_not_like_scan_files',
    'default_does_not_scan_files',
    'query_does_not_count_content',
    'query_does_not_like_scan_files',
    'query_sections_do_not_scan_files',
    'sections_answer_within_the_latency_budget',
}

URLS = {
    'default': '/search',
    'collections': f'/search?q={QUERY}&section=collections',
    'files': f'/search?q={QUERY}&section=files',
    'content': f'/search?q={QUERY}&section=content',
    'campaigns': f'/search?q={QUERY}&section=campaigns',
}
# section=files pages through matching files, so it walks the table by design;
# the default request has its own rule
FILTERED_URLS = {name: url for name, url in URLS.items()
                 if name not in ('default', 'files')}

FILES_COLUMNS = {
    'id', 'collection_id', 'rel_path', 'filename', 'ext', 'kind', 'category',
    'subcategory', 'size', 'mtime',
}
# words that can follow "FROM files" without being a table alias
NON_ALIAS = {
    'where', 'join', 'on', 'using', 'group', 'order', 'limit', 'indexed', 'not',
    'as', 'left', 'inner', 'outer', 'cross', 'natural', 'union', 'except',
    'intersect', 'window', 'and', 'or', 'set', 'values', 'select', 'by',
}

FILES_REFERENCE = re.compile(r'\b(?:FROM|JOIN)\s+files\b', re.I)
FILES_BINDING = re.compile(
    r'\b(?:FROM|JOIN)\s+files\b(?:\s+(?:AS\s+)?([A-Za-z_]\w*))?', re.I)
LIKE_PATTERN = re.compile(
    r'([A-Za-z_]\w*\s*\.\s*)?([A-Za-z_]\w*)\s+(?:NOT\s+)?LIKE\s+(\?|\'[^\']*\')',
    re.I)
PLAN_SCAN = re.compile(r'\s*SCAN\s+([A-Za-z_]\w*)', re.I)


# --------------------------------------------------------------- statements ---

class RecordingConnection(sqlite3.Connection):
    """A connection that keeps every statement and its parameters."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.statements = []

    def _record(self, sql, parameters):
        try:
            stored = tuple(parameters or ())
        except TypeError:  # a generator or a custom sequence
            stored = ()
        self.statements.append((sql, stored))

    def execute(self, sql, parameters=()):
        self._record(sql, parameters)
        return super().execute(sql, parameters)

    def executemany(self, sql, parameters):
        # The search path writes nothing; keep the first row only for the trace.
        if isinstance(parameters, (list, tuple)):
            self._record(sql, parameters[0] if parameters else ())
        else:
            self.statements.append((sql, ()))
        return super().executemany(sql, parameters)


def files_aliases(sql):
    """Names that refer to the `files` table in this statement."""
    aliases = {'files'}
    for match in FILES_BINDING.finditer(sql):
        name = (match.group(1) or '').lower()
        if name and name not in NON_ALIAS:
            aliases.add(name)
    return aliases


def like_scan_on_files(statement):
    """The statement if it applies a leading-wildcard LIKE to a files column."""
    sql, params = statement
    if not FILES_REFERENCE.search(sql):
        return None
    aliases = files_aliases(sql)
    for match in LIKE_PATTERN.finditer(sql):
        qualifier, column, pattern = match.group(1), match.group(2).lower(), match.group(3)
        if qualifier:
            if qualifier.replace(' ', '').rstrip('.').lower() not in aliases:
                continue
        elif column not in FILES_COLUMNS:
            continue
        if pattern == '?':
            position = sql.count('?', 0, match.start(3))
            value = params[position] if position < len(params) else None
            if not (isinstance(value, str) and value.startswith('%')):
                continue
        elif not pattern.startswith("'%"):
            continue
        return ' '.join(sql.split())
    return None


def content_count(statement):
    """The statement if it counts rows of the PDF full-text index."""
    sql = statement[0]
    if 'pdf_fts' not in sql or not re.search(r'COUNT\s*\(\s*\*\s*\)', sql, re.I):
        return None
    return ' '.join(sql.split())


def files_scan(detail, aliases):
    """The plan detail if it scans the files table end to end."""
    match = PLAN_SCAN.match(detail)
    if match and match.group(1).lower() in aliases:
        return detail.strip()
    return None


# ------------------------------------------------------------------ fixture ---

def build_catalogue(db_path):
    """A catalogue shaped like the real one, without any library data."""
    con = sqlite3.connect(db_path)
    try:
        con.executescript(catalog.SCHEMA)
        taxonomy.initialize(con)
        metadata.initialize(con)
        classification.initialize(con)
        index_pdf_text.ensure_schema(con)
        collections, files, classified = [], [], []
        file_id = 0
        for index in range(COLLECTIONS):
            cid = index + 1
            shelf = 'd20' if index % 2 else 'OSR'
            folder = f'01-DnD-and-d20/Game {cid:02d}' if shelf == 'd20' else f'02-OSR/Game {cid:02d}'
            rel_path = f'Books/{folder}'
            collections.append((
                cid, shelf, 'book', f'Game {cid:02d}', f'Game {cid:02d}',
                f'game {cid:02d}', 'd20' if shelf == 'd20' else 'OSR',
                rel_path, FILES_PER_COLLECTION, FILES_PER_COLLECTION * 1_000_000))
            for _ in range(FILES_PER_COLLECTION):
                file_id += 1
                stem = (f'Dragon Compendium {file_id}' if file_id % 10 == 0
                        else f'Corebook {file_id}')
                path = f'{rel_path}/Core Rules/{stem}.pdf'
                files.append((
                    file_id, cid, path, f'{stem}.pdf', '.pdf', 'document', 'book',
                    'core-rules', 1_000_000 + file_id, 1.6e9 + file_id))
                classified.append((
                    path,
                    'Pathfinder' if shelf == 'd20' else 'Old-School Essentials',
                    '1e' if shelf == 'd20' else '',
                    f'Series {cid % 7}',
                    f'Campaign {cid % 11}' if file_id % 5 == 0 else '',
                    file_id % 5 + 1, 'core-rules', 'high', '', 1))
        con.executemany(
            'INSERT INTO collections(id,shelf,category,folder,name,sort_name,'
            'family,rel_path,n_files,n_bytes) VALUES(?,?,?,?,?,?,?,?,?,?)', collections)
        con.executemany(
            'INSERT INTO files(id,collection_id,rel_path,filename,ext,kind,'
            'category,subcategory,size,mtime) VALUES(?,?,?,?,?,?,?,?,?,?)', files)
        con.executemany(
            'INSERT INTO book_classification(rel_path,system,edition,series,'
            'campaign,volume,role,confidence,evidence,version) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)', classified)
        con.executemany(
            'INSERT INTO search(collection_id,name,shelf,tags,filenames) '
            'VALUES(?,?,?,?,?)',
            [(row[0], row[4], row[1], '', f'Game {row[0]:02d}') for row in collections])
        # The content index: one page row per entry, spread over every file.
        con.executemany(
            'INSERT INTO pdf_pages(rel_path,page,text) VALUES(?,?,?)',
            [(files[index % len(files)][2], index % 200 + 1,
              f'{PDF_TEXT} page {index}') for index in range(PDF_PAGES)])
        builder = getattr(catalog, 'rebuild_files_fts', None)
        if builder is not None:  # added by #4; the files section would find nothing without it
            builder(con)
        con.commit()
    finally:
        con.close()


# -------------------------------------------------------------------- tests ---

class SearchPerformanceTests(unittest.TestCase):
    """The /search request path must not fall back to scanning."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.db_path = cls.root / 'perf.db'
        (cls.root / 'library').mkdir()
        build_catalogue(cls.db_path)

        import app
        from fastapi.testclient import TestClient
        cls.app = app
        cls.saved = (app.db, app.DB, app.LIBRARY)
        cls.connections = []
        app.DB = str(cls.db_path)
        app.LIBRARY = str(cls.root / 'library')
        app.db = cls.connect
        cls.client = TestClient(app.app)
        cls.explain = sqlite3.connect(cls.db_path)
        cls.recorded = {}

    @classmethod
    def tearDownClass(cls):
        cls.explain.close()
        cls.app.db, cls.app.DB, cls.app.LIBRARY = cls.saved
        cls.temp.cleanup()

    @classmethod
    def connect(cls):
        con = sqlite3.connect(cls.db_path, timeout=30, factory=RecordingConnection)
        con.row_factory = sqlite3.Row
        cls.connections.append(con)
        return con

    @classmethod
    def run_request(cls, url):
        """(response, milliseconds, [(sql, params), ...]) for one request."""
        cls.connections.clear()
        started = time.perf_counter()
        response = cls.client.get(url)
        elapsed = (time.perf_counter() - started) * 1000.0
        statements = [statement for con in cls.connections
                      for statement in con.statements]
        for con in cls.connections:
            try:
                con.close()
            except sqlite3.Error:
                pass
        return response, elapsed, statements

    @classmethod
    def recorded_request(cls, url):
        """The first request to `url`, replayed for the whole class."""
        if url not in cls.recorded:
            response, elapsed, statements = cls.run_request(url)
            cls.recorded[url] = (response, elapsed, statements)
        return cls.recorded[url]

    def statements(self, name):
        return self.recorded_request(URLS[name])[2]

    def plans(self, statements):
        """Every scan step of the plan SQLite picks for these statements."""
        found = []
        for sql, params in statements:
            if not re.match(r'\s*(SELECT|WITH)\b', sql, re.I):
                continue
            try:
                rows = self.explain.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
            except sqlite3.Error:
                continue
            aliases = files_aliases(sql)
            for row in rows:
                step = files_scan(row[-1], aliases)
                if step:
                    found.append(f'{step}   <- {" ".join(sql.split())}')
        return found

    def assert_rule(self, name, offenders):
        """Enforce a rule, or skip it while the issue that blocks it is open."""
        if not offenders:
            if name in PENDING:
                warnings.warn(
                    f'{name} now passes; delete it from PENDING in '
                    f'tests/test_performance.py so it is enforced', stacklevel=2)
            return
        detail = '\n'.join(offenders[:5])
        if STRICT or name not in PENDING:
            self.fail(f'{name}: {len(offenders)} offending statement(s):\n{detail}')
        blockers = ', '.join(f'#{issue}' for issue in PENDING[name])
        self.skipTest(f'{name}: not enforced yet, waiting on {blockers} - '
                      f'{len(offenders)} offending statement(s):\n{detail}')

    def collect(self, names, extract):
        """Every offender over `names`, each labelled with the request."""
        found = []
        for name in names:
            for offender in map(extract, self.statements(name)):
                if offender:
                    found.append(f'{offender}\n      in {URLS[name]}')
        return found

    # -- 1. no eagerly paid content total ---------------------------------

    def test_default_does_not_count_content(self):
        self.assert_rule('default_does_not_count_content',
                         self.collect(['default'], content_count))

    def test_query_does_not_count_content(self):
        # section=content displays its own total, so counting it is the point
        self.assert_rule('query_does_not_count_content',
                         self.collect(['collections', 'files', 'campaigns'], content_count))

    # -- 2. no leading-wildcard LIKE against files ------------------------

    def test_default_does_not_like_scan_files(self):
        self.assert_rule('default_does_not_like_scan_files',
                         self.collect(['default'], like_scan_on_files))

    def test_query_does_not_like_scan_files(self):
        self.assert_rule('query_does_not_like_scan_files',
                         self.collect(list(URLS), like_scan_on_files))

    # -- 3. no end-to-end scan of files -----------------------------------

    def test_default_does_not_scan_files(self):
        self.assert_rule('default_does_not_scan_files',
                         self.plans(self.statements('default')))

    def test_query_sections_do_not_scan_files(self):
        offenders = []
        for name in FILTERED_URLS:
            offenders += [f'{step}      in {URLS[name]}'
                          for step in self.plans(self.statements(name))]
        self.assert_rule('query_sections_do_not_scan_files', offenders)

    # -- 4. latency backstop ----------------------------------------------

    def test_sections_answer_within_the_latency_budget(self):
        for name, url in URLS.items():
            times = []
            for _ in range(TIMING_REPEATS):
                response, elapsed, _statements = self.run_request(url)
                self.assertEqual(response.status_code, 200, url)
                times.append(elapsed)
            best = min(times)
            self.assertLess(
                best, LATENCY_BUDGET_MS,
                f'{url} took {best:.0f} ms (best of {TIMING_REPEATS}, '
                f'{", ".join(f"{t:.0f}" for t in times)}); budget is '
                f'{LATENCY_BUDGET_MS:.0f} ms - a full scan or an eager count is back')

    # -- 5. the fixture measures something ---------------------------------

    def test_fixture_is_realistic_and_requests_succeed(self):
        con = sqlite3.connect(self.db_path)
        counts = {
            'collections': con.execute('SELECT COUNT(*) FROM collections').fetchone()[0],
            'files': con.execute('SELECT COUNT(*) FROM files').fetchone()[0],
            'pages': con.execute('SELECT COUNT(*) FROM pdf_fts').fetchone()[0],
            'hits': con.execute(
                "SELECT COUNT(*) FROM pdf_fts WHERE pdf_fts MATCH '\"dragon\"*'").fetchone()[0],
        }
        con.close()
        self.assertEqual(counts['collections'], COLLECTIONS)
        self.assertEqual(counts['files'], COLLECTIONS * FILES_PER_COLLECTION)
        self.assertEqual(counts['pages'], PDF_PAGES)
        self.assertGreater(counts['hits'], 0, 'the planted PDF text is not searchable')

        # Requests must be served from the synthetic catalogue, never from a
        # real library, and the recorder must actually see the route's SQL.
        self.assertEqual(self.app.DB, str(self.db_path))
        for name, url in URLS.items():
            response, _elapsed, statements = self.recorded_request(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertGreaterEqual(len(statements), 3, f'{url}: recorder saw no SQL')
        self.assertTrue(
            any('pdf_fts' in sql for sql, _params in self.statements('content')),
            'the content section executed no statement against pdf_fts')
        self.assertTrue(
            any(self.explain.execute('EXPLAIN QUERY PLAN ' + sql).fetchall()
                for sql, params in self.statements('default')
                if re.match(r'\s*SELECT\b', sql, re.I) and not params),
            'EXPLAIN QUERY PLAN returned nothing; the plan rules would pass vacuously')
        self.assertIn('Dragon Compendium', self.recorded_request(URLS['content'])[0].text)

    def test_pending_rules_name_implemented_rules(self):
        self.assertEqual(set(PENDING) - RULES, set(),
                         'PENDING names a rule no test enforces')


if __name__ == '__main__':
    unittest.main()
