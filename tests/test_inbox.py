import unittest,sqlite3,zipfile,time,os
from pathlib import Path
from unittest.mock import patch
import test_library as fixtures
import inbox,jobs

class InboxTests(unittest.TestCase):
 setUp=fixtures.LibraryTests.setUp
 tearDown=fixtures.LibraryTests.tearDown
 command=fixtures.LibraryTests.command
 scan=fixtures.LibraryTests.scan
 query=fixtures.LibraryTests.query
 client=fixtures.LibraryTests.client
 def arrivals(self):
  p=self.library/'_Inbox';p.mkdir(exist_ok=True);return p
 def plan(self):
  con=sqlite3.connect(self.db);con.row_factory=sqlite3.Row
  try:return inbox.plan(self.library,con)
  finally:con.close()
 def test_batch_groups_related_files_and_routes_unknown_to_unsorted(self):
  p=self.arrivals()
  for name in ['Rapscallion-Maps.pdf','Rapscallion-Playbooks.pdf','Mystery Volume.pdf']:(p/name).write_bytes(b'content')
  plan=self.plan();self.assertEqual(plan['count'],3)
  g=next(g for g in plan['groups'] if g['title']=='Rapscallion')
  self.assertEqual(len(g['items']),2);self.assertEqual(g['shelf'],'03-Indie-and-Story')
  g=next(g for g in plan['groups'] if g['title']=='Mystery Volume');self.assertEqual(g['shelf'],'_Unsorted');self.assertTrue(g['needs_review'])
 def test_upload_is_exclusive_and_rejects_traversal(self):
  c=self.client()
  with patch.object(jobs,'BASE',self.root):
   self.assertEqual(c.post('/api/inbox/upload?filename=notes.pdf',content=b'one').status_code,403)
   r=c.post('/api/inbox/upload',params={'filename':'../escape'},headers={'X-Library-Request':'1'},content=b'one');self.assertEqual(r.status_code,400)
   r=c.post('/api/inbox/upload?filename=notes.pdf',headers={'X-Library-Request':'1'},content=b'one');self.assertEqual(r.status_code,200)
   r=c.post('/api/inbox/upload?filename=notes.pdf',headers={'X-Library-Request':'1'},content=b'two');self.assertEqual(r.status_code,409)
   self.assertEqual((self.arrivals()/'notes.pdf').read_bytes(),b'one')
 def test_zip_traversal_failure_leaves_originals_and_no_destination(self):
  p=self.arrivals();source=p/'bad.zip'
  with zipfile.ZipFile(source,'w') as z:z.writestr('../escape.txt','bad')
  with patch.object(jobs,'BASE',self.root),jobs.lease() as fd:
   with self.assertRaises(ValueError):inbox.process(self.library,['bad.zip'],'Bad','_Unsorted',None,fd)
  self.assertTrue(source.exists());self.assertFalse((self.library/'Books'/'_Unsorted'/'Bad').exists());self.assertFalse((self.root/'escape.txt').exists())
 def test_import_unpacks_archives_preserves_originals_and_refuses_existing_destination(self):
  p=self.arrivals()
  with zipfile.ZipFile(p/'Delta Green.zip','w') as z:z.writestr('Maps/map.txt','map')
  with patch.object(jobs,'BASE',self.root),jobs.lease() as fd:
   dest=inbox.process(self.library,['Delta Green.zip'],'Delta Green','05-Horror-and-Modern',None,fd)
   self.assertEqual((dest/'Delta Green'/'Maps'/'map.txt').read_text(),'map')
   self.assertEqual(len(list((self.root/'inbox-originals').rglob('*.zip'))),1);self.assertFalse((p/'Delta Green.zip').exists())
   (p/'another.txt').write_text('keep')
   with self.assertRaises(ValueError):inbox.process(self.library,['another.txt'],'Delta Green','05-Horror-and-Modern',None,fd)
   self.assertTrue((p/'another.txt').exists())
 def test_automatic_endpoint_refreshes_catalog_and_rejects_stale_plan(self):
  p=self.arrivals();(p/'Rapscallion-Maps.txt').write_text('map');c=self.client()
  with patch.object(jobs,'BASE',self.root),patch.dict(os.environ,self.env):
   plan=c.get('/api/inbox/plan').json()
   bad=c.post('/api/inbox/process',headers={'X-Library-Request':'1'},json={'automatic':True,'groups':[]});self.assertEqual(bad.status_code,409)
   r=c.post('/api/inbox/process',headers={'X-Library-Request':'1'},json={'automatic':True,'groups':plan['groups']});self.assertEqual(r.status_code,202,r.text)
   deadline=time.monotonic()+15
   while jobs.read('inbox').get('running') and time.monotonic()<deadline:time.sleep(.05)
   state=jobs.read('inbox');self.assertFalse(state['running']);self.assertIsNone(state['error'],state)
   self.assertEqual(self.query("SELECT count(*) FROM files WHERE filename='Rapscallion-Maps.txt'"),[(1,)])
   self.assertEqual(c.get('/inbox').status_code,200)
 def test_hundreds_of_arrivals_need_one_group_not_per_file_metadata(self):
  p=self.arrivals()
  for i in range(300):(p/f'Rapscallion-Map-{i}.txt').write_text('map')
  plan=self.plan();self.assertEqual(plan['count'],300);self.assertEqual(len(plan['groups']),1)
  self.assertEqual(len(plan['groups'][0]['items']),300)
