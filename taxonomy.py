"""Conservative title/series classification; storage paths are never moved.
Rules describe title evidence, not publisher identity. Third-party publishers stay intact.
Unrecognized collections retain their shelf and enter the review queue.
"""
import re, unicodedata
D20='01-DnD-and-d20';OSR='02-OSR-and-Old-School';INDIE='03-Indie-and-Story'
SCI='04-SciFi-and-Cyberpunk';HORROR='05-Horror-and-Modern';LICENSED='06-Licensed-IP'
UNIVERSAL='07-Universal-Systems';DARK='08-Warhammer-and-Grimdark';FANTASY='09-Fantasy-and-Adventure'

def normalize(value):
    return ' '.join(re.sub(r'[^\w]+',' ',unicodedata.normalize('NFKC',value).casefold()).split())

# pattern, browsing shelf, engine / family, genre, discoverability tags
RULES=[
 (r'old school essentials|\bose\b|carcass crawler',OSR,'B/X','fantasy',['osr','old-school-essentials']),
 (r'basic fantasy|chromatic dungeons',OSR,'B/X','fantasy',['osr','retro-clone']),
 (r'\bshadow ?dark\b',OSR,'Shadowdark','fantasy',['osr']),
 (r'\bknave\b',OSR,'Knave','fantasy',['osr']),
 (r'\bcairn\b',OSR,'Cairn','fantasy',['osr','nsr']),
 (r'\bdolmenwood\b',OSR,'Dolmenwood','fantasy',['osr']),
 (r'\binto the odd\b',OSR,'Into the Odd','weird-fantasy',['osr','nsr']),
 (r'pirate borg',OSR,'Mörk Borg','dark-fantasy',['osr','pirate']),
 (r'five torches deep',OSR,'Five Torches Deep','fantasy',['osr','5e-hack']),
 (r'worlds without number',OSR,'Without Number','fantasy',['osr','sandbox']),
 (r'daggerheart',INDIE,'Daggerheart','fantasy',['duality-dice']),
 (r'crown and skull',FANTASY,'Crown & Skull','fantasy',[]),
 (r'\bdragonbane\b|drakar och demoner',FANTASY,'Dragonbane','fantasy',['d20-roll-under']),
 (r'\bforbidden lands\b',FANTASY,'Year Zero','fantasy',['sandbox','forbidden-lands']),
 (r'\bvaesen\b',HORROR,'Year Zero','horror',['vaesen','investigation']),
 (r'\bsymbaroum\b',DARK,'Symbaroum','dark-fantasy',['d20-roll-under']),
 (r'\btrudvang\b',FANTASY,'Trudvang','fantasy',[]),
 (r'\bcoriolis\b',SCI,'Coriolis','sci-fi',['coriolis']),
 (r'\bdeath in space\b',SCI,'Death in Space','sci-fi',[]),
 (r'\bstars without number\b',SCI,'Without Number','sci-fi',['osr']),
 (r'^mutant (year zero|elysium|genlab alpha|mechatron|ark projects)',SCI,'Year Zero','post-apocalyptic',[]),
 (r'tales from the loop|things from the flood',SCI,'Year Zero','sci-fi',[]),
 (r'twilight (2000|2 000)',HORROR,'Year Zero','military',[]),
 (r'\brivers of london\b',HORROR,'BRP','urban-fantasy',[]),
 (r'\bdelta green\b',HORROR,'Delta Green','horror',[]),
 (r'fear itself|night s black agents|mutant city blues',HORROR,'GUMSHOE','modern',[]),
 (r'blades in the dark|band of blades|a fistful of darkness',INDIE,'Forged in the Dark','fantasy',[]),
 (r'apocalypse (world|keys)|avatar legends|monster of the week|^masks\b|rapscallion|urban shadows|^root (rpg|the |ruins|travelers)',INDIE,'PbtA',None,[]),
 (r'legend in the mist',INDIE,'Legend in the Mist','fantasy',[]),
 (r'13th age|^13 true ways$',D20,'13th Age','fantasy',[]),
 (r'\bdc20\b',D20,'DC20','fantasy',[]),
 (r'tales of the valiant',D20,'5e','fantasy',[]),
 (r'\bhackmaster\b',D20,'HackMaster','fantasy',[]),
 (r'\bgurps\b',UNIVERSAL,'GURPS',None,[]),
 (r'\bsavage\b|\bswade\b',UNIVERSAL,'Savage Worlds',None,[]),
 (r'\bcypher system\b',UNIVERSAL,'Cypher','universal',[]),
 (r'\bbasic role ?playing\b',UNIVERSAL,'BRP','universal',[]),
 (r'\bwoin\b',UNIVERSAL,'WOIN',None,[]),
 (r'\bmythras\b',FANTASY,'Mythras','fantasy',[]),
 (r'advanced fighting fantasy',FANTASY,'AFF','fantasy',[]),
 (r'\btiny dungeon\b',FANTASY,'TinyD6','fantasy',[]),
 (r'\btiny (frontiers|wastelands)\b',SCI,'TinyD6',None,[]),
 (r'\btiny (cthulhu|living dead)\b',HORROR,'TinyD6','horror',[]),
 (r'\bthe one ring\b',LICENSED,'The One Ring','fantasy',['tolkien']),
 (r'\bjudge dredd\b|\brogue trooper\b|\bstrontium dog\b',LICENSED,'WOIN','sci-fi',[]),
 (r'\bt4\b|\btraveller\b',SCI,'Traveller','sci-fi',[]),
]

def classify_title(title, category, shelf, meta):
    family,publisher,genre,tags=meta
    tags=set(tags)
    n=normalize(title)
    reason='curated-title' if family else 'folder-only'
    if category!='book':return shelf,(family,publisher,genre,sorted(tags)),reason
    # Early DCC adventure numbers refer to D&D editions, not the later DCC RPG.
    numbered=re.search(r'dungeon crawl classics\s*#\s*(\d+(?:\.\d+)?)',title,re.I)
    if numbered:
        number=float(numbered.group(1))
        if number<66.5:
            return D20,('D&D 3.x' if number<=52 else 'D&D 4e',publisher,'fantasy',sorted(tags|{'dnd-adventure'})),'dcc-edition-number'
    if re.search(r'dungeon crawl classics|\bdcc\b',n):
        return OSR,('DCC',publisher,'fantasy',sorted(tags|{'osr','dcc'})),'dcc-rpg-title'
    for pattern,target,engine,theme,extra in RULES:
        if re.search(pattern,n):
            tags.update(extra)
            return target,(engine,publisher,theme or genre,sorted(tags)), 'series: '+pattern
    if family=='OSR' or 'osr' in tags or 'retro-clone' in tags:
        return OSR,(family,publisher,genre,sorted(tags|{'osr'})),'curated-osr'
    return shelf,(family,publisher,genre,sorted(tags)),reason


def initialize(con):
    con.execute('CREATE TABLE IF NOT EXISTS classification_audit(rel_path TEXT PRIMARY KEY, source_shelf TEXT, assigned_shelf TEXT, rule TEXT, needs_review INTEGER)')
