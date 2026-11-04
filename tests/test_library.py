import os, sys, tempfile, subprocess, sqlite3, unittest, json
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
        self.library=self.root/'library'; self.folder=self.library/'Books'/'01-DnD-and-d20'/'Test Realm';self.folder.mkdir(parents=True)
        self.db=self.root/'test.db'; self.env={**os.environ,'RPG_STATE_DIR':str(self.root),'RPG_LIBRARY':str(self.library),'RPG_DB':str(self.db)}
        import pymupdf
        doc=pymupdf.open();p=doc.new_page();p.insert_text((70,70),'ancient dragon wisdom');doc.save(self.folder/'Corebook.pdf');doc.close()
        (self.folder/'dragon-notes.txt').write_text('notes')
        self.scan()
    def tearDown(self):self.temp.cleanup()
    def command(self,*args):
        r=subprocess.run([sys.executable,*map(str,args)],env=self.env,capture_output=True,text=True)
        self.assertEqual(r.returncode,0,r.stderr+r.stdout);return r
    def scan(self):return self.command(ROOT/'catalog.py','--library',self.library,'--db',self.db)
    def query(self,q):
        with sqlite3.connect(self.db) as con:return con.execute(q).fetchall()
    def test_scan_preserves_ids_and_removed_ids_are_not_reused(self):
        before=dict(self.query('select rel_path,id from files'));self.scan();self.assertEqual(before,dict(self.query('select rel_path,id from files')))
        (self.folder/'A-first.txt').write_text('new');self.scan();after=dict(self.query('select rel_path,id from files'))
        for key,value in before.items():self.assertEqual(after[key],value)
        highest=max(after.values());(self.folder/'A-first.txt').unlink();self.scan();(self.folder/'B-next.txt').write_text('new');self.scan()
        self.assertGreater(dict(self.query('select rel_path,id from files'))[str((self.folder/'B-next.txt').relative_to(self.library))],highest)
    def test_index_rebuild_replaces_existing_pages(self):
        args=(ROOT/'index_pdf_text.py','--library',self.library,'--db',self.db,'--workers','1')
        self.command(*args);self.command(*args,'--rebuild')
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(1,)])
        import pymupdf
        doc=pymupdf.open();p=doc.new_page();p.insert_text((70,70),'replacement gryphon text');doc.save(self.folder/'Corebook.pdf');doc.close()
        self.command(*args,'--rebuild');self.assertEqual(self.query("select count(*) from pdf_fts where pdf_fts match 'ancient'"),[(0,)])
        self.assertEqual(self.query("select count(*) from pdf_fts where pdf_fts match 'gryphon'"),[(1,)])
    def client(self):
        import app
        from fastapi.testclient import TestClient
        app.DB=str(self.db);app.LIBRARY=str(self.library)
        return TestClient(app.app)
    def test_search_combines_query_and_filter(self):
        c=self.client();r=c.get('/search?q=dragon&cat=asset&section=files');self.assertEqual(r.status_code,200);self.assertNotIn('dragon-notes.txt</a>',r.text)
        r=c.get('/search?q=dragon&cat=book&section=files');self.assertEqual(r.status_code,200);self.assertIn('dragon-notes.txt</a>',r.text)
        self.assertEqual(c.get('/search?q=AND&section=content').status_code,200)
        self.assertEqual(c.get('/search?page=-5').status_code,200)
    def test_suffix_ranges_and_unicode_filename(self):
        c=self.client();fid=self.query("select id from files where filename='dragon-notes.txt'")[0][0]
        r=c.get('/raw/'+str(fid),headers={'Range':'bytes=-2'});self.assertEqual(r.status_code,206);self.assertEqual(r.content,b'es')
        r=c.get('/raw/'+str(fid),headers={'Range':'bytes=999-'});self.assertEqual(r.status_code,416);self.assertEqual(r.headers['content-range'],'bytes */5')
    def test_mutations_reject_cross_origin_and_plain_forms(self):
        c=self.client();self.assertEqual(c.post('/api/rescan').status_code,403)
        self.assertEqual(c.post('/api/rescan',headers={'x-library-request':'1','origin':'https://evil.test'}).status_code,403)
    def test_all_templates_render(self):
        c=self.client();cid=self.query('select id from collections')[0][0];fid=self.query('select id from files')[0][0]
        for path in ['/', '/shelf', '/browse','/search','/desk','/dtrpg',f'/collection/{cid}',f'/view/{fid}']:
            with self.subTest(path=path):
                response=c.get(path);self.assertEqual(response.status_code,200)
                title=response.text.split('<title>')[1].split('</title>')[0]
                self.assertNotIn('<',title)
    def test_metadata_survives_rescan(self):
        import jobs
        c=self.client();cid=self.query('SELECT id FROM collections')[0][0]
        with patch.object(jobs,'BASE',self.root):
            r=c.post('/api/metadata/'+str(cid),headers={'X-Library-Request':'1'},json={'name':'My Realm','family':'Custom','publisher':'My Press','genre':'fantasy','cover_file':'','cover_page':1})
            self.assertEqual(r.status_code,200,r.text)
        self.scan()
        self.assertEqual(self.query('SELECT name,family FROM collections'),[('My Realm','Custom')])
    def test_file_categories_respect_word_boundaries(self):
        from catalog import classify_category
        self.assertEqual(classify_category('document','RuneQuest.pdf'),('book','expansion'))
        self.assertEqual(classify_category('document','Dragonbane Bestiary.pdf'),('book','expansion'))
        self.assertEqual(classify_category('document','Core Rules.pdf'),('book','core-rules'))

    def test_taxonomy_series_editions_and_manual_shelves(self):
        import taxonomy
        empty=(None,None,None,[])
        for title in ['Old School Essentials','OLD-SCHOOL ESSENTIALS - Basic Rules','Creature Feature Quarterly vol. 1 (OSE)']:
            shelf,meta,rule=taxonomy.classify_title(title,'book',taxonomy.D20,empty)
            self.assertEqual(shelf,taxonomy.OSR);self.assertEqual(meta[0],'B/X')
        self.assertEqual(taxonomy.classify_title('Dungeon Crawl Classics #28 - Into the Wilds','book',taxonomy.OSR,empty)[0],taxonomy.D20)
        self.assertEqual(taxonomy.classify_title('Dungeon Crawl Classics RPG','book',taxonomy.D20,empty)[0],taxonomy.OSR)
        self.assertEqual(taxonomy.classify_title('Vaesen - Solo Rules','book',taxonomy.UNIVERSAL,empty)[0],taxonomy.HORROR)
        self.assertEqual(taxonomy.classify_title('Pirate-Borg','book',taxonomy.UNIVERSAL,empty)[1][0],'Mörk Borg')
        self.assertEqual(taxonomy.classify_title('Old-School Essentials Map Pack','asset','Assets',empty)[0],'Assets')
        import jobs
        c=self.client();cid=self.query('SELECT id FROM collections')[0][0]
        with patch.object(jobs,'BASE',self.root):
            r=c.post('/api/metadata/'+str(cid),headers={'X-Library-Request':'1'},json={'name':'Test Realm','family':'Custom','shelf':taxonomy.OSR,'tags':'osr, personal','cover_file':''})
            self.assertEqual(r.status_code,200,r.text)
        self.scan()
        self.assertEqual(self.query('SELECT shelf FROM collections'),[(taxonomy.OSR,)])
        self.assertIn('Test Realm',c.get('/search?tag=personal').text)
        self.assertEqual(c.get('/classification').status_code,200)

    def test_render_process_produces_preview(self):
        from concurrent.futures import ProcessPoolExecutor
        import multiprocessing
        from render_media import render
        with ProcessPoolExecutor(max_workers=1,mp_context=multiprocessing.get_context('spawn')) as pool:
            result=pool.submit(render,str(self.folder/'Corebook.pdf'),str(self.root/'cover'),[160],0).result(timeout=30)
        self.assertTrue(Path(result[160][0]).is_file())
    def test_maintenance_lock_excludes_other_process(self):
        import jobs
        with patch.object(jobs,'BASE',self.root):
            with jobs.lease():
                result=subprocess.run([sys.executable,'-c','import jobs; jobs.run("test",lambda fd:0)'],cwd=ROOT,env=self.env,capture_output=True)
                self.assertNotEqual(result.returncode,0)
    def test_backup_is_consistent(self):
        from backup import backup
        target=backup(self.db,self.root/'backup.db')
        with sqlite3.connect(target) as c:self.assertEqual(c.execute('pragma integrity_check').fetchone()[0],'ok')
    def test_sync_stops_and_reports_failed_stage(self):
        import app,jobs
        from types import SimpleNamespace
        with patch.object(jobs,'BASE',self.root),patch.object(app,'SYNC_LOG',str(self.root/'sync.log')),patch.object(app.subprocess,'run',side_effect=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=7)]) as runner:
            app._run_sync(False,True,True,True)
            self.assertEqual(runner.call_count,2);self.assertEqual(app._sync_state['returncode'],7)
            self.assertEqual(jobs.read('sync')['stage'],'dedupe')
if __name__=='__main__':unittest.main()
