"""Campaign views derived from catalog paths; no source files or IDs are changed."""
import re
from collections import defaultdict

NAMES=['Rise of the Runelords','Curse of the Crimson Throne','Second Darkness','Legacy of Fire','Council of Thieves','Kingmaker',"Serpent's Skull",'Carrion Crown','Jade Regent','Skull & Shackles','Shattered Star','Reign of Winter','Wrath of the Righteous',"Mummy's Mask",'Iron Gods','Giantslayer','Ironfang Invasion','Ruins of Azlant','War for the Crown',"Tyrant's Grasp",'Abomination Vaults','Age of Ashes','Agents of Edgewatch','Extinction Curse','Fists of the Ruby Phoenix','Strength of Thousands','Quest for the Frozen Flame','Outlaws of Alkenstar','Blood Lords']
def compact(s):return re.sub(r'[^a-z0-9]','',s.lower().replace('_s', 's'))
def slug(s):return re.sub(r'[^a-z0-9]+','-',s.lower()).strip('-')
def natural(s):return [int(x) if x.isdigit() else x.lower() for x in re.split(r'(\d+)',s)]
def identify(path):
    norm=compact(path)
    for name in NAMES:
        if compact(name) in norm:return name
    if 'agentsofedgewater' in norm:return 'Agents of Edgewatch'
    parts=path.split('/')
    for i,p in enumerate(parts[:-2]):
        if p.lower() in ('adventure paths','ap'):
            candidate=re.sub(r'^PF\d+-\d+\s*','',parts[i+1],flags=re.I).replace('_s',"'s").replace('_',' ')
            if not candidate.startswith('PathfinderAdventurePath'):return candidate
    return None

def _legacy_catalog(con):
    rows=con.execute("SELECT f.*,c.name collection_name,c.rel_path collection_path,c.family FROM files f JOIN collections c ON c.id=f.collection_id WHERE lower(c.name) LIKE '%pathfinder%' OR f.rel_path LIKE '%/Adventure Paths/%' OR f.rel_path LIKE '%/AP/%'").fetchall()
    groups={}
    for row in rows:
        r=dict(row);path=r['rel_path'];name=identify(path)
        if not name:continue
        cn=r['collection_name'].lower()
        system='savage-pathfinder' if 'savage' in cn else 'pathfinder-2e' if 'second edition' in cn else 'pathfinder-1e' if 'pathfinder' in cn else 'collection-'+str(r['collection_id'])
        key=(system,slug(name))
        g=groups.setdefault(key,dict(system=system,slug=key[1],name=name,edition={'savage-pathfinder':'Savage Worlds','pathfinder-2e':'Pathfinder 2e','pathfinder-1e':'Pathfinder 1e'}.get(system,r['collection_name']),files=[],collections={},expected=0))
        g['collections'][r['collection_id']]=r['collection_name']
        relative=path[len(r['collection_path'])+1:]
        volume=None
        numbered=re.search(r'(\d)\s*Of\s*(\d)',relative,re.I)
        if numbered:volume=int(numbered[1]);g['expected']=max(g['expected'],int(numbered[2]))
        bounds=re.search(r'PF(\d+)-(\d+)',relative,re.I)
        product=re.search(r'PZO90(\d{2,3})(?!\d)',r['filename'],re.I)
        if bounds:
            start,end=map(int,bounds.groups());g['expected']=max(g['expected'],end-start+1)
            if product and start<=int(product[1])<=end:volume=int(product[1])-start+1
        if volume is None:
            m=re.search(r'(?:book|chapter|part)\s*#?\s*0?([1-9])\b',r['filename'],re.I) or re.search(r'\s-\s0?([1-9])\s-',r['filename'])
            if not m:m=re.search(re.escape(name)+r'\s+([1-9])\b',r['filename'],re.I)
            if m:volume=int(m[1])
        low=r['filename'].lower()
        role='maps' if 'map' in low or r['subcategory']=='maps' else 'extras' if r['ext'] not in ('.pdf','.epub') or any(x in low for x in ('guide','pawn','token','minis','deck','conversion','handout','sheet')) else 'books'
        r.update(volume=volume,role=role);g['files'].append(r)
    for g in groups.values():
        g['files'].sort(key=lambda r:(r['volume'] is None,r['volume'] or 0,natural(r['filename'])))
        g['sections']=[(role,[r for r in g['files'] if r['role']==role]) for role in ('books','maps','extras')]
        g['present']=sorted({r['volume'] for r in g['files'] if r['role']=='books' and r['volume']})
        g['missing']=[n for n in range(1,g['expected']+1) if n not in g['present']] if g['present'] else []
        g['url']='/campaign/'+g['system']+'/'+g['slug']
    return sorted(groups.values(),key=lambda g:(g['edition'],g['name']))


def catalog(con):
    import classification
    if not con.execute("SELECT 1 FROM sqlite_master WHERE name='book_classification'").fetchone():return _legacy_catalog(con)
    result=[]
    for g in classification.grouped(con,campaign_only=True):
        if not g['campaign']:continue
        files=[]
        for original in g['files']:
            r=dict(original)
            r['role']='maps' if r['role']=='map' else 'books' if r['role'] in ('campaign','adventure','core-rules','supplement','unclassified') and r['ext'] in ('.pdf','.epub') else 'extras'
            files.append(r)
        key=slug(g['system']+' '+g['edition'])
        result.append(dict(system=key,slug=slug(g['name']),name=g['name'],edition=g['system']+' '+g['edition'],files=files,collections={r['collection_id']:r['collection_name'] for r in files},expected=0,present=sorted({r['volume'] for r in files if r['volume'] and r['role']=='books'}),missing=[],sections=[(role,[r for r in files if r['role']==role]) for role in ('books','maps','extras')],url='/campaign/'+key+'/'+slug(g['name'])))
    return result
