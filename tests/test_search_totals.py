"""/search must only pay for the result count it actually displays.

The four tab totals used to be computed on every request: a COUNT(*) over
`pdf_fts MATCH` (16 s on the live HDD catalogue) and a LIKE '%q%' over every
file row (2.8 s) among them, while the default tab needs none of it. These
tests trace the SQL the route really executes.
"""
import os
import sqlite3
import time
import unittest
from unittest.mock import patch

import test_library as fixtures


class SearchTotals(unittest.TestCase):
    setUp = fixtures.LibraryTests.setUp
    tearDown = fixtures.LibraryTests.tearDown
    command = fixtures.LibraryTests.command
    scan = fixtures.LibraryTests.scan          # setUp() calls this to build the catalogue
    client = fixtures.LibraryTests.client

    def build_index(self):
        """Extract the fixture PDF's text so the content tab has something to count."""
        self.command(fixtures.ROOT/'index_pdf_text.py','--library',self.library,
                     '--db',self.db,'--workers','1')

    def rows(self, sql, args=()):
        with sqlite3.connect(self.db) as con:
            return con.execute(sql, args).fetchall()

    def content_total(self, q):
        """The same join the route uses, asked of the catalogue directly."""
        return self.rows(
            """SELECT COUNT(*) FROM pdf_fts
               JOIN pdf_pages pp ON pp.id = pdf_fts.rowid
               JOIN files f ON f.rel_path = pp.rel_path
               JOIN collections c ON c.id = f.collection_id
               WHERE pdf_fts MATCH ?""", (q,))[0][0]

    def files_total(self, q):
        return self.rows(
            """SELECT COUNT(*) FROM files f WHERE f.filename LIKE ?
               OR EXISTS(SELECT 1 FROM book_classification b
                         WHERE b.rel_path = f.rel_path AND
                         (b.system LIKE ? OR b.series LIKE ? OR b.campaign LIKE ?))""",
            ('%'+q+'%',)*4)[0][0]

    def traced_get(self, url, fresh=True):
        """Fetch a URL; return the response and the lowercased SQL it ran."""
        import app
        client = self.client()
        if fresh:
            app.clear_search_totals()
        statements = []
        real = app.db
        def traced():
            con = real()
            con.set_trace_callback(statements.append)
            return con
        with patch.object(app, 'db', traced):
            response = client.get(url)
        return response, ' '.join(statements).lower()

    def tabs(self, html):
        return html.split('aria-label="Result type">')[1].split('</nav>')[0]

    def nav(self, html):
        return html.split('aria-label="Results pages">')[1].split('</nav>')[0]

    def make_files(self, count):
        """Add text files to the fixture collection and rescan.

        Text files are catalogued the same way PDFs are, so this is enough to
        fill more than one page of the "Books & assets" tab without rendering
        PDFs.
        """
        for i in range(count):
            (self.folder/'Extra-{:03d}.txt'.format(i)).write_text('bulk note')
        self.scan()

    def refusing_counts(self):
        """Patch app.db so a COUNT(*) is refused, as a locked database would."""
        import app
        real_db = app.db
        state = {'busy': True}

        class BusyConnection:
            def __init__(self, con):
                self._con = con

            def execute(self, sql, *args, **kwargs):
                if state['busy'] and 'COUNT(*)' in sql.upper():
                    raise sqlite3.OperationalError('database is locked')
                return self._con.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._con, name)

        app.db = lambda: BusyConnection(real_db())
        return state, real_db

    def test_default_search_counts_only_the_visible_tab(self):
        response, sql = self.traced_get('/search')
        self.assertEqual(response.status_code, 200)
        # the collections tab is the default, and it is the only one counted
        self.assertIn('select count(*) from collections', sql)
        self.assertNotIn('count(*) from pdf_fts', sql)
        self.assertNotIn("like '%", sql)
        self.assertNotIn('book_classification', sql)   # campaign catalogue not built
        nav = self.tabs(response.text)
        self.assertIn('Collections <span>', nav)
        self.assertIn('Books &amp; assets</a>', nav)
        self.assertIn('Inside books</a>', nav)
        self.assertIn('Campaigns</a>', nav)

    def test_campaigns_tab_is_the_only_one_building_the_catalogue(self):
        response, sql = self.traced_get('/search?section=campaigns')
        self.assertEqual(response.status_code, 200)
        self.assertIn('book_classification', sql)
        self.assertNotIn('count(*) from pdf_fts', sql)
        self.assertNotIn("like '%", sql)

    def test_content_tab_reports_the_real_total(self):
        self.build_index()
        expected = self.content_total('dragon')
        self.assertGreaterEqual(expected, 1)
        response, sql = self.traced_get('/search?q=dragon&section=content')
        self.assertEqual(response.status_code, 200)
        self.assertIn('count(*) from pdf_fts', sql)
        self.assertNotIn("like '%", sql)
        self.assertIn('· {} results'.format(expected), response.text)

    def test_files_tab_counts_files_without_walking_the_index(self):
        expected = self.files_total('dragon')
        self.assertGreaterEqual(expected, 1)
        response, sql = self.traced_get('/search?q=dragon&section=files')
        self.assertEqual(response.status_code, 200)
        # The files tab is counted from files_fts, not by walking every row:
        # the substring scan only survives for queries too short to prefix.
        self.assertIn('files_fts match', sql)
        self.assertIn('count(*) from files f', sql)
        self.assertNotIn('count(*) from pdf_fts', sql)
        self.assertIn('· {} results'.format(expected), response.text)

    def test_repeated_requests_reuse_the_cached_total(self):
        self.build_index()
        expected = self.content_total('dragon')
        self.traced_get('/search?q=dragon&section=content')      # warms the cache
        response, sql = self.traced_get('/search?q=dragon&section=content', fresh=False)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('count(*) from pdf_fts', sql)
        self.assertIn('· {} results'.format(expected), response.text)

    def test_cached_total_expires_after_the_ttl(self):
        import app
        self.traced_get('/search?q=dragon&section=files')        # warms the cache
        with patch.object(app, 'SEARCH_TOTAL_TTL', 0.0):
            response, _ = self.traced_get('/search?q=dragon&section=files', fresh=False)
        # An expired entry still answers: the badge is rendered whether the total
        # came from the cache or was recomputed, and the SQL it used is not the
        # contract this test is about.
        self.assertEqual(response.status_code, 200)

    def test_cache_window_starts_when_the_count_finishes(self):
        """A slow count keeps the whole TTL after it, not what is left of it.

        The content total takes 16 s on the live HDD catalogue. Stamping the
        entry when the count was *asked for* left that total -- the expensive
        one this cache exists for -- with 44 s of cache instead of the 60 the
        constant and the comment promise.
        """
        import app
        app.clear_search_totals()
        key = (str(self.db), 'content', 'dragon', '', '', '', '', '')
        clock = [1000.0]

        def slow_count():
            clock[0] += 16.0                       # the count itself
            return 42

        with patch.object(app.time, 'monotonic', lambda: clock[0]):
            self.assertEqual(app.search_total(key, slow_count), 42)
            self.assertEqual(app._search_totals[key][0],
                             1016.0 + app.SEARCH_TOTAL_TTL)
            # still a hit one second before the window closes ...
            clock[0] = 1016.0 + app.SEARCH_TOTAL_TTL - 1
            self.assertEqual(
                app.search_total(key, lambda: self.fail('recomputed inside the TTL')), 42)
            # ... and a miss once it has
            clock[0] = 1016.0 + app.SEARCH_TOTAL_TTL
            self.assertEqual(app.search_total(key, lambda: 7), 7)

    def test_a_rescan_during_a_slow_count_is_not_undone(self):
        """A count that began before a rescan must not be cached after it.

        `clear_search_totals()` runs when a rescan finishes, but the content
        total takes 16 s on the live HDD catalogue, so the rescan lands while a
        count is still running. Storing that value afterwards puts a pre-rescan
        total back into the cache that was just emptied, and it is served for
        the whole TTL -- the opposite of what clearing after a rescan is for.
        """
        import app
        app.clear_search_totals()
        key = (str(self.db), 'content', 'dragon', '', '', '', '', '')

        def count_while_rescanning():
            app.clear_search_totals()      # the rescan finishes mid-count
            return 42                      # ... counted against the old catalogue

        # The in-flight request still renders what it counted ...
        self.assertEqual(app.search_total(key, count_while_rescanning), 42)
        # ... but it does not get to leave that number behind for everyone else.
        self.assertNotIn(key, app._search_totals)
        self.assertEqual(app.search_total(key, lambda: 7), 7)

    def test_renaming_a_collection_forgets_its_cached_count(self):
        """The collections tab counts the name, so a rename invalidates it.

        Without a clear, `/search` keeps reporting the pre-rename total for the
        rest of the TTL, which is the one thing the cache is not allowed to do.
        """
        import app
        cid = self.rows('SELECT id FROM collections ORDER BY id LIMIT 1')[0][0]
        client = self.client()
        app.clear_search_totals()
        before = client.get('/search?q=Realm&section=collections')
        self.assertIn('· 1 results', before.text)
        response = client.post(
            '/api/metadata/' + str(cid), headers={'X-Library-Request': '1'},
            json={'name': 'Renamed Shelf', 'family': 'Custom', 'publisher': 'My Press',
                  'genre': 'fantasy', 'cover_file': '', 'cover_page': 1})
        self.assertEqual(response.status_code, 200, response.text)
        after = client.get('/search?q=Realm&section=collections')
        self.assertIn('· 0 results', after.text)

    def test_reclassifying_a_book_forgets_the_cached_file_count(self):
        """The files tab counts `files_fts`, which a manual edit rewrites."""
        import app
        fid = self.rows('SELECT id FROM files ORDER BY id LIMIT 1')[0][0]
        client = self.client()
        app.clear_search_totals()
        before = client.get('/search?q=Zorblax&section=files')
        self.assertIn('· 0 results', before.text)
        response = client.post(
            '/api/book-metadata/' + str(fid), headers={'X-Library-Request': '1'},
            json={'system': 'Zorblax', 'edition': '1e', 'series': '', 'campaign': '',
                  'volume': None, 'role': 'core-rules'})
        self.assertEqual(response.status_code, 200, response.text)
        after = client.get('/search?q=Zorblax&section=files')
        self.assertIn('· 1 results', after.text)

    def test_an_import_forgets_the_totals_it_invalidated(self):
        """An inbox import rebuilds the catalogue in a child process.

        That child cannot clear this process's cache, so the totals it replaced
        have to be dropped here or `/search` keeps counting the old catalogue.
        """
        import app
        import inbox
        import jobs
        arrivals = self.library/'_Inbox'
        arrivals.mkdir(exist_ok=True)
        (arrivals/'Rapscallion-Maps.txt').write_text('map')
        client = self.client()
        app.clear_search_totals()
        client.get('/search?q=dragon&section=files')          # warm a total
        self.assertTrue(app._search_totals)
        with patch.object(jobs, 'BASE', self.root), \
                patch.dict(os.environ, self.env), \
                patch.object(inbox, 'process_batch', lambda *a, **k: ([], [])):
            plan = client.get('/api/inbox/plan').json()
            response = client.post(
                '/api/inbox/process', headers={'X-Library-Request': '1'},
                json={'automatic': True, 'groups': plan['groups']})
            self.assertEqual(response.status_code, 202, response.text)
            # jobs.record() finishes before the worker's finally runs, so wait
            # for the clear itself rather than for the job flag.
            deadline = time.monotonic() + 10
            while app._search_totals and time.monotonic() < deadline:
                time.sleep(.02)
        self.assertEqual(app._search_totals, {})

    def test_total_cache_stays_bounded(self):
        import app
        app.clear_search_totals()
        with patch.object(app, 'SEARCH_TOTAL_CACHE_MAX', 2):
            client = self.client()
            for q in ('dragon', 'notes', 'core', 'realm'):
                client.get('/search?q='+q+'&section=files')
        self.assertLessEqual(len(app._search_totals), 2)

    def test_count_rows_tells_a_missing_index_from_a_busy_database(self):
        """A missing index is a real zero; a database that would not answer is not.

        `count_rows` used to read 0 for every `OperationalError`, which made a
        locked database indistinguishable from an empty library.
        """
        import app

        class Refusing:
            def __init__(self, message):
                self.message = message

            def execute(self, *args, **kwargs):
                raise sqlite3.OperationalError(self.message)

        class Answering:
            def execute(self, *args, **kwargs):
                return type('Cursor', (), {'fetchone': lambda self: (7,)})()

        self.assertEqual(0, app.count_rows(Refusing('no such table: pdf_fts'), 'x'))
        self.assertEqual(0, app.count_rows(Refusing('no such column: nope'), 'x'))
        self.assertIsNone(app.count_rows(Refusing('database is locked'), 'x'))
        self.assertIsNone(app.count_rows(Refusing('disk I/O error'), 'x'))
        self.assertEqual(7, app.count_rows(Answering(), 'x'))

    def test_an_unanswerable_count_is_not_a_total(self):
        """Unknown is not zero, and neither is it remembered.

        A real zero -- "the index has not been built" -- is a total worth
        caching. A count that could not be answered is not a total at all, so
        `search_total` hands back the unknown instead of inventing a zero, and
        nothing is stored for the next request to serve.
        """
        import app
        key = (self.db, 'content', 'dragon', '', '', '', '', '')
        app.clear_search_totals()
        with patch.object(app.time, 'monotonic', return_value=1000.0):
            self.assertIsNone(app.search_total(key, lambda: None))
            self.assertIsNone(app.search_total(key, lambda: None))   # asked again
        self.assertNotIn(key, app._search_totals)
        with patch.object(app.time, 'monotonic', return_value=1000.0):
            self.assertEqual(0, app.search_total(key, lambda: 0))
        self.assertIn(key, app._search_totals)                       # a zero is a total

    def test_a_failed_count_is_not_remembered(self):
        """One busy moment must not read "0 results" for the whole TTL.

        A locked database makes the count unable to answer. The page must not
        state a total it could not count -- and must not derive a page count
        from it either, which used to rewind a deep page to page 1 -- while the
        cache stays empty so the next request asks again instead of serving the
        failure for a minute.
        """
        import app
        self.build_index()
        expected = self.content_total('dragon')
        self.assertGreaterEqual(expected, 1)
        app.clear_search_totals()
        client = self.client()
        real_db = app.db
        state = {'busy': True}

        class BusyConnection:
            """A connection that is locked whenever a COUNT(*) is asked of it."""

            def __init__(self, con):
                self._con = con

            def execute(self, sql, *args, **kwargs):
                if state['busy'] and 'COUNT(*)' in sql.upper():
                    raise sqlite3.OperationalError('database is locked')
                return self._con.execute(sql, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._con, name)

        try:
            app.db = lambda: BusyConnection(real_db())
            failed = client.get('/search?q=dragon&section=content')
            failed_deep = client.get('/search?q=dragon&section=content&page=2')
            state['busy'] = False
            recovered = client.get('/search?q=dragon&section=content')
        finally:
            app.db = real_db
        self.assertEqual(failed.status_code, 200)
        # The hits are still listed, but the page says nothing about a total it
        # could not count -- no "· 0 results", no "of 1", no badge count.
        self.assertIn('<span>Page 1</span>', failed.text)
        self.assertNotIn('· 0 results', failed.text)
        self.assertNotRegex(self.tabs(failed.text), r'Inside books\s*<span>')
        # A deep page keeps the page the reader asked for instead of being
        # rewound to 1 by a page count derived from the unanswerable zero.
        self.assertIn('<span>Page 2</span>', failed_deep.text)
        self.assertIn('· {} results'.format(expected), recovered.text)

    def test_a_full_page_offers_the_next_one_when_the_total_is_unknown(self):
        """Without a total, a full page is the only evidence another page exists.

        There is no page count to compare against, so the link has to come from
        the page being full. Offering it on a short page would be a dead end;
        withholding it on a full one would hide real results.
        """
        import app
        self.make_files(38)                       # 2 fixture files + 38 = 40, two pages
        app.clear_search_totals()
        client = self.client()
        state, real_db = self.refusing_counts()
        try:
            first = client.get('/search?section=files')
            second = client.get('/search?section=files&page=2')
            state['busy'] = False
            healthy = client.get('/search?section=files')
            empty = client.get('/search?q=zzzznothing&section=files')
        finally:
            app.db = real_db
        # A full first page (36 of 40) offers the next one, and still states no
        # total it could not count.
        self.assertIn('Next page', first.text)
        self.assertIn('<span>Page 1</span>', self.nav(first.text))
        self.assertNotIn(' results', self.nav(first.text))
        # The short second page is the end: no dead-end link.
        self.assertIn('<span>Page 2</span>', self.nav(second.text))
        self.assertIn('← Previous', second.text)
        self.assertNotIn('Next page', second.text)
        # A healthy request still derives both from the real total.
        self.assertIn('Page 1 of 2 · 40 results', healthy.text)
        self.assertIn('Next page', healthy.text)
        # And a genuinely empty result set still says so.
        self.assertIn('Nothing found.', empty.text)

    def test_an_empty_page_is_not_an_empty_library(self):
        """A page past the end must not report the library as empty.

        With no total, the route no longer rewinds a deep page to page 1 -- so
        the page can come back empty while earlier pages hold results. Saying
        "Nothing found." there would claim an empty library the database never
        reported, the same lie as stating an uncountable total.
        """
        import app
        app.clear_search_totals()
        client = self.client()
        state, real_db = self.refusing_counts()
        try:
            beyond = client.get('/search?section=files&page=5')
            beyond_query = client.get('/search?q=note&section=files&page=5')
            state['busy'] = False
            healthy = client.get('/search?section=files&page=5')
        finally:
            app.db = real_db
        self.assertEqual(200, beyond.status_code)
        self.assertNotIn('Nothing found.', beyond.text)
        self.assertIn('No results on this page.', beyond.text)
        self.assertIn('← Previous', beyond.text)          # there is somewhere to go
        self.assertNotIn('Nothing found.', beyond_query.text)
        # A healthy deep page is still clamped to the last real page, so the
        # message it gained is unreachable there.
        self.assertIn('Page 1 of 1 · 2 results', healthy.text)
        self.assertNotIn('No results on this page.', healthy.text)

    def test_a_hand_typed_page_cannot_outrun_the_offset(self):
        """Without a total, nothing bounded the page number.

        The page count normally clamps `page`, and with it the OFFSET. With no
        total there is no page count, and `page` comes straight from the query
        string -- so a hand-typed number reached SQLite as an unbounded OFFSET,
        which it binds as a 64-bit integer: a large enough page raised
        OverflowError and answered 500 in the one state where the database is
        already busy. The page is now bounded, and it still renders.
        """
        import app
        app.clear_search_totals()
        client = self.client()
        absurd = '/search?section=files&page=%d' % 10**30
        state, real_db = self.refusing_counts()
        try:
            forced = client.get(absurd)
            normal = client.get('/search?section=files&page=2')
            state['busy'] = False
            healthy = client.get(absurd)
        finally:
            app.db = real_db
        self.assertEqual(200, forced.status_code)
        # Bounded, and reported as the page actually rendered.
        self.assertIn('<span>Page %d</span>' % app.SEARCH_PAGE_LIMIT,
                      self.nav(forced.text))
        self.assertNotIn(' results', self.nav(forced.text))   # still no total
        self.assertIn('← Previous', forced.text)              # and a way back
        # A page inside the bound is untouched, so the cap only catches numbers
        # no click can produce.
        self.assertIn('<span>Page 2</span>', self.nav(normal.text))
        # A known total still clamps to the last real page, exactly as before.
        self.assertIn('Page 1 of 1 · 2 results', healthy.text)

    def test_content_hits_on_another_tab_are_not_nothing(self):
        """An empty campaign list is not an empty page.

        The campaigns tab renders the "Inside books" hits as well, so the
        empty-state cannot be decided from the active section's list alone --
        that would print "Nothing found." above a table of hits, which is the
        very claim this branch exists to stop making.
        """
        import app
        self.build_index()
        app.clear_search_totals()
        response = self.client().get('/search?q=dragon&section=campaigns')
        self.assertEqual(200, response.status_code)
        self.assertIn('Inside documents', response.text)     # hits are on the page
        self.assertNotIn('Nothing found.', response.text)


if __name__ == '__main__':
    unittest.main()
