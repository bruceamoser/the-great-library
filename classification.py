"""Per-file library hierarchy with explicit evidence and persistent manual overrides.
No inference moves files, changes IDs, or asserts campaign completeness.
"""
import json,re,sqlite3,argparse
from pathlib import PurePosixPath
from collections import defaultdict
from functools import lru_cache

VERSION=1
ROLES=['core-rules','adventure','campaign','setting','bestiary','supplement','player-guide','gm-reference','character-sheet','handout','map','token','art','audio','video','tool-project','reference','unclassified']
SYSTEMS=[('Pathfinder Second Edition','Pathfinder','2e'),('Pathfinder 2e','Pathfinder','2e'),('Savage-Pathfinder','Pathfinder for Savage Worlds',''),('Pathfinder for Savage Worlds','Pathfinder for Savage Worlds',''),('D&D 5E','Dungeons & Dragons','5e'),('Dungeons and Dragons','Dungeons & Dragons',''),('Old School Essentials','Old-School Essentials',''),('Shadowdark','Shadowdark',''),('Shadow Dark','Shadowdark',''),('Dungeon World','Dungeon World',''),('Scum and Villainy','Scum and Villainy',''),('Hack the Planet','Hack the Planet',''),('Band of Blades','Band of Blades',''),('Fistful of Darkness','A Fistful of Darkness',''),('Glow in the Dark','Glow in the Dark',''),('Worlds in Peril','Worlds in Peril',''),('City of Mist','City of Mist',''),('Blade Runner','Blade Runner',''),('The One Ring','The One Ring',''),('Forbidden Lands','Forbidden Lands',''),('Forbidden Land','Forbidden Lands',''),('Mutant Year Zero','Mutant: Year Zero',''),('Pirate Borg','Pirate Borg',''),('Coriolis','Coriolis',''),('Vaesen','Vaesen',''),('Twilight2000','Twilight: 2000',''),('Dragonbane','Dragonbane',''),('Symbaroum','Symbaroum',''),('Starfinder 2E','Starfinder','2e'),('Starfinder','Starfinder','1e'),('Traveller','Traveller',''),('Call of Cthulhu','Call of Cthulhu',''),('Delta Green','Delta Green',''),('Dungeon Crawl Classics','Dungeon Crawl Classics',''),('Blades in the Dark','Blades in the Dark',''),('Savage Worlds','Savage Worlds',''),('Pathfinder','Pathfinder','1e'),('GURPS','GURPS',''),('Ironsworn','Ironsworn',''),('Dolmenwood','Dolmenwood',''),('Cairn','Cairn',''),('Knave','Knave',''),('Mork Borg','Mörk Borg',''),('Daggerheart','Daggerheart',''),('Draw Steel','Draw Steel',''),('13th Age','13th Age','')]
GENERIC={'books','assets','rules','core','core rules','core books','source books','adventures','adventure','adventure modules','modules','adventure paths','adventure path','ap','campaigns','campaign settings','settings','society','guides','maps','maps and mappacks','sheets','zines','bestiaries','companion','tokens','art','audio','music','other','additional material','pdf','pdfs','character sheets','gm','players','handouts','print','vtt','old versions'}
CAMPAIGN_CONTAINERS={'ap','adventure path','adventure paths','campaigns'}
PUBLICATION_SERIES=[]
CAMPAIGN_TITLES=[]

@lru_cache(maxsize=8192)
def words(s):
 s=re.sub(r"(\w)_s\b",r"\1's",s)
 s=re.sub(r'(?<=[a-z])(?=[A-Z])',' ',s).replace('_',' ')
 s=re.sub(r'^PF\d+-\d+\s*','',s,flags=re.I)
 return re.sub(r'\s+',' ',s).strip()
@lru_cache(maxsize=8192)
def norm(s):return re.sub(r'[^a-z0-9]+',' ',words(s).lower()).strip()
@lru_cache(maxsize=8192)
def compact(s):return re.sub(r'[^a-z0-9]','',s.lower())
def initialize(con):
 con.execute('CREATE TABLE IF NOT EXISTS book_classification(rel_path TEXT PRIMARY KEY,system TEXT,edition TEXT,series TEXT,campaign TEXT,volume INTEGER,role TEXT,confidence TEXT,evidence TEXT,version INTEGER)')
 con.execute('CREATE TABLE IF NOT EXISTS book_overrides(rel_path TEXT PRIMARY KEY,data TEXT NOT NULL)')
 con.execute('CREATE TABLE IF NOT EXISTS book_title_evidence(rel_path TEXT PRIMARY KEY,mtime REAL,text TEXT)')

def classify(row,title_text=''):
 r=dict(row);relative=r['rel_path'][len(r['collection_path'])+1:];parts=list(PurePosixPath(relative).parts);filename=parts[-1];dirs=parts[:-1]
 evidence=[];system=r['collection_name'];edition='';matched=False
 # Edition belongs to the game, not a supplement's volume number. Most specific
 # matches within each path component win. Inspect leaf before broad containers.
 for source in [filename]+list(reversed(dirs))+[r['collection_name']]:
  s=compact(source)
  choices=[x for x in SYSTEMS if compact(x[0]) in s]
  if choices:
   alias,system,edition=max(choices,key=lambda x:len(compact(x[0])))
   evidence.append('System named in: '+source);matched=True;break
 # A PF2 collection contains many product folders saying only Pathfinder.
 if system=='Pathfinder' and ('second edition' in r['collection_name'].lower() or re.search(r'\b2e\b',r['collection_name'],re.I)):edition='2e'
 if system=='Pathfinder' and 'savage' in r['collection_name'].lower():system='Pathfinder for Savage Worlds';edition=''
 if system=='Shadowdark':edition=''
 if re.search(r'(?:^|[ /_])5e(?:[ /_.-]|$)',relative,re.I) and system in ('Delve',r['collection_name']) and not matched:system='Dungeons & Dragons';edition='5e';matched=True;evidence.append('Explicit 5e compatibility in path')
 if not edition:
  explicit=re.search(r'\b([1-9])e\b',words(relative+' '+r['collection_name']),re.I)
  if explicit:edition=explicit[1]+'e';evidence.append('Explicit edition marker in path')
 if not edition:
  m=re.search(r'\b(1st|2nd|3rd|4th|5th|6th|7th|first|second|third|fourth|fifth|sixth|seventh) edition\b',words(relative),re.I)
  if m:edition=m[1].lower()+' edition';evidence.append('Explicit edition in path')
 if not matched:
  from catalog import META
  exact=next((k for k in META if norm(k)==norm(r['collection_name'])),None)
  if exact:system=words(exact);evidence.append('Curated game title: '+exact);matched=True
  else:
   for alias,game,ed in SYSTEMS:
    if re.search(r'\b'+re.escape(norm(alias))+r'\b',norm(title_text[:900])):
     system=game;edition=ed;evidence.append('Game named in indexed opening text: '+alias);matched=True;break
  if not matched:
   system='Unassigned system';evidence.append('No reliable game identification; collection: '+r['collection_name'])
 legacy=re.search(r'dungeon crawl classics\s*#\s*(\d+(?:\.\d+)?)',relative+' '+r['collection_name'],re.I)
 if legacy and float(legacy[1])<66.5:
  system='Dungeons & Dragons';edition='3.x' if float(legacy[1])<=52 else '4e';evidence.append('Early DCC adventure edition range')
 if system=='Coriolis':
  for variant in ('The Great Dark','The Third Horizon'):
   if compact(variant) in compact(relative+' '+r['collection_name']):edition=variant
 if system=='Old-School Essentials':
  for variant in ('Advanced Fantasy','Classic Fantasy'):
   if compact(variant) in compact(relative+' '+r['collection_name']):edition=variant
 # Recognize known titles where publisher product-code folders encode the title.
 from campaigns import identify
 campaign=identify(relative) or identify(r['collection_name']) or '';series='';volume=None
 if not campaign:
  for title in CAMPAIGN_TITLES:
   if compact(title) in compact(relative+' '+r['collection_name']):campaign=title;break
 nfile=norm(filename);ndirs=[norm(x) for x in dirs]
 if not campaign:
  for i,d in enumerate(ndirs):
   if d in CAMPAIGN_CONTAINERS:
    candidate=words(dirs[i+1] if i+1<len(dirs) else PurePosixPath(filename).stem)
    if norm(candidate) not in GENERIC:campaign=candidate;break
 # D&D's numbered campaign folders and similar explicitly named adventures.
 if not campaign:
  for d in dirs:
   m=re.match(r'^\d+\s+(.+)',d)
   if m and norm(m[1]) not in GENERIC and 'core' not in norm(m[1]):campaign=words(m[1]);break
 if not campaign:
  for d in dirs:
   m=re.match(r'^Adventure\s*[-–:]\s*(.+)',d,re.I)
   if m:campaign=words(m[1]);break
 if campaign:evidence.append('Campaign title/path: '+campaign)
 # Keep season, zine, sourcebook and meaningful nested product series.
 if 'society' in ' '.join(ndirs):
  season=next((words(d) for d in dirs if re.match(r'season\s*\d',d,re.I)),None)
  series='Society'+(' · '+season if season else '')
 elif campaign:series=campaign
 else:
  candidates=[words(d) for d in dirs if norm(d) not in GENERIC and norm(d)!=norm(system) and norm(d)!=norm(r['collection_name']) and not re.search(r'asset pack|single file|file per chapter|foundry|unzipped|digital assets|^\d+ core',norm(d))]
  series=candidates[0] if candidates else (r['collection_name'] if system=='Unassigned system' else '')
 if not campaign:
  for publication in PUBLICATION_SERIES:
   if compact(publication) in compact(relative+' '+r['collection_name']):series=publication;break
 # Cross-collection numbered publications group together.
 for source in [r['collection_name'],filename]:
  m=re.match(r'(.+?)\s+(?:vol(?:ume)?\.?|issue|book)\s*#?\s*(\d+)\b',words(source),re.I)
  if m and not campaign:series=m[1].strip(' -');volume=int(m[2]);evidence.append('Numbered publication: '+source);break
 num=re.search(r'(\d)\s*Of\s*\d',relative,re.I) or re.search(r'(?:book|chapter|part)\s*#?\s*(\d+)\b',words(filename),re.I)
 if num:volume=int(num[1])
 if volume is None and campaign:
  num=re.search(r'PZO90\d{2,3}\s+(\d)\b',filename,re.I) or re.search(r'\s-\s0?(\d)\s-',filename)
  if not num:num=re.search(re.escape(campaign)+r'\s+(\d)\b',words(filename),re.I)
  if num:volume=int(num[1])
 ext=r['ext'].lower();kind=r['kind'];role=None
 # Filename evidence takes priority over broad parent folders (maps in campaigns).
 patterns=[('character-sheet',r'character sheets?|charsheets?|pregens?|pre generated'),('player-guide',r'players? guide|player s guide'),('map',r'\bmaps?\b|map folio|battlemap|poster map'),('token',r'tokens?|pawns?|paper minis'),('handout',r'handouts?|cards?|decks?'),('gm-reference',r'gm screen|gamemaster screen|referee screen|cheat sheet|quick reference'),('bestiary',r'bestiary|bestiaries|monster manual|creature feature'),('core-rules',r'core\s*(?:book|rules?|rulebook)|corebook|player s handbook|players handbook|basic rules|rulebook'),('adventure',r'\badventure\b|\bscenario\b|\bmodule\b'),('setting',r'gazetteer|campaign setting|world guide|setting guide'),('supplement',r'companion|supplement|expansion|conversion|errata')]
 for value,pattern in patterns:
  if re.search(pattern,nfile,re.I):role=value;evidence.append('Role from filename: '+value);break
 if ext in ('.mp3','.wav','.ogg','.flac','.m4a'):role='audio'
 elif ext in ('.mp4','.webm','.mkv','.mov'):role='video'
 elif ext in ('.png','.jpg','.jpeg','.tif','.tiff','.webp','.gif','.svg','.bmp'):
  role=role if role in ('map','token','handout','character-sheet') else ('map' if any('map' in d for d in ndirs) or re.search(r'\bmaps?\b',norm(r['collection_name'])) else 'token' if any('token' in d for d in ndirs) else 'art')
 if not role and campaign and ext in ('.pdf','.epub'):role='campaign';evidence.append('Campaign membership from directory/title')
 if not role:
  for d in reversed(ndirs):
   if d in ('core','core rules','core books','0 core books'):role='core-rules'
   elif d in ('adventures','adventure modules','modules') or d.startswith('season '):role='adventure'
   elif d in ('bestiaries','bestiary'):role='bestiary'
   elif d in ('campaign settings','settings','lost omens','chronicles'):role='setting'
   if role:evidence.append('Role from parent directory: '+d);break
 # Only title/front-page evidence, never a passing reference anywhere in a book.
 if not role and title_text:
  front=norm(title_text[:1200])
  for value,pattern in patterns:
   if re.search(pattern,front):role=value;evidence.append('Role from indexed opening pages: '+value);break
 if not role:role={'rulebook':'core-rules','map':'map','token':'token','art':'art','character-sheet':'character-sheet','handout':'handout','project':'tool-project'}.get(kind,'unclassified')
 if series:evidence.append('Series: '+series)
 return dict(system=system,edition=edition,series=series,campaign=campaign,volume=volume,role=role,confidence='review' if role=='unclassified' or not matched else 'inferred',evidence='; '.join(evidence))

def enrich(con):
 initialize(con)
 global PUBLICATION_SERIES,CAMPAIGN_TITLES
 CAMPAIGN_TITLES=[]
 PUBLICATION_SERIES=sorted({m[1].strip(' -') for title, in con.execute('SELECT name FROM collections') if (m:=re.match(r'(.+?)\s+(?:vol(?:ume)?\.?|issue|book)\s*#?\s*\d+\b',words(title),re.I))},key=len,reverse=True)
 excerpts={r[0]:r[1] for r in con.execute('SELECT rel_path,text FROM book_title_evidence')}
 overrides={r[0]:json.loads(r[1]) for r in con.execute('SELECT rel_path,data FROM book_overrides')}
 rows=con.execute('SELECT f.*,c.name collection_name,c.rel_path collection_path,c.category collection_category,c.family collection_family FROM files f JOIN collections c ON c.id=f.collection_id').fetchall()
 old_factory=con.row_factory
 # Callers must return named rows; isolate row conversion for catalog's tuples.
 if rows and not isinstance(rows[0],sqlite3.Row):
  cur=con.execute('SELECT f.*,c.name collection_name,c.rel_path collection_path,c.category collection_category,c.family collection_family FROM files f JOIN collections c ON c.id=f.collection_id');keys=[x[0] for x in cur.description];rows=[dict(zip(keys,r)) for r in cur]
 # Learn campaign names from explicit folder/title evidence across all games,
 # then resolve abbreviated resources against their enclosing product titles.
 CAMPAIGN_TITLES=sorted({d['campaign'] for row in rows if (d:=classify(dict(row),excerpts.get(dict(row)['rel_path'],'')))['campaign'] and len(compact(d['campaign']))>=8},key=len,reverse=True)
 for row in rows:
  r=dict(row);d=classify(r,excerpts.get(r['rel_path'],''))
  if r['rel_path'] in overrides:d.update(overrides[r['rel_path']],confidence='manual',evidence='Keeper annotation')
  con.execute('INSERT OR REPLACE INTO book_classification VALUES (?,?,?,?,?,?,?,?,?,?)',(r['rel_path'],d['system'],d['edition'],d['series'],d['campaign'],d['volume'],d['role'],d['confidence'],d['evidence'],VERSION))
 con.execute('DELETE FROM book_classification WHERE rel_path NOT IN (SELECT rel_path FROM files)')
 return len(rows)

def grouped(con,q='',system='',edition='',role='',cid=None,campaign_only=False,related_collection=None,exact_edition=False):
 try:
  where=[];args=[]
  if related_collection is not None:
   # Match full-library membership using the same identity as the group cards.
   where.append("""(b.system,b.edition,COALESCE(NULLIF(b.campaign,''),NULLIF(b.series,''),'General collection')) IN
    (SELECT local.system,local.edition,COALESCE(NULLIF(local.campaign,''),NULLIF(local.series,''),'General collection')
     FROM book_classification local JOIN files member USING(rel_path) WHERE member.collection_id=?)""")
   args.append(related_collection)
  if exact_edition and edition=='':where.append("b.edition=''")
  if campaign_only:where.append("b.campaign!=''")
  for col,val in [('b.system',system),('b.edition',edition),('b.role',role),('f.collection_id',cid)]:
   if val is not None and val!='':where.append(col+'=?');args.append(val)
  if q:where.append('(b.system||\' \'||b.series||\' \'||b.campaign||\' \'||f.filename LIKE ?)');args.append('%'+q+'%')
  sql='SELECT b.*,f.id,f.filename,f.ext,f.size,f.mtime,f.collection_id,c.name collection_name,c.rel_path collection_path FROM book_classification b JOIN files f USING(rel_path) JOIN collections c ON c.id=f.collection_id'
  rows=con.execute(sql+(' WHERE '+' AND '.join(where) if where else ''),args).fetchall()
 except sqlite3.OperationalError:return []
 groups={}
 for r in rows:
  r=dict(r);name=r['campaign'] or r['series'] or 'General collection'
  key=(r['system'],r['edition'],name)
  g=groups.setdefault(key,dict(system=r['system'],edition=r['edition'],name=name,campaign=bool(r['campaign']),files=[],roles=defaultdict(int)))
  g['files'].append(r);g['roles'][r['role']]+=1
 for g in groups.values():g['files'].sort(key=lambda r:(r['volume'] is None,r['volume'] or 0,r['filename'].casefold()))
 return sorted(groups.values(),key=lambda g:(g['system'].casefold(),g['edition'],g['name'].casefold()))

if __name__=='__main__':
 from jobs import run
 p=argparse.ArgumentParser();p.add_argument('--db',required=True);p.add_argument('--extract-titles',action='store_true');a=p.parse_args()
 def work(fd):
  con=sqlite3.connect(a.db,timeout=60);con.row_factory=sqlite3.Row;initialize(con)
  if a.extract_titles:
   try:
    # Existing extracted text only: no PDF parsing, networking, or OCR work.
    titles=defaultdict(list)
    for r in con.execute('SELECT rel_path,page,substr(text,1,1800) text FROM pdf_fts WHERE CAST(page AS INTEGER)<=2'):
     titles[r['rel_path']].append((int(r['page']),r['text']))
    for path,parts in titles.items():con.execute('INSERT OR REPLACE INTO book_title_evidence VALUES (?,0,?)',(path,'\n'.join(t for _,t in sorted(parts))[:3600]))
   except sqlite3.OperationalError:pass
  print('Classified',enrich(con),'files',flush=True)
  # system/series/campaign feed files_fts, so a standalone classification run
  # must refresh the file index that /search reads.
  from catalog import rebuild_files_fts
  print('Indexed',rebuild_files_fts(con),'files',flush=True)
  con.commit();con.close()
 run('classification',work)
