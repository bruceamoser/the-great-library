"""Migration and search-parity tests for the PDF page index.

The interesting properties are (a) the old layout can be upgraded in place
without losing hits, (b) running the migration again does nothing, and (c) the
external-content index stays correct when pages are inserted, updated and
deleted. No real PDFs or library data are needed: the old FTS table is built
directly.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import index_pdf_text as index  # noqa: E402

OLD_SCHEMA = """
CREATE VIRTUAL TABLE pdf_fts USING fts5(
  rel_path UNINDEXED,
  page UNINDEXED,
  text,
  tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE pdf_status (
  rel_path TEXT PRIMARY KEY, mtime REAL, pages INTEGER, chars INTEGER,
  indexed_at REAL
);
CREATE TABLE pdf_page_map (rel_path TEXT NOT NULL, fts_rowid INTEGER PRIMARY KEY);
"""

OLD_PAGES = [
    ('Books/Fantasy/A/Core Rules.pdf', 1, 'The ancient dragon guards the gate.'),
    ('Books/Fantasy/A/Core Rules.pdf', 2, 'A gryphon nests above the gate.'),
    ('Books/Fantasy/A/Core Rules.pdf', 3, 'Ancient magic missile and sword rules.'),
    ('Books/Fantasy/A/Bestiary.pdf', 1, 'Dragons, wyverns and other winged beasts.'),
    ('Books/Fantasy/A/Bestiary.pdf', 2, 'The crypt holds a death knight.'),
    ('Books/Fantasy/A/Bestiary.pdf', 3, 'Wizard or sorcerer: either can cast it.'),
    ('Books/SciFi/B/Starship.pdf', 1, 'The laser sword hums in vacuum.'),
    ('Books/SciFi/B/Starship.pdf', 2, 'A fire in the engine room; death follows.'),
    ('Books/SciFi/B/Starship.pdf', 3, 'Ancient alien ruins on the moon.'),
    ('Books/SciFi/B/Starship.pdf', 4, 'The dragon-class cruiser decloaks.'),
    ('Books/Modern/C/Field Guide.pdf', 1, 'Cryptography is not a crypt.'),
    ('Books/Modern/C/Field Guide.pdf', 3, 'Saving throws versus magic and fire.'),
]

# Representative queries: single terms, a prefix, a phrase, boolean and AND.
QUERIES = [
    'dragon',
    'ancient',
    'gryphon',
    'sword',
    'crypt',
    'magic',
    'crypt*',
    '"magic missile"',
    'wizard OR sorcerer',
    'death AND fire',
]


def old_hits(con, query):
    return con.execute(
        'SELECT rel_path, CAST(page AS INTEGER) FROM pdf_fts '
        'WHERE pdf_fts MATCH ? ORDER BY rel_path, page', (query,)).fetchall()


def new_hits(con, query):
    return con.execute(
        'SELECT pp.rel_path, pp.page FROM pdf_fts '
        'JOIN pdf_pages pp ON pp.id = pdf_fts.rowid '
        'WHERE pdf_fts MATCH ? ORDER BY pp.rel_path, pp.page', (query,)).fetchall()


def make_old_db(path):
    con = sqlite3.connect(path)
    con.executescript(OLD_SCHEMA)
    for rel_path, page, text in OLD_PAGES:
        cur = con.execute(
            'INSERT INTO pdf_fts(rel_path, page, text) VALUES (?,?,?)',
            (rel_path, page, text))
        con.execute('INSERT INTO pdf_page_map VALUES (?,?)', (rel_path, cur.lastrowid))
    con.executemany(
        'INSERT INTO pdf_status VALUES (?,?,?,?,?)',
        [('Books/Fantasy/A/Core Rules.pdf', 100.0, 3, 120, 1.0),
         ('Books/Fantasy/A/Bestiary.pdf', 200.0, 3, 130, 2.0),
         ('Books/SciFi/B/Starship.pdf', 300.0, 4, 140, 3.0)]
    )
    con.commit()
    con.close()


class PdfIndexMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / 'library.db'
        self.env = {**os.environ, 'RPG_STATE_DIR': str(self.root)}

    def tearDown(self):
        self.temp.cleanup()

    def con(self):
        return sqlite3.connect(self.db)

    def test_migration_preserves_hits_and_status(self):
        make_old_db(self.db)
        con = self.con()
        before = {query: old_hits(con, query) for query in QUERIES}
        status_before = con.execute(
            'SELECT * FROM pdf_status ORDER BY rel_path').fetchall()
        self.assertTrue(all(before[q] for q in QUERIES), before)

        self.assertTrue(index.migrate(con))

        for query in QUERIES:
            with self.subTest(query=query):
                self.assertEqual(before[query], new_hits(con, query))
        self.assertEqual(
            status_before,
            con.execute('SELECT * FROM pdf_status ORDER BY rel_path').fetchall())
        self.assertEqual(len(OLD_PAGES),
                         con.execute('SELECT COUNT(*) FROM pdf_pages').fetchone()[0])
        # The obsolete companion table and the old columns are gone.
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('pdf_page_map', names)
        self.assertEqual(['text'], index._columns(con, 'pdf_fts'))
        # The external-content index agrees with its content table.
        con.execute("INSERT INTO pdf_fts(pdf_fts) VALUES('integrity-check')")
        con.close()

    def test_migration_is_idempotent(self):
        make_old_db(self.db)
        con = self.con()
        self.assertTrue(index.migrate(con))
        snapshot = (con.execute('SELECT * FROM pdf_pages ORDER BY id').fetchall(),
                    con.execute('SELECT * FROM pdf_status ORDER BY rel_path').fetchall(),
                    con.execute('SELECT COUNT(*) FROM pdf_fts').fetchone())
        # Second and third runs must not recopy, rebuild or duplicate anything.
        self.assertFalse(index.migrate(con))
        self.assertFalse(index.migrate(con))
        self.assertEqual(snapshot, (
            con.execute('SELECT * FROM pdf_pages ORDER BY id').fetchall(),
            con.execute('SELECT * FROM pdf_status ORDER BY rel_path').fetchall(),
            con.execute('SELECT COUNT(*) FROM pdf_fts').fetchone()))
        self.assertEqual(len(OLD_PAGES),
                         con.execute('SELECT COUNT(*) FROM pdf_pages').fetchone()[0])
        con.close()

    def test_fresh_database_gets_the_new_schema(self):
        con = self.con()
        self.assertTrue(index.migrate(con))
        self.assertFalse(index.migrate(con))
        self.assertEqual(['text'], index._columns(con, 'pdf_fts'))
        con.execute("INSERT INTO pdf_pages(rel_path, page, text) VALUES ('a.pdf',1,'hello world')")
        con.commit()
        self.assertEqual(1, con.execute(
            "SELECT COUNT(*) FROM pdf_fts WHERE pdf_fts MATCH 'hello'").fetchone()[0])
        con.close()

    def test_external_index_tracks_insert_update_and_delete(self):
        con = self.con()
        index.migrate(con)
        con.execute("INSERT INTO pdf_pages(rel_path, page, text) VALUES ('a.pdf',1,'original text')")
        con.execute("INSERT INTO pdf_pages(rel_path, page, text) VALUES ('a.pdf',2,'second page')")
        con.commit()
        self.assertEqual([(1,)], con.execute(
            "SELECT page FROM pdf_pages WHERE id IN "
            "(SELECT rowid FROM pdf_fts WHERE pdf_fts MATCH 'original')").fetchall())
        con.execute("UPDATE pdf_pages SET text='rewritten words' WHERE page=1")
        con.commit()
        con.execute("DELETE FROM pdf_pages WHERE page=2")
        con.commit()
        self.assertEqual(0, con.execute(
            "SELECT COUNT(*) FROM pdf_fts WHERE pdf_fts MATCH 'original'").fetchone()[0])
        self.assertEqual(0, con.execute(
            "SELECT COUNT(*) FROM pdf_fts WHERE pdf_fts MATCH 'second'").fetchone()[0])
        self.assertEqual(1, con.execute(
            "SELECT COUNT(*) FROM pdf_fts WHERE pdf_fts MATCH 'rewritten'").fetchone()[0])
        con.execute("INSERT INTO pdf_fts(pdf_fts) VALUES('integrity-check')")
        con.close()

    def test_migrate_cli_is_idempotent_and_reports_size(self):
        make_old_db(self.db)
        args = [sys.executable, str(ROOT / 'index_pdf_text.py'),
                '--library', str(self.root), '--db', str(self.db),
                '--migrate', '--vacuum']
        first = subprocess.run(args, env=self.env, capture_output=True, text=True)
        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertIn('database', first.stdout)
        second = subprocess.run(args, env=self.env, capture_output=True, text=True)
        self.assertEqual(second.returncode, 0, second.stderr + second.stdout)
        con = self.con()
        self.assertEqual(len(OLD_PAGES),
                         con.execute('SELECT COUNT(*) FROM pdf_pages').fetchone()[0])
        self.assertEqual(['text'], index._columns(con, 'pdf_fts'))
        con.close()


if __name__ == '__main__':
    unittest.main()
