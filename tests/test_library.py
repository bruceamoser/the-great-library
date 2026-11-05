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
    def index_args(self,*extra):return (ROOT/'index_pdf_text.py','--library',self.library,'--db',self.db,'--workers','1',*extra)
    def test_default_worker_pool_indexes_everything(self):
        # Runs the shipped default (three streaming workers, no --workers) so
        # CI exercises a real pool, including 3.14's multiprocessing changes.
        self.make_pdfs(6)
        out=self.command(ROOT/'index_pdf_text.py','--library',self.library,'--db',self.db)
        self.assertIn('failed       : 0',out.stdout)
        self.assertEqual(self.query('select count(*) from pdf_status'),[(7,)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(7,)])
        self.assertEqual(self.query("select count(*) from pdf_fts where pdf_fts match 'indexable'"),[(6,)])
    def make_pdfs(self,count):
        import pymupdf
        for i in range(count):
            doc=pymupdf.open();p=doc.new_page();p.insert_text((70,70),f'bulk tome {i} indexable');doc.save(self.folder/f'Bulk-{i:03d}.pdf');doc.close()
        self.scan()
    def test_index_writer_buffers_by_volume_and_loses_only_the_open_batch(self):
        import index_pdf_text
        con=sqlite3.connect(self.db);index_pdf_text.ensure_schema(con)
        writer=index_pdf_text.IndexWriter(con,1,batch_mb=0.005)
        self.assertFalse(writer.add('a.pdf',1.0,2,3500,[(1,'alpha '*500),(2,'beta text')]))
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(0,)])
        self.assertTrue(writer.add('b.pdf',2.0,1,2500,[(1,'gamma '*400)]))
        writer.flush();self.assertEqual(writer.commits,1)
        writer.add('c.pdf',3.0,1,2500,[(1,'delta '*400)])
        con.close()
        self.assertEqual(self.query('select rel_path from pdf_status order by 1'),[('a.pdf',),('b.pdf',)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(3,)])
        self.assertEqual(self.query('select count(*) from pdf_page_map where fts_rowid not in (select rowid from pdf_fts)'),[(0,)])
        con=sqlite3.connect(self.db);writer=index_pdf_text.IndexWriter(con,con.execute('select coalesce(max(rowid),0)+1 from pdf_fts').fetchone()[0])
        writer.add('a.pdf',9.0,1,2500,[(1,'epsilon '*350)]);writer.flush();con.close()
        self.assertEqual(self.query("select count(*) from pdf_fts where pdf_fts match 'alpha'"),[(0,)])
        self.assertEqual(self.query("select count(*) from pdf_fts where pdf_fts match 'epsilon'"),[(1,)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(2,)])
    def test_interrupted_index_flushes_what_is_done_and_resumes(self):
        import index_pdf_text
        self.make_pdfs(3)
        class InterruptingPool:
            def __init__(self,*a,**k):pass
            def __enter__(self):return self
            def __exit__(self,*exc):return False
            def imap_unordered(self,func,jobs,chunksize=1):
                yield func(jobs[0]);raise KeyboardInterrupt
        argv=['index_pdf_text.py','--library',str(self.library),'--db',str(self.db),'--workers','1']
        with patch.object(index_pdf_text,'Pool',InterruptingPool),patch.object(sys,'argv',argv):
            self.assertEqual(index_pdf_text.main(),130)
        self.assertEqual(self.query('select count(*) from pdf_status'),[(1,)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(1,)])
        out=self.command(*self.index_args())
        self.assertIn('processed      : 3',out.stdout)
        self.assertEqual(self.query('select count(*) from pdf_status'),[(4,)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(4,)])
    def test_killed_index_is_consistent_and_resumable(self):
        import time
        self.make_pdfs(60)
        # Kill a *rebuild*, because that is the run that relaxes pragmas: the
        # catalogue must still be in WAL afterwards (MEMORY would be persistent
        # in the file and leave the web app writing to a crash-unsafe database).
        proc=subprocess.Popen([sys.executable,*map(str,self.index_args('--rebuild','--batch-mb','0.001'))],env=self.env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            deadline=time.time()+30
            while time.time()<deadline and proc.poll() is None:
                try:
                    if self.query('select count(*) from pdf_status')[0][0]:break
                except sqlite3.OperationalError:pass
                time.sleep(0.01)
        finally:
            if proc.poll() is None:proc.kill()
        proc.wait(timeout=30)
        self.assertNotEqual(proc.returncode,0,'the kill landed after the run finished')
        self.assertEqual(self.query('pragma journal_mode'),[('wal',)])
        self.assertEqual(self.query('pragma integrity_check'),[('ok',)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),self.query('select count(*) from pdf_page_map'))
        self.assertEqual(self.query('select count(*) from pdf_page_map where fts_rowid not in (select rowid from pdf_fts)'),[(0,)])
        self.assertEqual(self.query('select count(*) from pdf_fts where rowid not in (select fts_rowid from pdf_page_map)'),[(0,)])
        self.assertEqual(self.query('select count(*) from (select rel_path,page from pdf_fts group by rel_path,page having count(*)>1)'),[(0,)])
        for rel_path,mtime,pages,chars,_ in self.query('select rel_path,mtime,pages,chars,indexed_at from pdf_status'):
            self.assertEqual((pages,chars==0),(1,False),rel_path)
        self.command(*self.index_args())
        self.assertEqual(self.query('select count(*) from pdf_status'),[(61,)])
        self.assertEqual(self.query('select count(*) from pdf_fts'),[(61,)])
        self.assertIn('nothing to do',self.command(*self.index_args()).stdout)
    def test_rebuild_leaves_no_persistent_pragma_change(self):
        # journal_mode lives in the database file: a rebuild must never switch
        # the live catalogue to something crash-unsafe, even if killed.
        self.assertEqual(self.query('pragma journal_mode'),[('wal',)])
        out=self.command(*self.index_args('--rebuild'))
        self.assertIn('rebuild pragmas',out.stdout)
        self.assertIn('journal_mode=wal',out.stdout)
        self.assertEqual(self.query('pragma journal_mode'),[('wal',)])
        self.assertEqual(self.query('select count(*) from pdf_status'),[(1,)])
        # The relaxed settings are per-connection, so a new one gets the defaults.
        with sqlite3.connect(self.db) as con:
            self.assertNotEqual(con.execute('pragma synchronous').fetchone()[0],0)
    def test_open_pdf_streams_small_files_and_falls_back_to_path(self):
        import index_pdf_text
        class Stub:
            def __init__(self,fail_stream=False):self.calls=[];self.fail_stream=fail_stream
            def open(self,*a,**kw):
                if 'stream' in kw:
                    self.calls.append(('stream',kw['filetype']))
                    if self.fail_stream:raise RuntimeError('not a pdf')
                else:self.calls.append(('path',a[0]))
                return 'DOC'
        small=self.folder/'Corebook.pdf'
        stub=Stub();self.assertEqual(index_pdf_text.open_pdf(stub,small,1<<20),'DOC')
        self.assertEqual(stub.calls,[('stream','pdf')])
        stub=Stub();index_pdf_text.open_pdf(stub,small,1)          # above the cap
        self.assertEqual(stub.calls,[('path',small)])
        stub=Stub();index_pdf_text.open_pdf(stub,small,0)          # disabled
        self.assertEqual(stub.calls,[('path',small)])
        stub=Stub(fail_stream=True);index_pdf_text.open_pdf(stub,small,1<<20)
        self.assertEqual(stub.calls,[('stream','pdf'),('path',small)])
    def test_streamed_extraction_matches_reading_in_place(self):
        import index_pdf_text
        path=self.folder/'Corebook.pdf'
        rel=str(path.relative_to(self.library))
        streamed=index_pdf_text.extract((rel,str(path),1.0,1<<20))
        in_place=index_pdf_text.extract((rel,str(path),1.0,0))
        self.assertIsNone(streamed[5]);self.assertIsNone(in_place[5])
        self.assertEqual(streamed[2:5],in_place[2:5])
        self.assertIn('ancient dragon wisdom',''.join(t for _,t in streamed[4]))
    def test_default_worker_count_is_three(self):
        # Three workers, each streaming a whole file, was fastest on the real
        # disk; both fewer and more were slower. See the module docstring.
        import index_pdf_text
        self.assertEqual(index_pdf_text.DEFAULT_WORKERS,3)
        args=index_pdf_text.build_parser().parse_args(['--library','l','--db','d'])
        self.assertEqual(args.workers,3)
        self.assertEqual(args.batch_mb,index_pdf_text.BATCH_MB)
        self.assertEqual(args.stream_max_mb,index_pdf_text.STREAM_MAX_MB)
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
    def test_read_connections_refuse_to_write(self):
        import app
        app.DB=str(self.db)
        con=app.db()
        try:
            with self.assertRaises(sqlite3.OperationalError) as caught:con.execute("UPDATE files SET filename='x'")
            self.assertIn('readonly',str(caught.exception))
        finally:con.close()
        writable=app.db(write=True)
        try:writable.execute("UPDATE files SET filename=filename");writable.rollback()
        finally:writable.close()
    def test_read_routes_open_read_only_connections(self):
        import app
        from fastapi.testclient import TestClient
        app.DB=str(self.db);app.LIBRARY=str(self.library)
        client=TestClient(app.app);cid=self.query('SELECT id FROM collections')[0][0]
        real=app._connect;opened=[]
        def spy(path,write):
            opened.append(write);return real(path,write)
        with patch.object(app,'_connect',side_effect=spy):
            for path in ('/','/browse','/search?q=dragon','/api/stats','/classification','/explore','/campaigns',f'/collection/{cid}'):
                with self.subTest(path=path):self.assertEqual(client.get(path).status_code,200)
        self.assertTrue(opened);self.assertFalse(any(opened),'a GET opened a writable connection')
    def test_every_connection_gets_tuned_pragmas(self):
        import app
        app.DB=str(self.db)
        for write in (False,True):
            con=app.db(write=write)
            try:pragmas={name:con.execute('PRAGMA '+name).fetchone()[0] for name in ('cache_size','mmap_size','temp_store','busy_timeout')}
            finally:con.close()
            with self.subTest(write=write):
                self.assertEqual(pragmas,{'cache_size':-65536,'mmap_size':268435456,'temp_store':2,'busy_timeout':5000})
    def test_pragmas_are_configurable_and_validated(self):
        import app
        app.DB=str(self.db)
        with patch.dict(os.environ,{'RPG_SQLITE_CACHE_MB':'12','RPG_SQLITE_MMAP_MB':'0','RPG_SQLITE_TEMP_STORE':'file','RPG_SQLITE_BUSY_TIMEOUT_MS':'250'}):
            con=app.db()
            try:pragmas={name:con.execute('PRAGMA '+name).fetchone()[0] for name in ('cache_size','mmap_size','temp_store','busy_timeout')}
            finally:con.close()
            self.assertEqual(pragmas,{'cache_size':-12288,'mmap_size':0,'temp_store':1,'busy_timeout':250})
        # nonsense falls back to the defaults instead of reaching SQL
        with patch.dict(os.environ,{'RPG_SQLITE_CACHE_MB':'lots','RPG_SQLITE_TEMP_STORE':'MEMORY; DROP TABLE files'}):
            con=app.db()
            try:
                self.assertEqual(con.execute('PRAGMA cache_size').fetchone()[0],-65536)
                self.assertEqual(con.execute('PRAGMA temp_store').fetchone()[0],2)
                self.assertEqual(con.execute('SELECT COUNT(*) FROM files').fetchone()[0],2)
            finally:con.close()
    def test_sync_stops_and_reports_failed_stage(self):
        import app,jobs
        from types import SimpleNamespace
        with patch.object(jobs,'BASE',self.root),patch.object(app,'SYNC_LOG',str(self.root/'sync.log')),patch.object(app.subprocess,'run',side_effect=[SimpleNamespace(returncode=0),SimpleNamespace(returncode=7)]) as runner:
            app._run_sync(False,True,True,True)
            self.assertEqual(runner.call_count,2);self.assertEqual(app._sync_state['returncode'],7)
            self.assertEqual(jobs.read('sync')['stage'],'dedupe')
if __name__=='__main__':unittest.main()
