"""Performance regression checks for the `/search` request path.

The 16 s search was a full scan plus an eager count, and nothing in the suite
noticed when they appeared.  These checks drive the real `/search` route over a
synthetic catalogue, record every statement the route executes with its
parameters, and ask SQLite for the plan it picks.  A reintroduced full scan or
an eagerly paid count fails here instead of on the host's spinning disk.

The rules
---------

1. ``no_content_count`` - no eagerly paid ``COUNT(...)`` over the PDF full-text
   index unless the request renders the content section.  The live content
   total is the 16 s query.  ``pdf_fts`` is external content over
   ``pdf_pages``, so counting either is the same scan, and ``COUNT(1)`` or
   ``COUNT(rowid)`` costs exactly what ``COUNT(*)`` does - the argument is
   therefore not restricted to ``*``.
2. ``no_like_scan`` - no leading-wildcard ``LIKE`` or ``GLOB`` (neither can use
   an index) against a column of the ``files`` table, whether the pattern is a
   literal, a positional binding or a named one, and even when the column is
   wrapped in a function such as ``lower()``.  The live files total is a 2.8 s
   query.  A qualified ``f.<column>`` is matched on its qualifier alone - the
   qualifier names the table, so a column list could only veto a column the
   schema grew after this file was written.  An unqualified column must instead
   be in a list read from ``catalog.SCHEMA`` rather than copied by hand.  That
   list is what the files section leans on, because it plans ``SEARCH f USING
   INTEGER PRIMARY KEY`` and rule 3 never sees ``SCAN f`` there: rule 2 is the
   only guard, so a column the schema grows must not slip past it.
3. ``no_files_scan`` - no plan step that scans ``files`` end to end, in every
   section.  ``section=files`` is included: since #4 its search reads
   ``files_fts``, so the plan is ``SCAN files_fts`` and walking ``files``
   there is the very regression this file exists for.  It was exempt until
   that exemption was found to hide a scan - see the note on
   ``FILTERED_URLS``.
4. Latency - each cold section answers well inside a generous budget on the
   synthetic set.  The totals cache is emptied before each sample, so the
   figure covers the query the section genuinely needs rather than a cache
   hit.  This is a backstop against collapse, not a benchmark: the structural
   rules above are exact and deterministic.

Every rule is enforced: there is no skip list, so a reintroduced full scan or an
eagerly paid count always fails the suite rather than being parked on an issue.
All four pass today because #3 made the section totals lazy and cached and #4
moved the filename and classification searches onto ``files_fts`` - for queries
of ``FTS_MIN_CHARS`` characters or more, which is what the requests below use.
``RPG_PERF_LATENCY_MS`` overrides the latency budget for a slower machine.

What these rules do not cover
-----------------------------

``files_filter`` keeps the old substring scan below ``FTS_MIN_CHARS`` (three
characters), because an FTS5 prefix of one or two characters matches most of
the library and the results would be useless.  That fallback is deliberate - #10
introduced it and ``test_files_search`` pins it - and it *is* a leading-wildcard
``LIKE`` over ``files``, the same count #10 measured at 2,779 ms on the host
catalogue.  Two more inputs reach it at or above the threshold: a query whose
words ``fts_query`` drops as operators (``q=and``), and one that holds only
punctuation ``fts_query`` cannot turn into a term (``q=!!!``) - ``-`` and ``'``
are word characters to it, so ``q=---`` keeps a term and takes the index.  Both
leave an empty ``MATCH``, which cannot run, so the substring scan answers
instead: that too is a leading-wildcard ``LIKE`` over ``files``.  A catalogue
whose ``files_fts`` has not been built yet falls back for every query, because
``use_fts`` also requires ``has_files_index``.  So rules 2 and 3 describe the
**indexed** path, which every request in ``URLS`` takes; they do not claim
``/search`` never scans ``files`` for every possible input.
``test_gate_measures_the_indexed_search_path`` pins the length end of that
boundary and ``test_operator_queries_keep_the_substring_scan`` (in
``test_files_search``) pins the operator end by result, so moving either fails
loudly rather than quietly leaving every rule on a path it does not describe.
Removing the fallback would change search results, so it is a product decision
rather than something this gate should decide by assertion.

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

URLS = {
    'default': '/search',
    'collections': f'/search?q={QUERY}&section=collections',
    'files': f'/search?q={QUERY}&section=files',
    'content': f'/search?q={QUERY}&section=content',
    'campaigns': f'/search?q={QUERY}&section=campaigns',
}
# The default request has its own rule, so it is the only section left out.
# Everything else - ``section=files`` included - is answered from an index.
# ``section=files`` used to be exempt on the grounds that it "pages through
# matching files, so it walks the table by design", which stopped being true
# when #4 moved the filename search onto ``files_fts``: for a query of
# ``FTS_MIN_CHARS`` characters or more the live plan there is ``SCAN
# files_fts``, never ``SCAN f``.  (Shorter queries keep the substring scan by
# design - see "What these rules do not cover" above.)  While it was exempt,
# replacing the ``files_fts`` restriction with ``INSTR(f.filename, ?) > 0``, or
# with ``COALESCE(f.filename,'') LIKE ?`` - which the like rule cannot see
# because the adjacency is broken - reintroduced a full scan of ``files`` in
# the one section that queries it, and the suite stayed green.
FILTERED_URLS = {name: url for name, url in URLS.items() if name != 'default'}

FILES_CONSTRAINTS = {'primary', 'unique', 'check', 'foreign', 'constraint'}


def files_columns():
    """The ``files`` columns, read from the schema the catalogue is built from.

    Read rather than listed: rule 2 only recognises a wildcard on a column it
    knows, and the files section plans ``SEARCH f USING INTEGER PRIMARY KEY`` -
    never ``SCAN f`` - so the plan rule cannot back rule 2 up there.  A column
    added to ``catalog.SCHEMA`` therefore has to widen this set by itself, or
    the rule would quietly stop covering it.
    ``test_files_columns_track_the_schema`` pins the parse against
    ``PRAGMA table_info(files)``, so a definition this cannot read fails loudly
    instead of narrowing the rule in silence.
    """
    body = re.search(
        r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?files\s*\((.*?)\)\s*;',
        catalog.SCHEMA, re.I | re.S)
    if not body:                    # pinned by the test below; never silently empty
        return set()
    columns = set()
    for definition in body.group(1).split(','):
        words = definition.split()
        if words and words[0].lower() not in FILES_CONSTRAINTS:
            columns.add(words[0].strip('"\'').lower())
    return columns


FILES_COLUMNS = files_columns()
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
    r'(?:([A-Za-z_]\w*)\s*\.\s*)?([A-Za-z_]\w*)\s*\)*\s*'
    r'(?:COLLATE\s+[A-Za-z_]\w*\s+)?(?:NOT\s+)?(LIKE|GLOB)\s+'
    r'(\?|:[A-Za-z_]\w*|\'[^\']*\')',
    re.I)
# the wildcard each operator treats as "match anything", so a leading one
# defeats an index exactly as LIKE '%x' does
LEADING_WILDCARD = {'like': '%', 'glob': '*'}
# `pdf_fts` is external content over `pdf_pages`, so counting either walks the
# same rows; `COUNT(1)` and `COUNT(rowid)` cost what `COUNT(*)` costs
CONTENT_TABLES = re.compile(r'\bpdf_(?:fts|pages)\b', re.I)
COUNT_CALL = re.compile(r'\bCOUNT\s*\(', re.I)
PLAN_SCAN = re.compile(r'\s*SCAN\s+([A-Za-z_]\w*)', re.I)
# A statement that *reads* files_fts, as opposed to one that merely mentions
# the name: `has_files_index()` probes `sqlite_master` with
# `name = 'files_fts'`, which every files request records, so a bare
# `'files_fts' in sql` test is satisfied by the probe alone and stays green
# when the section stops using the index.  JOIN is allowed because reading the
# index through a join is as good as through `id IN (SELECT ...)`; a
# schema-qualified `main.files_fts` would not match, which is the one rewrite
# this would have to be taught about, and nothing in the tree uses one.
FILES_FTS_READ = re.compile(r'\b(?:FROM|JOIN)\s+files_fts\b', re.I)
# Only a SELECT or a WITH can be a read; `catalog.rebuild_files_fts()` issues
# `DELETE FROM files_fts`, which would otherwise satisfy FILES_FTS_READ.
READ_STATEMENT = re.compile(r'\s*(?:SELECT|WITH)\b', re.I)


# --------------------------------------------------------------- statements ---

class RecordingConnection(sqlite3.Connection):
    """A connection that keeps every statement and its parameters."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.statements = []

    def _record(self, sql, parameters):
        if isinstance(parameters, dict):
            # Named placeholders: keep the mapping, so the pattern a LIKE binds
            # stays readable instead of collapsing to a tuple of key names.
            self.statements.append((sql, dict(parameters)))
            return
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


def bound_pattern(sql, match, params):
    """The pattern a LIKE/GLOB match binds, or None when it cannot be read."""
    placeholder = match.group(4)
    if placeholder.startswith(':'):
        return params.get(placeholder[1:]) if isinstance(params, dict) else None
    if placeholder != '?':           # a literal such as '%dragon%'
        return placeholder.strip("'")
    if isinstance(params, dict):     # named bindings, positional placeholder
        return None
    position = sql.count('?', 0, match.start(4))
    return params[position] if position < len(params) else None


def like_scan_on_files(statement):
    """The statement if it matches a files column with a leading wildcard.

    The column is matched tightly against the operator - only an optional
    qualifier, closing parentheses and ``COLLATE`` may sit between them - so a
    ``LIKE`` on another table in the same statement cannot be blamed on a files
    column that merely appears earlier in the SQL.
    """
    sql, params = statement
    if not FILES_REFERENCE.search(sql):
        return None
    aliases = files_aliases(sql)
    for match in LIKE_PATTERN.finditer(sql):
        qualifier, column, operator = (match.group(1), match.group(2).lower(),
                                       match.group(3).lower())
        if qualifier:
            # ``f.some_column`` names the table it belongs to, so the column
            # list adds nothing here and can only drop a column the schema grew
            # after this file was written - a hole the plan rule cannot see
            # through, because the files section searches `files` by rowid.
            if qualifier.lower() not in aliases:
                continue
        elif column not in FILES_COLUMNS:
            continue
        value = bound_pattern(sql, match, params)
        if isinstance(value, str) and value.startswith(LEADING_WILDCARD[operator]):
            return ' '.join(sql.split())
    return None


def content_count(statement):
    """The statement if it eagerly counts rows of the PDF full-text index."""
    sql = statement[0]
    if not CONTENT_TABLES.search(sql) or not COUNT_CALL.search(sql):
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
        """Every scan step of the plan SQLite picks for these statements.

        A statement SQLite refuses to plan is reported as an offender rather
        than skipped: a rule that silently ignores what it could not read
        passes vacuously, which is how a full scan hid in the files section
        until the exemption there was removed.
        """
        found = []
        for sql, params in statements:
            if not READ_STATEMENT.match(sql):
                continue
            try:
                rows = self.explain.execute('EXPLAIN QUERY PLAN ' + sql, params).fetchall()
            except sqlite3.Error as exc:
                found.append(f'could not be planned ({exc}): {" ".join(sql.split())}')
                continue
            aliases = files_aliases(sql)
            for row in rows:
                step = files_scan(row[-1], aliases)
                if step:
                    found.append(f'{step}   <- {" ".join(sql.split())}')
        return found

    def assert_rule(self, name, offenders):
        """Fail, naming the offending SQL, when a rule is broken."""
        if offenders:
            detail = '\n'.join(offenders[:5])
            self.fail(f'{name}: {len(offenders)} offending statement(s):\n{detail}')

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

    def test_files_section_is_guarded_against_a_scan(self):
        """The files section is the one place a scan of `files` can reappear.

        It was exempt from the plan rule until the exemption was caught hiding
        a full scan, so pin both halves: that the rule covers it, and that the
        request is answered from `files_fts` rather than from the table.

        The second half matches a statement that *reads* ``files_fts``, not one
        that mentions it: `has_files_index()` probes ``sqlite_master`` with
        ``name = 'files_fts'`` on every files request, so a bare substring test
        is satisfied by that probe and stays green when the section stops using
        the index altogether.
        """
        self.assertIn(
            'files', FILTERED_URLS,
            'section=files was exempted from the plan rule again; it queries the '
            'files table, so an exemption hides a reintroduced scan there')
        self.assertTrue(
            any(FILES_FTS_READ.search(sql)
                for sql, _params in self.statements('files')
                if READ_STATEMENT.match(sql)),
            'the files section executed no statement that reads FROM or JOIN '
            'files_fts, so the plan rule has no index path to check')

    def test_gate_measures_the_indexed_search_path(self):
        """The rules describe the indexed path, so pin where that path begins.

        ``files_filter`` deliberately keeps the substring scan below
        ``FTS_MIN_CHARS`` (an FTS5 prefix of one or two characters matches most
        of the library), so every rule above only means what it says while the
        requests in ``URLS`` are at or above that length.  Two separate guards:

        * the gate's own query is at least ``FTS_MIN_CHARS`` long, so raising
          the threshold cannot silently move every rule onto the fallback while
          the suite stays green;
        * a query of exactly ``FTS_MIN_CHARS`` characters is still answered
          from the index, so the boundary itself cannot drift by one without
          this failing.

        The boundary sample is also checked for being a real success that read
        ``files_fts``: a 500, or a request answered from somewhere else, would
        record no statements and let the check above pass vacuously.

        Neither asserts anything about the fallback below the threshold: it is
        deliberate, pinned by ``test_files_search``, and removing it would
        change search results.
        """
        import app
        self.assertGreaterEqual(
            len(QUERY.strip()), app.FTS_MIN_CHARS,
            f'the gate searches for {QUERY!r}, shorter than FTS_MIN_CHARS '
            f'({app.FTS_MIN_CHARS}), so the rules would measure the substring '
            'fallback instead of the indexed search')
        url = f'/search?q={QUERY.strip()[:app.FTS_MIN_CHARS]}&section=files'
        response, _elapsed, statements = self.run_request(url)
        # An error response records no statements, so every assertion after this
        # point would pass for the wrong reason. The sample has to be a real 200
        # that reached the index, or the boundary is not being measured at all.
        self.assertEqual(response.status_code, 200, url)
        offenders = [offender
                     for offender in map(like_scan_on_files, statements)
                     if offender]
        offenders += self.plans(statements)
        self.assertEqual(
            offenders, [],
            f'a query of exactly FTS_MIN_CHARS characters ({url}) fell back to '
            'the substring scan:\n\n    ' + '\n\n    '.join(offenders))
        self.assertTrue(
            any(FILES_FTS_READ.search(sql)
                for sql, _params in statements
                if READ_STATEMENT.match(sql)),
            f'a query of exactly FTS_MIN_CHARS characters ({url}) executed no '
            'statement that reads FROM or JOIN files_fts, so this measured '
            'something other than the indexed path')

    def test_unplannable_statements_are_reported_not_skipped(self):
        """A statement SQLite will not plan must fail, not vanish from the rule."""
        offenders = self.plans([('SELECT ? FROM files', ('a', 'b'))])
        self.assertEqual(len(offenders), 1, offenders)
        self.assertIn('could not be planned', offenders[0])

    # -- 4. latency backstop ----------------------------------------------

    def test_sections_answer_within_the_latency_budget(self):
        """Time a *cold* section, so the sample pays whatever query it needs.

        The totals cache makes every repeat after the first a hit: test_fixture_*
        has already requested each URL, so an untimed measurement would report
        the cache rather than the query and a content count that had grown from
        70 ms to 16 s would still pass here (#20 fixed the same mistake in
        benchmarks/search_bench.py).  Emptying the cache before each sample is
        what makes this a backstop against the expensive case.
        """
        paid_content_count = False
        for name, url in URLS.items():
            times = []
            for _ in range(TIMING_REPEATS):
                self.app.clear_search_totals()
                response, elapsed, statements = self.run_request(url)
                self.assertEqual(response.status_code, 200, url)
                if name == 'content' and any(map(content_count, statements)):
                    paid_content_count = True
                times.append(elapsed)
            best = min(times)
            self.assertLess(
                best, LATENCY_BUDGET_MS,
                f'{url} took {best:.0f} ms (best of {TIMING_REPEATS}, '
                f'{", ".join(f"{t:.0f}" for t in times)}); budget is '
                f'{LATENCY_BUDGET_MS:.0f} ms - a full scan or an eager count is back')
        # Guard the guard: if the samples ever go back to hitting the cache,
        # this fails instead of the budget passing on work that never ran.
        self.assertTrue(
            paid_content_count,
            'the content sample never executed its COUNT(*) over pdf_fts: the '
            'totals cache was warm, so this timed the cache, not the query')

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

    def test_files_columns_track_the_schema(self):
        """The like rule's column list must be the `files` table, not a copy of it.

        Rule 2 only recognises a wildcard on a column it knows, and the files
        section plans ``SEARCH f USING INTEGER PRIMARY KEY`` - never ``SCAN f``
        - so the plan rule cannot catch what rule 2 misses there.  A column
        added to ``catalog.SCHEMA`` must therefore appear in the set by itself;
        catching it here means a schema change cannot quietly narrow the rule.
        """
        con = sqlite3.connect(self.db_path)
        try:
            actual = {row[1].lower() for row in con.execute('PRAGMA table_info(files)')}
        finally:
            con.close()
        self.assertTrue(actual, 'PRAGMA table_info(files) returned nothing')
        self.assertEqual(
            actual, FILES_COLUMNS,
            'the like rule no longer knows every files column, so a leading '
            'wildcard on the missing one would pass: '
            f'{sorted(actual ^ FILES_COLUMNS)}')


class DetectorTests(unittest.TestCase):
    """Pin the shapes the rules catch, and the ones they must leave alone.

    A rule is only worth anything if it recognises the obvious rewrites of the
    regression it names: ``COUNT(1)`` costs exactly what ``COUNT(*)`` costs, and
    ``GLOB '*x*'`` defeats an index exactly as ``LIKE '%x%'`` does.  These
    cases feed the detectors directly, so they stay honest without depending on
    what the route happens to issue today.
    """

    def test_content_count_recognises_every_spelling(self):
        for sql in (
            'SELECT COUNT(*) FROM pdf_fts WHERE pdf_fts MATCH ?',
            'SELECT COUNT(1) FROM pdf_fts WHERE pdf_fts MATCH ?',
            'SELECT COUNT(rowid) FROM pdf_fts',
            'SELECT COUNT(*) FROM pdf_fts JOIN pdf_pages pp ON pp.id=pdf_fts.rowid',
            'SELECT COUNT(*) FROM pdf_pages',
            'select count( * ) from PDF_FTS',
        ):
            with self.subTest(sql=sql):
                self.assertIsNotNone(content_count((sql, ())))

    def test_content_count_leaves_other_counts_alone(self):
        for sql in (
            'SELECT COUNT(*) FROM collections',
            'SELECT COUNT(*) FROM files f',
            'SELECT f.id FROM pdf_fts WHERE pdf_fts MATCH ?',
        ):
            with self.subTest(sql=sql):
                self.assertIsNone(content_count((sql, ())))

    def test_like_scan_recognises_every_binding_shape(self):
        cases = [
            ('SELECT * FROM files f WHERE f.filename LIKE ?', ('%dragon%',)),
            ("SELECT * FROM files f WHERE f.filename LIKE '%dragon%'", ()),
            ("SELECT * FROM files f WHERE f.filename LIKE '%' || ? || '%'", ('dragon',)),
            ("SELECT * FROM files f WHERE f.filename GLOB ?", ('*dragon*',)),
            ("SELECT * FROM files f WHERE f.filename LIKE :pat", {'pat': '%dragon%'}),
            ("SELECT * FROM files WHERE filename LIKE ?", ('%dragon%',)),
            ("SELECT * FROM files f WHERE lower(f.filename) LIKE ?", ('%dragon%',)),
            ("SELECT * FROM files f WHERE f.filename COLLATE NOCASE LIKE ?", ('%dragon%',)),
        ]
        for sql, params in cases:
            with self.subTest(sql=sql, params=params):
                self.assertIsNotNone(like_scan_on_files((sql, params)))

    def test_like_scan_covers_a_qualified_column_it_never_heard_of(self):
        """A qualified ``f.<column>`` names its table, so the list cannot veto it.

        The files section searches `files` by rowid, so SQLite plans ``SEARCH f
        USING INTEGER PRIMARY KEY`` and the plan rule never sees ``SCAN f``:
        rule 2 is the only guard there.  Gating it on a column list would make
        every column the schema grew after this file was written a silent hole.
        """
        sql = ('SELECT f.* FROM files f JOIN collections c ON c.id=f.collection_id'
               ' WHERE f.sha_of_the_day LIKE ?')
        self.assertIsNotNone(like_scan_on_files((sql, ('%dragon%',))))

    def test_like_scan_does_not_blame_another_table(self):
        # A `files` reference elsewhere in the statement must not turn a
        # collection-name search into a files scan: the column is matched
        # tightly against the operator.
        cases = [
            ('SELECT * FROM collections c WHERE EXISTS(SELECT 1 FROM files f'
             ' WHERE f.collection_id=c.id) AND c.name LIKE ?', ('%dragon%',)),
            ('SELECT * FROM files f JOIN book_classification b ON b.rel_path=f.rel_path'
             ' WHERE b.system LIKE ?', ('%dragon%',)),
            # a trailing wildcard can still use the index
            ('SELECT * FROM files f WHERE f.filename LIKE ?', ('dragon%',)),
            ('SELECT * FROM files f WHERE f.filename LIKE ?', ('dragon',)),
        ]
        for sql, params in cases:
            with self.subTest(sql=sql, params=params):
                self.assertIsNone(like_scan_on_files((sql, params)))

    def test_recorder_keeps_named_parameters(self):
        con = sqlite3.connect(':memory:', factory=RecordingConnection)
        try:
            con.execute('SELECT :pat', {'pat': '%x%'})
            self.assertEqual(con.statements[-1][1], {'pat': '%x%'})
        finally:
            con.close()


if __name__ == '__main__':
    unittest.main()
