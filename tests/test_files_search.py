"""The files section of /search reads files_fts instead of scanning with LIKE.

The behaviour that must not change is which files a query returns, so most of
these tests compare the route against the substring query it replaced.
"""
import os, re, sqlite3, subprocess, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

# What /search?section=files used before files_fts: a leading-wildcard LIKE over
# the filename plus the three classification columns.
OLD_QUERY=("SELECT f.id FROM files f JOIN collections c ON c.id=f.collection_id "
           "WHERE 1=1{cat} AND (f.filename LIKE ? OR EXISTS(SELECT 1 FROM "
           "book_classification b WHERE b.rel_path=f.rel_path AND (b.system LIKE ? "
           "OR b.series LIKE ? OR b.campaign LIKE ?)))")

class FilesSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.library=self.root/'library'
        realm=self.library/'Books'/'01-DnD-and-d20'/'Test Realm';realm.mkdir(parents=True)
        (realm/'Corebook.pdf').write_bytes(b'%PDF-1.4 not really a pdf')
        (realm/'dragon-notes.txt').write_text('notes')
        second=self.library/'Books'/'01-DnD-and-d20'/'Second Realm';second.mkdir(parents=True)
        (second/'Abomination Vaults - Ruins of Gauntlight.pdf').write_bytes(b'%PDF-1.4 x')
        art=self.library/'Assets'/'Forbidden North';art.mkdir(parents=True)
        (art/'Ancient Dragon Lair Map.png').write_bytes(b'\x89PNG\r\n\x1a\n')
        self.db=self.root/'test.db'
        self.env={**os.environ,'RPG_STATE_DIR':str(self.root),'RPG_LIBRARY':str(self.library),'RPG_DB':str(self.db)}
        self.scan()
    def tearDown(self):self.temp.cleanup()
    def scan(self):
        r=subprocess.run([sys.executable,str(ROOT/'catalog.py'),'--library',str(self.library),'--db',str(self.db)],env=self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr+r.stdout)
    def query(self,sql,args=()):
        with sqlite3.connect(self.db) as con:return con.execute(sql,args).fetchall()
    def client(self):
        import app
        from fastapi.testclient import TestClient
        app.DB=str(self.db);app.LIBRARY=str(self.library)
        return TestClient(app.app)
    def old_result(self,q,cat=''):
        with sqlite3.connect(self.db) as con:
            return {r[0] for r in con.execute(OLD_QUERY.format(cat=' AND f.category=?' if cat else ''),
                                              ([cat] if cat else [])+['%'+q+'%']*4)}
    def searched_ids(self,q,cat=''):
        url='/search?section=files&q='+q.replace(' ','+')+('&cat='+cat if cat else '')
        response=self.client().get(url)
        self.assertEqual(response.status_code,200,response.text)
        matches=re.findall(r'/raw/(\d+)',response.text)
        return {int(m) for m in matches}, response.text
    def result_total(self,html):
        return int(re.search(r'Page \d+ of \d+ · ([\d,]+) results',html).group(1).replace(',',''))

    def test_scan_indexes_every_file_once(self):
        files={r[0] for r in self.query('SELECT id FROM files')}
        indexed=[int(i) for i,_type in self.query('SELECT id, typeof(id) FROM files_fts')]
        self.assertEqual(len(indexed),len(files))
        self.assertEqual(set(indexed),files)
        # the section compares files.id with the stored id, so they must be
        # comparable whatever affinity the FTS5 build gives an UNINDEXED column
        matched={r[0] for r in self.query(
            'SELECT f.id FROM files f WHERE f.id IN (SELECT CAST(id AS INTEGER) '
            "FROM files_fts WHERE files_fts MATCH '\"dragon\"*')")}
        self.assertTrue(matched)
        self.assertLessEqual(matched,files)

    def test_index_carries_filename_and_classification(self):
        rows=dict(self.query('SELECT f.filename, b.system FROM files f LEFT JOIN book_classification b USING(rel_path)'))
        indexed={r[0]:r[1] for r in self.query('SELECT filename, system FROM files_fts')}
        self.assertEqual(set(indexed),set(rows))
        for name,system in rows.items():
            self.assertEqual(indexed[name],system or '')

    def test_results_match_the_previous_substring_search(self):
        for q in ['dragon','corebook','notes','realm','vaults','gauntlight','ruins','unassigned','ancient','png']:
            with self.subTest(q=q):
                ids,html=self.searched_ids(q)
                self.assertEqual(ids,self.old_result(q))
                self.assertEqual(self.result_total(html),len(ids))
    def test_category_filter_still_applies(self):
        for q in ['dragon','realm','map']:
            with self.subTest(q=q):
                ids,_html=self.searched_ids(q,'asset')
                self.assertEqual(ids,self.old_result(q,'asset'))

    def test_short_queries_keep_the_substring_scan(self):
        # An FTS5 prefix of one or two characters matches most of the library,
        # so short queries keep the old substring scan.
        for q in ['ra','ag','no']:
            with self.subTest(q=q):
                ids,_html=self.searched_ids(q)
                self.assertEqual(ids,self.old_result(q))
                self.assertTrue(ids)

    def test_long_queries_use_the_index_not_a_substring_scan(self):
        # Mid-token fragments match a LIKE scan and never a token prefix, so an
        # empty result proves the files section is not doing one any more.
        for q in ['agon','oreboo','auntlig']:
            with self.subTest(q=q):
                self.assertTrue(self.old_result(q))
                ids,_html=self.searched_ids(q)
                self.assertEqual(ids,set())

    def test_manual_classification_refreshes_the_index(self):
        import jobs
        fid=self.query("SELECT id FROM files WHERE filename='Corebook.pdf'")[0][0]
        with patch.object(jobs,'BASE',self.root):
            response=self.client().post('/api/book-metadata/'+str(fid),headers={'X-Library-Request':'1'},
                                        json={'system':'Midgard Sagas','edition':'','series':'The White Wastes','campaign':'','role':'core-rules'})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.query('SELECT system,series FROM files_fts WHERE id=?',(fid,)),[('Midgard Sagas','The White Wastes')])
        for q in ['midgard','white']:
            with self.subTest(q=q):
                ids,_html=self.searched_ids(q)
                self.assertEqual(ids,self.old_result(q))
                self.assertIn(fid,ids)

    def test_removed_files_leave_the_index(self):
        (self.library/'Books'/'01-DnD-and-d20'/'Second Realm'/'Abomination Vaults - Ruins of Gauntlight.pdf').unlink()
        self.scan()
        self.assertEqual(self.query("SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH '\"gauntlight\"'"),[(0,)])
        ids,_html=self.searched_ids('gauntlight')
        self.assertEqual(ids,self.old_result('gauntlight'))

    def test_missing_index_falls_back_to_the_substring_scan(self):
        # A fresh deploy of the app can meet a catalogue that catalog.py has not
        # rebuilt yet; the files section must keep working.
        with sqlite3.connect(self.db) as con:
            con.execute('DROP TABLE files_fts')
        for q in ['dragon','ago','realm']:
            with self.subTest(q=q):
                ids,_html=self.searched_ids(q)
                self.assertEqual(ids,self.old_result(q))

if __name__=='__main__':unittest.main()
