#!/usr/bin/env python3
"""
Build the RPG library catalog (SQLite + FTS5) from /mnt/disk5/RPG-Library.

Collections are the game folders directly inside each shelf. Every file gets a
"kind" (rulebook / character-sheet / map / art / audio / vtt-data / software ...)
derived from its name and extension. Families, publishers, genres and tags come
from the curated META table below and are meant to be hand-editable in the app.

Usage:
  python3 catalog.py --library /mnt/disk5/RPG-Library --db library.db \
      --moves shelf-moves.json
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

# ------------------------------------------------------------ curated meta ---
# family    : the rules engine / game family
# publisher : who makes it
# genre     : primary genre
# tags      : free-form extras
META = {
    # ---- d20 / D&D-descended -------------------------------------------------
    '13th Age': ('d20', 'Pelgrane Press', 'fantasy', ['icons', 'escalation-die']),
    'Basic Fantasy Roleplaying': ('d20', 'Chris Gonnerman', 'fantasy', ['retro-clone', 'free']),
    'Chromatic Dungeons': ('d20', 'Chromatic Dungeons', 'fantasy', ['retro-clone']),
    'D&D 5E': ('d20', 'Wizards of the Coast', 'fantasy', ['5e', 'official']),
    'DC20': ('d20', 'The Dungeon Coach', 'fantasy', ['5e-alternative']),
    'Daggerheart': ('d20', 'Darrington Press', 'fantasy', ['critical-role', 'cards']),
    'Daggerheart-Playtest-Materials': ('d20', 'Darrington Press', 'fantasy', ['playtest']),
    'Draw Steel': ('d20', 'MCDM', 'fantasy', ['tactical']),
    'Dungeon Crawl Classics': ('d20', 'Goodman Games', 'fantasy', ['osr', 'dice-chain']),
    'Five Torches Deep': ('d20', 'Sigil Stone', 'fantasy', ['osr', '5e-hack']),
    'Hackmaster': ('d20', 'Kenzer & Company', 'fantasy', ['crunchy']),
    'Havenshield': ('d20', 'Havenshield', 'fantasy', ['5e']),
    'Heroes Against The Darkness': ('d20', 'HATD', 'fantasy', ['5e']),
    'Index Card RPG': ('d20', 'Runehammer', 'fantasy', ['icrpg', 'rules-light']),
    'Knave 2e': ('d20', 'Questing Beast', 'fantasy', ['osr', 'rules-light']),
    'Mazes Fantasy Roleplaying': ('d20', '9th Level Games', 'fantasy', ['osr']),
    'Midgard': ('d20', 'Kobold Press', 'fantasy', ['5e', 'setting']),
    'Mutants and Masterminds': ('d20', 'Green Ronin', 'superhero', ['point-buy']),
    'Nimble': ('d20', 'Nimble Co', 'fantasy', ['5e-alternative']),
    'Old School Essentials': ('d20', 'Necrotic Gnome', 'fantasy', ['osr', 'bx']),
    'Pathfinder': ('d20', 'Paizo', 'fantasy', ['pf1', 'crunchy']),
    'Pathfinder Second Edition': ('d20', 'Paizo', 'fantasy', ['pf2', 'crunchy']),
    'Shadow Dark': ('d20', 'The Arcane Library', 'fantasy', ['osr', 'rules-light']),
    'Sword Chronicle': ('d20', 'Green Ronin', 'fantasy', ['feudal', 'intrigue']),
    'Sword Chronicle - Feudal Fantasy Roleplaying': ('d20', 'Green Ronin', 'fantasy', ['feudal']),
    'Tales of the Valiant': ('d20', 'Kobold Press', 'fantasy', ['5e-alternative']),
    'Xtreme dungeon master': ('d20', 'Xtreme DM', 'fantasy', ['osr']),
    'dotDungeon': ('d20', 'dotDungeon', 'fantasy', ['rules-light']),
    'Shield Maidens': ('d20', 'Shield Maidens', 'fantasy', ['norse', '5e']),

    # ---- OSR / old-school ----------------------------------------------------
    'Arden Vul': ('OSR', 'Expeditious Retreat', 'fantasy', ['megadungeon']),
    'Cairn 2E': ('OSR', 'Yochai Gal', 'fantasy', ['rules-light', 'free']),
    'Curse of the Weaver Queen': ('OSR', 'OSR', 'fantasy', ['adventure']),
    'Delve': ('OSR', 'Delve', 'fantasy', ['5e', 'shadowdark', 'maps']),
    'Dolmenwood': ('OSR', 'Necrotic Gnome', 'fantasy', ['setting', 'fairy-tale']),
    'Gods of the Forbidden North': ('OSR', 'Forbidden North', 'fantasy', ['sandbox']),
    'Land of Em': ('OSR', 'Land of Eem', 'fantasy', ['family-friendly']),
    'Old School Manifesto': ('OSR', 'OSR', 'fantasy', []),
    'OldSkull RPG': ('OSR', 'OldSkull', 'fantasy', ['zine']),
    'The Devil_s Dandy Dogs': ('OSR', 'Indie', 'fantasy', ['adventure']),
    'Worlds without Number': ('OSR', 'Sine Nomine', 'sci-fantasy', ['sandbox', 'free']),

    # ---- Indie / story games -------------------------------------------------
    '2D6 Dungeon': ('solo', '2D6 Dungeon', 'fantasy', ['solo', 'dungeon-crawl']),
    'Apocalypse Keys': ('PbtA', 'Evil Hat', 'modern-horror', ['monster-of-the-week']),
    'Apocalypse World Engine': ('PbtA', 'Various', 'various', ['pbta', 'engine']),
    'Avatar Legends': ('PbtA', 'Magpie Games', 'fantasy', ['licensed', 'avatar']),
    'Be Like a Cat, A Solo_Duet TTRPG': ('solo', 'Indie', 'slice-of-life', ['solo']),
    'Be Like a Crow, A Solo TTRPG': ('solo', 'Critical Kit', 'slice-of-life', ['solo']),
    'Blades in the Dark ': ('Forged in the Dark', 'John Harper', 'heist-fantasy', ['duskvol']),
    'Castle Falkenstein': ('custom', 'R. Talsorian', 'steampunk', ['victorian', 'cards']),
    'City of Mist': ('PbtA', 'Son of Oak', 'urban-fantasy', ['noir', 'tags']),
    'Cool New Game': ('indie', 'Unknown', 'unknown', ['unsorted']),
    'Crown and Skull': ('Crown & Skull', 'Runehammer', 'dark-fantasy', []),
    'Fate': ('Fate', 'Evil Hat', 'universal', ['aspects']),
    'Goblin Quest': ('indie', 'Indie', 'comedy', ['gm-less']),
    'Household': ('indie', 'Two Little Mice', 'fantasy', ['fairy-tale']),
    'Ironsworn': ('PbtA', 'Shawn Tomkin', 'low-fantasy', ['solo', 'free', 'oracle']),
    'Kobolds Ate My Baby! The Orange Book': ('indie', '9th Level Games', 'comedy', ['beer-and-pretzels']),
    'Legacy_ Rhapsody of Blood (Worlds of Legacy 4)': ('PbtA', 'UFO Press', 'fantasy', ['legacy']),
    'Legend In The Mist': ('indie', 'Son of Oak', 'fantasy', ['city-of-mist-engine']),
    'LightMaster': ('indie', 'Unknown', 'unknown', []),
    'Masks': ('PbtA', 'Magpie Games', 'superhero', ['teen-drama']),
    'Memento Mori': ('indie', 'Indie', 'horror', ['cards']),
    'Monster of the Week Revised': ('PbtA', 'Evil Hat', 'modern-horror', ['monster-hunting']),
    'Outgunned': ('indie', 'Two Little Mice', 'action', ['cinematic']),
    'Pericle': ('indie', 'Unknown', 'fantasy', []),
    'Pro Patria Mori': ('indie', 'Indie', 'historical', ['ww1']),
    'Punk Is Dead': ('indie', 'Indie', 'modern', ['punk']),
    'Root RPG': ('PbtA', 'Magpie Games', 'fantasy', ['licensed', 'woodland']),
    'Sentai & Sensibility': ('PbtA', 'Indie', 'comedy', ['sentai', 'jane-austen']),
    'Slugblaster _ Kickflip Over a Quantum Centipede': ('Forged in the Dark', 'Wilhelm''s', 'sci-fi', ['teens', 'fitd']),
    'Spire_ The City Must Fall': ('custom', 'Rowan Rook', 'dark-fantasy', ['drow', 'resistance']),
    'The King Is Dead': ('indie', 'Indie', 'fantasy', []),
    'The Love Balloon': ('indie', 'Indie', 'comedy', []),
    'The Wildsea_ Core Rules': ('custom', 'Mythworks', 'sci-fantasy', ['wildsea', 'dice-pool']),
    'Tidal Blades_ the Roleplaying Game': ('indie', 'Druid City', 'fantasy', ['licensed']),
    'Time Force': ('indie', 'Indie', 'sci-fi', ['time-travel']),
    'Tome of Fey Lore': ('indie', 'Indie', 'fantasy', ['fey']),
    'Touched By The Gods': ('indie', 'Indie', 'dark-fantasy', []),
    'Under Hollow Hills': ('PbtA', 'Indie', 'fantasy', ['fairy', 'troupe']),
    'Urban Shadows': ('PbtA', 'Magpie Games', 'urban-fantasy', ['political']),
    'Vagabond': ('indie', 'Indie', 'fantasy', []),
    'Warlock Traitors Edition': ('indie', 'Indie', 'dark-fantasy', ['zine']),

    # ---- Sci-fi / cyberpunk --------------------------------------------------
    'Cyberpunk': ('Interlock', 'R. Talsorian', 'cyberpunk', ['classic', '2020']),
    'Cyberpunk RED': ('Interlock', 'R. Talsorian', 'cyberpunk', ['red']),
    'Dreams And Machines_ Player_s Guide': ('custom', 'Modiphius', 'sci-fi', ['2d20', 'post-apoc']),
    'Fragged Empire': ('custom', 'Design Ministries', 'sci-fi', ['space']),
    'Mutant Chronicles': ('2d20', 'Modiphius', 'sci-fi', ['dieselpunk']),
    'ShadowRun': ('dice-pool', 'Catalyst', 'cyberpunk-fantasy', ['sixth-world']),
    'Starfinder': ('d20', 'Paizo', 'sci-fantasy', ['sf1']),
    'Starfinder 2E': ('d20', 'Paizo', 'sci-fantasy', ['sf2']),
    'Traveller': ('2d6', 'Mongoose', 'sci-fi', ['space', 'classic', 'software']),
    'Trinity Continuum Core Rulebook': ('Storypath', 'Onyx Path', 'sci-fi', ['aeon']),

    # ---- Horror / modern -----------------------------------------------------
    'Absolute Power': ('Tri-Stat', 'Dyskami', 'superhero', ['silver-age']),
    'Buffy-Angel': ('Unisystem', 'Eden Studios', 'modern-horror', ['licensed', 'tv']),
    'Call of Cthulhu 7th Edition - Keeper_s Rulebook': ('BRP', 'Chaosium', 'horror', ['cthulhu', 'coc7']),
    'Dresden Files': ('Fate', 'Evil Hat', 'urban-fantasy', ['licensed']),
    'GumShoe Games': ('GUMSHOE', 'Pelgrane Press', 'horror', ['investigation', 'nba', 'fear-itself']),
    'Monster Hunter International - Savaged': ('Savage Worlds', 'Battlefield Press', 'modern-horror', ['licensed']),
    'Scion Second Edition Book One_ Origin': ('Storypath', 'Onyx Path', 'modern-mythic', ['gods']),
    'Supernatural': ('d20', 'Margaret Weis', 'modern-horror', ['licensed', 'tv']),
    'They Came From Beneath the Sea!': ('Storypath', 'Onyx Path', 'comedy-horror', ['b-movie']),

    # ---- Licensed IP ---------------------------------------------------------
    'Conan': ('2d20', 'Modiphius', 'sword-and-sorcery', ['licensed', 'hyboria']),
    'Dishonored_ The Roleplaying Game Core Rulebook': ('2d20', 'Modiphius', 'dark-fantasy', ['licensed', 'videogame']),
    'Dr. Who': ('2d6', 'Cubicle 7', 'sci-fi', ['licensed', 'tv']),
    'Dragon Age': ('AGE', 'Green Ronin', 'dark-fantasy', ['licensed', 'videogame']),
    'G.I. JOE Roleplaying Game Core Rulebook': ('Essence20', 'Renegade', 'action', ['licensed', 'toy']),
    'Homeworld_ Revelations - Core Rulebook': ('2d20', 'Modiphius', 'sci-fi', ['licensed', 'videogame']),
    'Judge-Dredd-Worlds-of-2000ad': ('WOIN', 'EN Publishing', 'sci-fi', ['licensed', 'comic']),
    'Middle Earth Roleplaying': ('MERP', 'Iron Crown Enterprises', 'fantasy', ['licensed', 'tolkien']),
    'Middle-Earth-5E': ('d20', 'Cubicle 7', 'fantasy', ['licensed', 'tolkien', '5e']),
    'Power Rangers Roleplaying Game Core Rulebook': ('Essence20', 'Renegade', 'action', ['licensed', 'tv']),
    'Serenity ': ('d20', 'Margaret Weis', 'sci-fi', ['licensed', 'firefly']),
    'The Witcher TRPG': ('custom', 'R. Talsorian', 'dark-fantasy', ['licensed']),
    'Transformers Roleplaying Game Core Book': ('Essence20', 'Renegade', 'sci-fi', ['licensed', 'toy']),

    # ---- Universal / toolkit -------------------------------------------------
    'Advanced Fighting Fantasy': ('AFF', 'Arion Games', 'fantasy', ['classic', 'british']),
    'Basic Role Playing (BRP)': ('BRP', 'Chaosium', 'universal', ['d100']),
    'Chivarly And Sorcery': ('BRP', 'Britannia Game Designs', 'fantasy', ['d100', 'medieval']),
    'Cypher System': ('Cypher', 'Monte Cook Games', 'universal', ['numenera']),
    'EZ-D6': ('d6', 'Indie', 'universal', ['rules-light']),
    'Fantasy Age': ('AGE', 'Green Ronin', 'fantasy', ['stunt-dice']),
    'Gurps': ('GURPS', 'Steve Jackson Games', 'universal', ['point-buy', 'crunchy']),
    'Harp': ('Rolemaster', 'Iron Crown Enterprises', 'fantasy', ['d100']),
    'Modern AGE Basic Rulebook': ('AGE', 'Green Ronin', 'modern', ['stunt-dice']),
    'Mythras': ('BRP', 'Design Mechanism', 'fantasy', ['d100']),
    'RuneQuest - Roleplaying in Glorantha': ('BRP', 'Chaosium', 'fantasy', ['d100', 'glorantha']),
    'Savage Worlds': ('Savage Worlds', 'Pinnacle', 'universal', ['fast-furious-fun', 'cards']),
    'Savage Worlds Fantasy': ('Savage Worlds', 'Pinnacle', 'fantasy', ['supplement']),
    'Savage Worlds Fantasy Companion': ('Savage Worlds', 'Pinnacle', 'fantasy', ['companion']),
    'Savage Worlds Rifts': ('Savage Worlds', 'Pinnacle', 'sci-fantasy', ['licensed', 'rifts']),
    'Savage Worlds Science Fiction': ('Savage Worlds', 'Pinnacle', 'sci-fi', ['companion']),
    'Savage-Pathfinder': ('Savage Worlds', 'Pinnacle', 'fantasy', ['licensed', 'pathfinder']),
    'Tiny Dungeon': ('TinyD6', 'Gallant Knight', 'fantasy', ['rules-light']),
    'WOIN': ('WOIN', 'EN Publishing', 'universal', ['d6-pool']),
    'Year Zero Engine': ('Year Zero', 'Free League', 'various', ['yzE', 'free-league']),

    # ---- Warhammer / grimdark ------------------------------------------------
    'Dark Heresy': ('d100', 'Fantasy Flight', 'grimdark-sci-fi', ['warhammer40k']),
    'DarkEye': ('TDE', 'Ulisses Spiele', 'fantasy', ['german', 'crunchy']),
    'Darkmaster': ('Rolemaster', 'Open Ended Games', 'dark-fantasy', ['vsdarkmaster']),
    'Dragonbane': ('Year Zero', 'Free League', 'fantasy', ['d20', 'duck']),
    'Shadow Of The Demon Lord': ('d20', 'Schwalb Entertainment', 'grimdark', ['horror']),
    'Symbaroum': ('Year Zero', 'Free League', 'dark-fantasy', ['grimdark']),
    'Trudvang': ('Year Zero', 'Free League', 'fantasy', ['norse']),
    'Warhammer': ('d100', 'Cubicle 7', 'grimdark', ['warhammer-fantasy']),

    # ---- Software / accessories ---------------------------------------------
    'Foundry': ('VTT', 'Foundry Gaming', 'software', ['vtt', 'modules']),
    'Pirate-Borg': ('Year Zero', 'Limithron', 'dark-fantasy', ['pirate', 'vtt', 'foundry']),
}

# accessory publishers moved out of the DriveThru-RPG dump
DTRPG_SOURCE_PREFIX = 'DriveThru-RPG/'

CTX_TAG = {
    '10-Maps-and-Accessories': 'maps',
    '09-VTT-and-Software': 'software',
    '11-DriveThruRPG': 'dtrpg',
    '_Unsorted': 'unsorted',
    '_Inbox': 'inbox',
}


def clean_name(raw: str) -> str:
    n = re.sub(r"(\w)_s\b", r"\1's", raw)
    n = re.sub(r"\s_\s", ": ", n)
    n = re.sub(r"_ ", ": ", n)
    n = n.replace('_', ' ')
    return re.sub(r"\s+", " ", n).strip()


MEDIA = {'.mp3', '.m4a', '.wav', '.ogg', '.flac', '.aac'}
VIDEO = {'.mp4', '.webm', '.mkv', '.mov'}
IMAGE = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.tif', '.tiff', '.psd', '.bmp', '.svg'}
SOFTWARE = {'.exe', '.dll', '.dylib', '.nib', '.cab', '.msi', '.dl_', '.bat', '.app'}
VTTDATA = {'.db', '.dat', '.ldb', '.lock', '.json', '.xml', '.css', '.js',
           '.ttf', '.otf', '.woff', '.woff2', '.nib'}
DOCS = {'.docx', '.doc', '.rtf', '.odt', '.txt', '.md', '.html', '.htm'}
SHEETS = {'.xlsx', '.xls', '.xlsm', '.csv', '.gsheet'}
EBOOK = {'.epub', '.mobi', '.azw3'}
ARCHIVE = {'.zip', '.rar', '.7z', '.gz', '.tar'}
# native files of map/token creation tools (Dungeon Alchemist, DungeonDraft,
# Character Creator, Clip Studio Paint, Paint.NET). Kept, not deleted -- they
# are the editable sources behind the exported art.
PROJECT = {'.dam', '.dungeondraft_map', '.pcg', '.dps', '.clip', '.pdn',
           '.dungeondraft', '.wonderdraft'}


def classify(filename: str, ext: str) -> str:
    n = re.sub(r'[_-]+', ' ', filename.lower())
    if ext in SOFTWARE:
        return 'software'
    if ext in ('.db', '.ldb', '.lock'):
        return 'vtt-data'
    if ext in MEDIA:
        return 'audio'
    if ext in VIDEO:
        return 'video'
    if ext in IMAGE:
        if re.search(r'\bmaps?\b', n):
            return 'map'
        if 'token' in n or 'mini' in n:
            return 'token'
        if 'sheet' in n:
            return 'character-sheet'
        return 'art'
    if ext == '.pdf':
        if re.search(r'\b(?:character ?sheets?|char ?sheets?|pc sheet|sheets?|cs)\b',n) or any(k in n for k in ('character sheet', 'charsheet', 'charsheet',
                               '_sheet', 'sheet_', 'pc_sheet', 'cs_')):
            return 'character-sheet'
        if re.search(r'\bmaps?\b', n):
            return 'map'
        if any(k in n for k in ('screen', 'handout', 'folio')):
            return 'handout'
        if any(k in n for k in ('rulebook', 'core book', 'core rule', 'core rules', 'corebook',
                               '_core', 'rules', 'rulebook')):
            return 'rulebook'
        return 'document'
    if ext in EBOOK:
        return 'ebook'
    if ext in PROJECT:
        return 'project'
    if ext in DOCS:
        return 'document'
    if ext in SHEETS:
        return 'data'
    if ext in ARCHIVE:
        return 'archive'
    if ext in VTTDATA:
        return 'vtt-data'
    return 'other'


# ------------------------------------------------------- book / asset model ---
# Two categories. Software is deliberately NOT tracked at all.
UNTRACKED_KINDS = {'software', 'vtt-data', 'archive'}

CAMPAIGN_HINTS = ('adventure', 'module', 'campaign', 'scenario', 'quest',
                  'dungeon crawl classics #', 'starter set', 'quickstart')
CORE_HINTS = ('core', 'rulebook', 'rule book', 'rules', 'bestiary',
              'monster manual', 'player', 'gamemaster', 'game master',
              'keeper', 'referee')


def classify_category(kind: str, filename: str):
    """Return (category, subcategory), or (None, None) if untracked.

    category is 'book' or 'asset'. Computed PER FILE so a game's maps and its
    rulebook can be categorised independently while living in one folder.
    """
    if kind in UNTRACKED_KINDS:
        return None, None
    if kind in ('map', 'art', 'token', 'audio', 'video'):
        sub = {'map': 'maps', 'art': 'art', 'token': 'art',
               'audio': 'audio', 'video': 'video'}[kind]
        return 'asset', sub
    if kind == 'project':
        return 'asset', 'project'
    if kind in ('character-sheet', 'handout', 'data'):
        return 'book', 'sheets'
    if kind == 'ebook':
        return 'book', 'core-rules'
    n = re.sub(r'[_-]+', ' ', filename.lower())
    if re.search(r'\b(bestiary|monster manual|creature|creatures|companion|expansion)\b',n):
        return 'book', 'expansion'
    if kind == 'rulebook':
        if any(re.search(r'\b'+re.escape(h)+r'\b',n) for h in CAMPAIGN_HINTS):
            return 'book', 'campaign'
        return 'book', 'core-rules'
    # document / other
    if any(re.search(r'\b'+re.escape(h)+r'\b',n) for h in CAMPAIGN_HINTS):
        return 'book', 'campaign'
    if any(re.search(r'\b'+re.escape(h)+r'\b',n) for h in CORE_HINTS):
        return 'book', 'core-rules'
    return 'book', 'expansion'


TOP_CATEGORIES = ('Books', 'Assets')


def iter_collections(library):
    """Yield (category, shelf, folder, path) for every collection.

    Supports both layouts so it works before and after the restructure:
        Books/<shelf>/<collection>      (new)
        Assets/<collection>             (new)
        <shelf>/<collection>            (legacy flat shelves)
        _Inbox/_Unsorted                (always)
    """
    tops = sorted(d for d in os.listdir(library)
                  if os.path.isdir(os.path.join(library, d)))
    new_layout = any(t in TOP_CATEGORIES for t in tops)

    def subdirs(p):
        return sorted(d for d in os.listdir(p)
                      if os.path.isdir(os.path.join(p, d)))

    def loose(p):
        return sorted(d for d in os.listdir(p)
                      if os.path.isfile(os.path.join(p, d)))

    for top in tops:
        top_path = os.path.join(library, top)
        if new_layout and top in TOP_CATEGORIES:
            if top == 'Books':
                for shelf in subdirs(top_path):
                    shelf_dir = os.path.join(top_path, shelf)
                    for c in subdirs(shelf_dir):
                        yield 'book', shelf, c, os.path.join(shelf_dir, c), False
                    if loose(shelf_dir):
                        yield 'book', shelf, f'({shelf})', shelf_dir, True
            else:
                for c in subdirs(top_path):
                    yield 'asset', 'Assets', c, os.path.join(top_path, c), False
                if loose(top_path):
                    yield 'asset', 'Assets', '(Assets)', top_path, True
        elif new_layout:
            # _Inbox / _Unsorted
            for c in subdirs(top_path):
                yield 'other', top, c, os.path.join(top_path, c), False
            if loose(top_path):
                yield 'other', top, f'({top})', top_path, True
        else:
            # legacy: flat shelves
            for c in subdirs(top_path):
                yield 'book', top, c, os.path.join(top_path, c), False
            if loose(top_path):
                yield 'book', top, f'({top})', top_path, True


SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS collections (
  id          INTEGER PRIMARY KEY,
  shelf       TEXT NOT NULL,
  category    TEXT,
  folder      TEXT NOT NULL,          -- raw folder name on disk
  name        TEXT NOT NULL,          -- cleaned display name
  sort_name   TEXT NOT NULL,
  family      TEXT,
  publisher   TEXT,
  genre       TEXT,
  source      TEXT,
  rel_path    TEXT NOT NULL UNIQUE,
  n_files     INTEGER DEFAULT 0,
  n_bytes     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
  id            INTEGER PRIMARY KEY,
  collection_id INTEGER NOT NULL REFERENCES collections(id),
  rel_path      TEXT NOT NULL UNIQUE,
  filename      TEXT NOT NULL,
  ext           TEXT,
  kind          TEXT,
  category      TEXT,
  subcategory   TEXT,
  size          INTEGER,
  mtime         REAL
);
CREATE INDEX IF NOT EXISTS idx_files_collection ON files(collection_id);
CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);

CREATE TABLE IF NOT EXISTS tags (id INTEGER PRIMARY KEY, name TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS collection_tags (
  collection_id INTEGER NOT NULL,
  tag_id        INTEGER NOT NULL,
  PRIMARY KEY (collection_id, tag_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(
  collection_id UNINDEXED,
  name,
  shelf,
  tags,
  filenames,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

# Per-file index behind the "Books & assets" section of /search. A leading
# wildcard LIKE over 25k filenames could not use an index and scanned the whole
# files and book_classification tables on every keystroke; this table is built
# once per scan instead. Kept separate from `search` (one row per collection)
# because the files section pages over individual files. `id` points back at
# files.id and is not tokenised.
FILES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  filename,
  system,
  series,
  campaign,
  id UNINDEXED,
  tokenize = 'unicode61 remove_diacritics 2'
);
"""

FILES_FTS_INSERT = """
INSERT INTO files_fts(filename, system, series, campaign, id)
SELECT f.filename, COALESCE(b.system, ''), COALESCE(b.series, ''),
       COALESCE(b.campaign, ''), f.id
  FROM files f LEFT JOIN book_classification b ON b.rel_path = f.rel_path
"""

SCHEMA += FILES_FTS_DDL


def rebuild_files_fts(con):
    """Rebuild the per-file index. Call after classification.enrich().

    system/series/campaign come from book_classification, so the index is only
    correct once enrichment has run. The delete and the rebuild happen in the
    caller's transaction, so a reader sees the previous index or the new one,
    never a half-built table.
    """
    import classification
    classification.initialize(con)
    con.execute(FILES_FTS_DDL)
    con.execute('DELETE FROM files_fts')
    con.execute(FILES_FTS_INSERT)
    return con.execute('SELECT COUNT(*) FROM files_fts').fetchone()[0]


def reindex_file(con, file_id):
    """Refresh one file's index row after a manual classification change.

    Returns False when files_fts has not been built yet; the next catalog scan
    creates and fills it. It deliberately never creates the table on its own: a
    partially populated index would silently hide the rest of the library from
    the files search.
    """
    try:
        con.execute('DELETE FROM files_fts WHERE id = ?', (file_id,))
    except sqlite3.OperationalError:
        return False
    con.execute(FILES_FTS_INSERT + ' WHERE f.id = ?', (file_id,))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--library', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--moves', help='shelf-moves.json, to recover provenance')
    ap.add_argument('--provenance',
                    help='JSON mapping rel_path -> {source, publisher, ...}')
    args = ap.parse_args()

    library = os.path.abspath(args.library)

    # provenance registry: rel_path -> {source, publisher, ...}. Lets the library
    # be one library with many sources instead of one shelf per source.
    prov = {}
    if args.provenance and os.path.exists(args.provenance):
        with open(args.provenance) as fh:
            prov = json.load(fh)

    # provenance: which rel_paths came out of the DriveThru-RPG dump
    dtrpg_paths = set()
    if args.moves and os.path.exists(args.moves):
        for m in json.load(open(args.moves)):
            if m['from'].startswith(DTRPG_SOURCE_PREFIX):
                dtrpg_paths.add(m['to'])

    # Update the catalog in one transaction. Existing IDs and content indexes survive.
    con = sqlite3.connect(args.db, timeout=60)
    if not os.path.isdir(library) or not any(os.scandir(library)):
        raise RuntimeError('Library directory is unavailable; catalog left unchanged.')
    con.executescript(SCHEMA)
    import taxonomy
    taxonomy.initialize(con)
    from metadata import initialize, get
    initialize(con)
    con.execute('CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)')
    con.execute('INSERT OR IGNORE INTO schema_migrations(version) VALUES (1)')
    con.commit()
    con.execute('BEGIN IMMEDIATE')
    con.execute('CREATE TEMP TABLE seen_collections(id INTEGER PRIMARY KEY)')
    con.execute('CREATE TEMP TABLE seen_files(rel_path TEXT PRIMARY KEY)')
    con.execute('DELETE FROM search')
    con.execute('DELETE FROM collection_tags')
    cur = con.cursor()

    con.execute('CREATE TABLE IF NOT EXISTS catalog_sequences(name TEXT PRIMARY KEY, value INTEGER NOT NULL)')
    for table in ('files','collections'):
        con.execute('INSERT INTO catalog_sequences VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=MAX(value,excluded.value)',
                    (table, con.execute(f'SELECT COALESCE(MAX(id),0) FROM {table}').fetchone()[0]))
    def next_id(table):
        con.execute('UPDATE catalog_sequences SET value=value+1 WHERE name=?',(table,))
        return con.execute('SELECT value FROM catalog_sequences WHERE name=?',(table,)).fetchone()[0]
    tag_ids = {}

    def tag_id(name):
        if name not in tag_ids:
            cur.execute('INSERT OR IGNORE INTO tags(name) VALUES (?)', (name,))
            tag_ids[name] = cur.execute('SELECT id FROM tags WHERE name=?',(name,)).fetchone()[0]
        return tag_ids[name]

    normalized_meta = {taxonomy.normalize(k):v for k,v in META.items()}
    n_collections = 0
    n_files = 0
    untracked = 0
    file_rows = []

    for category, shelf, folder, path, loose_only in iter_collections(library):
        rel = os.path.relpath(path, library)
        name = clean_name(folder) if not folder.startswith('(') else shelf
        source_shelf = shelf
        meta = normalized_meta.get(taxonomy.normalize(clean_name(folder)), (None,None,None,[]))
        shelf,(fam,pub,gen,extra),rule=taxonomy.classify_title(folder,category,shelf,meta)

        override = get(con, rel)
        name = override.get('name') or name
        fam = override.get('family', fam)
        pub = override.get('publisher', pub)
        gen = override.get('genre', gen)
        shelf = override.get('shelf') or shelf
        tags = set(override.get('tags',extra))
        if override.get('shelf'):rule='manual'
        con.execute('INSERT OR REPLACE INTO classification_audit VALUES (?,?,?,?,?)',(rel,source_shelf,shelf,rule,int(rule=='folder-only' and category=='book'))) 
        if gen:
            tags.add(gen)
        if fam:
            tags.add(fam)
        if shelf in CTX_TAG:
            tags.add(CTX_TAG[shelf])
        if rel in prov:
            entry = prov[rel]
            source = entry.get('source', 'takeout')
            tags.add(source)
            if entry.get('publisher'):
                pub = pub or entry['publisher']
            if entry.get('product'):
                tags.add('dtrpg')
        elif any(r == rel or r.startswith(rel + '/') for r in dtrpg_paths):
            source = 'dtrpg'
            tags.add('dtrpg')
        elif shelf == '_Inbox':
            source = 'inbox'
        else:
            source = 'takeout'

        cur.execute(
            """INSERT INTO collections
               (id, shelf, category, folder, name, sort_name, family, publisher,
                genre, source, rel_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(rel_path) DO UPDATE SET shelf=excluded.shelf,
               category=excluded.category, folder=excluded.folder, name=excluded.name,
               sort_name=excluded.sort_name, family=excluded.family,
               publisher=excluded.publisher, genre=excluded.genre, source=excluded.source""",
            (next_id('collections'), shelf, category, folder, name, name.lower(), fam, pub, gen,
             source, rel),
        )
        cid = cur.execute('SELECT id FROM collections WHERE rel_path=?',(rel,)).fetchone()[0]
        cur.execute('INSERT INTO seen_collections VALUES (?)',(cid,))
        n_collections += 1
        n_bytes = 0
        names = []

        # A collection created for LOOSE files must not walk recursively: its
        # directory also contains the sibling collections, which would index
        # every file twice and trip the UNIQUE constraint on rel_path.
        if loose_only:
            file_paths = [os.path.join(path, f) for f in sorted(os.listdir(path))
                          if os.path.isfile(os.path.join(path, f))]
        else:
            file_paths = [os.path.join(dp, f)
                          for dp, _dns, fns in os.walk(path) for f in fns]

        for fpath in file_paths:
            fn = os.path.basename(fpath)
            try:
                st = os.stat(fpath)
            except OSError:
                continue
            ext = os.path.splitext(fn)[1].lower()
            kind = classify(fn, ext)
            cat, subcat = classify_category(kind, fn)
            if cat is None:
                untracked += 1
                continue
            frel = os.path.relpath(fpath, library)
            file_rows.append((cid, frel, fn, ext, kind, cat, subcat,
                              st.st_size, st.st_mtime))
            n_bytes += st.st_size
            n_files += 1
            if len(names) < 400:
                names.append(fn)

        cur.execute('UPDATE collections SET n_bytes=? WHERE id=?',
                    (n_bytes, cid))
        for t in sorted(tags):
            cur.execute('INSERT OR IGNORE INTO collection_tags VALUES (?,?)',
                        (cid, tag_id(t)))

        cur.execute(
            'INSERT INTO search(collection_id,name,shelf,tags,filenames) '
            'VALUES (?,?,?,?,?)',
            (cid, name, shelf, ' '.join(sorted(tags)), ' '.join(names)),
        )

    cur.executemany(
        'INSERT INTO files(id, collection_id, rel_path, filename, ext, kind, '
        'category, subcategory, size, mtime) VALUES (?,?,?,?,?,?,?,?,?,?) '
        'ON CONFLICT(rel_path) DO UPDATE SET collection_id=excluded.collection_id, '
        'filename=excluded.filename, ext=excluded.ext, kind=excluded.kind, '
        'category=excluded.category, subcategory=excluded.subcategory, '
        'size=excluded.size, mtime=excluded.mtime',
        ((next_id('files'), *r) for r in file_rows))

    cur.executemany('INSERT INTO seen_files VALUES (?)', ((r[1],) for r in file_rows))
    cur.execute('DELETE FROM files WHERE rel_path NOT IN (SELECT rel_path FROM seen_files)')
    cur.execute('DELETE FROM collections WHERE id NOT IN (SELECT id FROM seen_collections)')
    cur.execute("""UPDATE collections SET n_files = (
        SELECT COUNT(*) FROM files WHERE files.collection_id = collections.id)""")

    import classification
    classification.enrich(con)
    n_fts = rebuild_files_fts(con)
    con.commit()

    # summary
    print(f"collections : {n_collections}")
    print(f"tracked files: {n_files}")
    print(f"untracked    : {untracked}  (software / vtt-data / archives - not indexed)")
    print(f"tags        : {len(tag_ids)}")
    print(f"files index : {n_fts} rows in files_fts")
    print()
    print("files by category -> subcategory:")
    for cat, sub, cnt, b in cur.execute(
            "SELECT COALESCE(category,'-'), COALESCE(subcategory,'-'), "
            "COUNT(*), SUM(size) FROM files "
            "GROUP BY category, subcategory ORDER BY 1, 3 DESC"):
        print(f"  {cat:7s} {sub:12s} {cnt:6d}   {b/1e9:6.2f} GB")
    print()
    print("files by kind:")
    for kind, cnt, b in cur.execute(
            "SELECT kind, COUNT(*), SUM(size) FROM files GROUP BY kind ORDER BY 2 DESC"):
        print(f"  {kind:18s} {cnt:6d}   {b/1e9:6.2f} GB")
    print()
    print("collections by category/shelf:")
    for cat, shelf, cnt in cur.execute(
            "SELECT COALESCE(category,'-'), shelf, COUNT(*) FROM collections "
            "GROUP BY category, shelf ORDER BY 1, 2"):
        print(f"  {cat:7s} {shelf:32s} {cnt}")
    con.close()
    return 0


if __name__ == '__main__':
    from jobs import run
    sys.exit(run('catalog', lambda fd: main()))
