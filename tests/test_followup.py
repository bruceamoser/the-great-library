import unittest,sqlite3,tempfile,os
from pathlib import Path
from unittest.mock import patch
import test_library as fixtures
import classification

class ClassificationTests(unittest.TestCase):
 def item(self,path,name='Year Zero Engine',ext='.pdf',kind='document'):
  root='Books/Engines/'+name
  return dict(rel_path=root+'/'+path,collection_path=root,collection_name=name,filename=Path(path).name,ext=ext,kind=kind)
 def test_campaign_membership_uses_full_collection_title(self):
  for suffix,filename,role in [('Handouts','CotCT_Handouts.pdf','handout'),('Poster Map','CotCT_Map.pdf','map'),('— Book 1 - Edge of Anarchy','CotCT_Book1.pdf','campaign')]:
   d=classification.classify(self.item(filename,'Pathfinder® for Savage Worlds - Curse of the Crimson Throne '+suffix))
   self.assertEqual(d['campaign'],'Curse of the Crimson Throne')
   self.assertEqual(d['series'],'Curse of the Crimson Throne')
   self.assertEqual(d['system'],'Pathfinder for Savage Worlds')
   self.assertEqual(d['role'],role)

 def test_nested_games_are_not_parent_engine(self):
  self.assertEqual(classification.classify(self.item('The One Ring/Rulebook.pdf'))['system'],'The One Ring')
  self.assertEqual(classification.classify(self.item('Dungeon World/Core.pdf','Apocalypse World Engine'))['system'],'Dungeon World')
  self.assertEqual(classification.classify(self.item('BladesInTheDark-ScumAndVillainy/Rulebook.pdf','Blades in the Dark'))['system'],'Scum and Villainy')
 def test_campaign_and_map_are_independent(self):
  d=classification.classify(self.item('D&D 5e collection/4 Curse of Strahd/Castle Map.pdf','D&D 5E'))
  self.assertEqual((d['system'],d['edition'],d['campaign'],d['role']),('Dungeons & Dragons','5e','Curse of Strahd','map'))
 def test_numbered_series_and_edition(self):
  d=classification.classify(self.item('Planar Compass Issue 2.pdf','Planar Compass Issue 2'))
  self.assertEqual((d['series'],d['volume']),('Planar Compass',2))
  d=classification.classify(self.item('AP/Abomination Vaults/PathfinderAdventurePath163RuinsOfGauntlightAbominationVaults1Of3PDF/Maps.pdf','Pathfinder Second Edition'))
  self.assertEqual((d['edition'],d['volume'],d['role']),('2e',1,'map'))
 def test_edition_markers_and_legacy_dcc(self):
  self.assertEqual(classification.classify(self.item('Rules.pdf','Cairn 2E'))['edition'],'2e')
  d=classification.classify(self.item('Dungeon Crawl Classics #28 - Into the Wilds.pdf','Dungeon Crawl Classics'))
  self.assertEqual((d['system'],d['edition']),('Dungeons & Dragons','3.x'))

 def test_uncertain_is_not_a_fabricated_system(self):
  d=classification.classify(self.item('Unknown.pdf','Some Random Bundle'))
  self.assertEqual(d['system'],'Unassigned system');self.assertEqual(d['confidence'],'review')
 def test_title_evidence_role(self):
  d=classification.classify(self.item('mystery.pdf','Traveller'),'Core Rulebook\nTraveller')
  self.assertEqual(d['role'],'core-rules');self.assertIn('indexed opening',d['evidence'])

class FollowupRoutes(unittest.TestCase):
 setUp=fixtures.LibraryTests.setUp
 tearDown=fixtures.LibraryTests.tearDown
 command=fixtures.LibraryTests.command
 scan=fixtures.LibraryTests.scan
 query=fixtures.LibraryTests.query
 client=fixtures.LibraryTests.client
 def test_return_navigation_preserves_filters_and_rejects_external(self):
  c=self.client();fid=self.query('select id from files limit 1')[0][0]
  url='/search?q=dragon&section=files&sort=size&page=2'
  r=c.get('/view/'+str(fid),params={'return_to':url});self.assertIn('Back to search results',r.text);self.assertIn('sort=size',r.text)
  self.assertNotIn('Back to search results',c.get('/view/'+str(fid),params={'return_to':'https://evil.example/search'}).text)
  self.assertIn('return_to=',c.get('/search?q=dragon&section=files').text)
 def test_explorer_and_manual_file_classification_survive_scan(self):
  import jobs
  c=self.client();fid=self.query('select id from files limit 1')[0][0]
  self.assertEqual(c.get('/explore').status_code,200)
  with patch.object(jobs,'BASE',self.root):
   r=c.post('/api/book-metadata/'+str(fid),headers={'X-Library-Request':'1'},json={'system':'My System','edition':'2e','series':'The Journey','campaign':'The Journey','volume':2,'role':'campaign'})
   self.assertEqual(r.status_code,200,r.text)
  self.scan();d=self.query("select system,edition,volume,confidence from book_classification where system='My System'")
  self.assertEqual(d,[('My System','2e',2,'manual')]);self.assertIn('The Journey',c.get('/explore?system=My+System').text)
 def test_tiff_view_uses_converted_image(self):
  import app
  self.assertEqual(app.media_of('.tif'),'image');self.assertEqual(app.media_of('.tiff'),'image')
  with sqlite3.connect(self.db) as con:con.execute("update files set ext='.tiff' where id=(select min(id) from files)")
  fid=self.query('select min(id) from files')[0][0]
  text=self.client().get('/view/'+str(fid)).text
  self.assertIn('/thumb/'+str(fid)+'?w=1600',text)
 def test_membership_card_counts_full_collection_and_keeps_editions_separate(self):
  other=self.folder.parent/'Related';other.mkdir();(other/'Map.txt').write_text('map')
  self.scan()
  with sqlite3.connect(self.db) as con:
   con.execute("UPDATE book_classification SET system='Test System',edition='',campaign='Shared Campaign',series='Shared Campaign',role='campaign'")
   con.execute("UPDATE book_classification SET edition='2e' WHERE rel_path LIKE '%dragon-notes.txt'")
   con.row_factory=sqlite3.Row
   cid=con.execute("SELECT id FROM collections WHERE name='Test Realm'").fetchone()[0]
   groups=classification.grouped(con,related_collection=cid)
   self.assertEqual([(g['edition'],len(g['files'])) for g in groups],[('',2),('2e',1)])
  c=self.client();html=c.get('/collection/'+str(cid)).text
  self.assertIn('Part of a collection',html)
  self.assertIn('2 files in the full campaign',html)
  self.assertIn('exact_edition=1',html)
  response=c.get('/explore',params={'system':'Test System','edition':'','exact_edition':1,'series':'Shared Campaign'})
  self.assertIn('Map.txt',response.text);self.assertIn('Corebook.pdf',response.text)
  self.assertNotIn('dragon-notes.txt',response.text)
