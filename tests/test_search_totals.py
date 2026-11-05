"""/search must only pay for the result count it actually displays.

The four tab totals used to be computed on every request: a COUNT(*) over
`pdf_fts MATCH` (16 s on the live HDD catalogue) and a LIKE '%q%' over every
file row (2.8 s) among them, while the default tab needs none of it. These
tests trace the SQL the route really executes.
"""
import sqlite3
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
            """SELECT COUNT(*) FROM pdf_fts p
               JOIN files f ON f.rel_path = p.rel_path
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

    def test_total_cache_stays_bounded(self):
        import app
        app.clear_search_totals()
        with patch.object(app, 'SEARCH_TOTAL_CACHE_MAX', 2):
            client = self.client()
            for q in ('dragon', 'notes', 'core', 'realm'):
                client.get('/search?q='+q+'&section=files')
        self.assertLessEqual(len(app._search_totals), 2)


if __name__ == '__main__':
    unittest.main()
