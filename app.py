#!/usr/bin/env python3
"""
LAN-only RPG library web app.

Browses the SQLite catalog built by catalog.py and streams files straight off
disk from /mnt/disk5/RPG-Library. No authentication: bind to the LAN interface
only (see run.sh).

Run:  ./.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8765
"""
import html as html_lib
import logging
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import quote, urlparse, urlencode
from contextlib import contextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

try:
    import pymupdf            # thumbnails for the multi-hundred-MB map scans
except ImportError:           # pragma: no cover - the app runs fine without it
    pymupdf = None

BASE = os.path.dirname(os.path.abspath(__file__))
LIBRARY = os.environ.get('RPG_LIBRARY', '/mnt/disk5/RPG-Library')
DB = os.environ.get('RPG_DB', os.path.join(BASE, 'library.db'))
CATALOG_PY = os.path.join(BASE, 'catalog.py')


def _env_ms(name: str, default: float) -> float:
    """A millisecond budget from the environment; junk falls back to `default`."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# One stderr line for any request slower than this; 0 turns the log off.
# Without it the slow paths are invisible until a page visibly hangs: a content
# total over 300k indexed pages was once measured at 16 s on the host's disk.
SLOW_LOG_MS = _env_ms('RPG_SLOW_LOG_MS', 250.0)
slow_log = logging.getLogger('rpg.slow')
slow_log.setLevel(logging.INFO)
if not slow_log.handlers:
    # uvicorn configures only its own loggers, so the root logger has no
    # handler and an INFO record would vanish. Give this logger its own:
    # stderr goes to the journal under systemd, and nothing needs configuring.
    _slow_handler = logging.StreamHandler(sys.stderr)
    _slow_handler.setFormatter(logging.Formatter('%(message)s'))
    slow_log.addHandler(_slow_handler)
    slow_log.propagate = False

# Cached downscaled images. The library holds map scans up to 184 MB and
# 20064x13082 pixels; decoding one of those in the browser takes seconds and
# hundreds of MB of RAM, so both the hover peek and the popup use a thumbnail.
THUMBS_DIR = os.environ.get('RPG_THUMBS', os.path.join(BASE, 'thumbnails'))
# 160 for list rows, 320 for the hover snapshot, 1600 inside the popup
THUMB_SIZES = (160, 320, 1600)
# Shelf covers: page 1 of each collection's core book, rendered once and cached.
COVERS_DIR = os.environ.get('RPG_COVERS', os.path.join(BASE, 'covers'))
COVER_WIDTH = 340
CACHE_HEADERS = {'Cache-Control': 'public, max-age=604800'}
# One 184 MB scan decodes to roughly 800 MB of RAM, so only a couple of
# thumbnails may be built at once -- a burst of hovers must not exhaust memory.
_THUMB_SLOTS = threading.BoundedSemaphore(2)
@contextmanager
def _thumbnail_slot():
    if not _THUMB_SLOTS.acquire(blocking=False):
        raise HTTPException(503,'Preview queued; retry shortly',headers={'Retry-After':'3'})
    try: yield
    finally: _THUMB_SLOTS.release()

SHELF_LABELS = {
    '09-Fantasy-and-Adventure': 'Fantasy & Adventure',
    '01-DnD-and-d20': 'D&D & d20 Fantasy',
    '02-OSR-and-Old-School': 'OSR & Old-School',
    '03-Indie-and-Story': 'Indie & Story Games',
    '04-SciFi-and-Cyberpunk': 'Sci-Fi & Cyberpunk',
    '05-Horror-and-Modern': 'Horror & Modern',
    '06-Licensed-IP': 'Licensed IP',
    '07-Universal-Systems': 'Universal Systems',
    '08-Warhammer-and-Grimdark': 'Warhammer & Grimdark',
    '09-VTT-and-Software': 'VTT & Software',
    '10-Maps-and-Accessories': 'Maps & Accessories',
    '11-DriveThruRPG': 'DriveThruRPG Library',
    '_Unsorted': 'Unsorted',
    '_Inbox': 'Inbox',
}

KIND_ORDER = ['rulebook', 'document', 'ebook', 'character-sheet', 'handout',
              'map', 'token', 'art', 'audio', 'video', 'vtt-data', 'software',
              'data', 'archive', 'other']

# two-category model: books (with a 'sheets' group) and assets
CATEGORY_ORDER = ['book', 'asset']
SUBCATEGORY = {
    'book': ['core-rules', 'expansion', 'campaign', 'sheets'],
    'asset': ['maps', 'art', 'audio', 'video', 'project'],
}
CATEGORY_LABEL = {'book': '📖 Books', 'asset': '🎨 Assets'}
SUBCATEGORY_LABEL = {
    'core-rules': 'Core Rules', 'expansion': 'Expansions',
    'campaign': 'Campaigns & Modules', 'sheets': 'Sheets & Handouts',
    'maps': 'Maps', 'art': 'Art & Tokens', 'audio': 'Audio', 'video': 'Video',
    'project': 'Tool Projects',
}

# order the shelves appear on the wall, top to bottom
SHELF_ORDER = ['01-DnD-and-d20', '02-OSR-and-Old-School', '03-Indie-and-Story',
               '04-SciFi-and-Cyberpunk', '05-Horror-and-Modern', '06-Licensed-IP',
               '07-Universal-Systems', '08-Warhammer-and-Grimdark', '09-Fantasy-and-Adventure',
               'Assets', '_Inbox', '_Unsorted']

INLINE_KINDS = {'document', 'rulebook', 'ebook', 'character-sheet', 'handout',
                'map', 'art', 'token', 'audio', 'video'}

# What kind of in-page viewer a file gets, keyed on extension. Single source of
# truth: the /view route and the action buttons in the file lists both read it,
# so a file can never be previewable in one place and not the other.
MEDIA_BY_EXT = {
    '.pdf': 'pdf',
    '.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image',
    '.tif': 'image', '.tiff': 'image', '.gif': 'image', '.svg': 'image', '.bmp': 'image', '.avif': 'image',
    '.mp3': 'audio', '.m4a': 'audio', '.wav': 'audio', '.ogg': 'audio',
    '.oga': 'audio', '.flac': 'audio', '.opus': 'audio',
    '.mp4': 'video', '.webm': 'video', '.m4v': 'video', '.ogv': 'video',
    '.txt': 'text', '.md': 'text', '.html': 'text', '.htm': 'text',
    '.json': 'text', '.xml': 'text', '.css': 'text', '.js': 'text',
    '.csv': 'text',
}

# media types that open in the modal rather than a new tab
MODAL_MEDIA = ('image', 'audio', 'video')

# Small icons for file types that have no thumbnail. Deliberately plain
# symbols rather than emoji: the emoji set is patchy across the fonts on a
# Linux desktop, and U+1F5BC (framed picture) rendered as a tofu box here.
MEDIA_ICON = {'pdf': '▤', 'image': '▣', 'audio': '♪', 'video': '▶',
              'text': '≡', 'none': '•'}


def media_of(ext: str) -> str:
    """'pdf' | 'image' | 'audio' | 'video' | 'text' | 'none'."""
    return MEDIA_BY_EXT.get((ext or '').lower(), 'none')


def human_size(n) -> str:
    """Byte count in the units the lists show (decimal, like '1.2 MB').

    Without this, anything under 50 KB renders as '0.0 MB' -- which is worse
    than useless for the many small VTT tokens and handouts in the library.
    """
    n = float(n or 0)
    if n >= 1_000_000_000:
        return f'{n / 1e9:.2f} GB'
    if n >= 1_000_000:
        return f'{n / 1e6:.1f} MB'
    if n >= 1_000:
        return f'{n / 1e3:.0f} KB'
    return f'{n:.0f} B'


def _asset_version() -> str:
    """Newest mtime of the static assets, as a cache-busting token.

    Static files change far more often than app.py, and the browser happily
    keeps the old style.css across a restart -- which silently hides new CSS.
    """
    stamps = []
    for name in ('style.css', 'media.js', 'pdfviewer.js', 'theme.css', 'library.js'):
        try:
            stamps.append(int(os.path.getmtime(os.path.join(BASE, 'static', name))))
        except OSError:
            pass
    return str(max(stamps) if stamps else 0)


def _cover_version() -> str:
    """Cache-buster for cover URLs, derived from the covers directory.

    The URL /cover/{id} is stable but its content is not: regenerating the
    whole shelf (after a change to the page picker) rewrites every file under
    the same names, and the browser would keep showing the old artwork for a
    week. The directory mtime moves whenever covers are added or replaced.
    Call this per render rather than caching it at import.
    """
    try:
        return str(int(os.path.getmtime(COVERS_DIR)))
    except OSError:
        return '0'

app = FastAPI(title='The Great Library')

@app.middleware('http')
async def protect_changes(request, call_next):
    if request.method in ('POST','PUT','PATCH','DELETE'):
        origin=request.headers.get('origin')
        if origin and urlparse(origin).netloc != request.headers.get('host'):
            return JSONResponse({'error':'Cross-origin changes are not allowed.'},status_code=403)
        if request.headers.get('x-library-request') != '1':
            return JSONResponse({'error':'Use the library interface to make changes.'},status_code=403)
        if request.url.path.startswith(('/api/inbox','/api/rescan','/api/dtrpg/sync','/api/index/build','/api/restart','/api/metadata','/api/book-metadata')):
            import secrets
            token=os.environ.get('RPG_ADMIN_TOKEN','')
            local=request.client and request.client.host in ('127.0.0.1','::1','testclient')
            if not local and not (token and secrets.compare_digest(request.headers.get('x-library-admin',''),token)):
                return JSONResponse({'error':'Maintenance is available on the library host. Remote administration requires RPG_ADMIN_TOKEN.'},status_code=403)
    return await call_next(request)


@app.middleware('http')
async def log_slow_requests(request, call_next):
    """One line per request slower than RPG_SLOW_LOG_MS, with its section.

    Added after `protect_changes`, so it is the outer middleware and its timing
    includes the mutation guard. A streamed body (a file or a thumbnail) is
    measured up to its headers only: FastAPI hands back the response before the
    bytes are written.
    """
    started = time.perf_counter()
    status = 500          # an exception leaves no response to read
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        if SLOW_LOG_MS > 0:
            elapsed = (time.perf_counter() - started) * 1000
            if elapsed >= SLOW_LOG_MS:
                slow_log.info('slow %s %s section=%s %.0fms status=%d',
                              request.method, request.url.path,
                              request.query_params.get('section') or '-',
                              elapsed, status)


templates = Jinja2Templates(directory=os.path.join(BASE, 'templates'))
app.mount('/static', StaticFiles(directory=os.path.join(BASE, 'static')),
          name='static')

# templates ask media() which button to render for a file, and modal_media to
# know which types open in the popup rather than a new tab
templates.env.globals['media'] = media_of
templates.env.globals['modal_media'] = MODAL_MEDIA
templates.env.globals['media_icon'] = lambda e: MEDIA_ICON.get(media_of(e), '📦')
templates.env.globals['INLINE_KINDS'] = INLINE_KINDS
templates.env.globals['asset_v'] = _asset_version
# a function, not a value: covers are regenerated while the app is running
templates.env.globals['cover_v'] = _cover_version
templates.env.filters['filesize'] = human_size


# SQLite starts every connection with a 2 MB page cache, no memory map and temp
# tables on disk. That is invisible on a small database and painful on this one:
# the catalogue is 3.66 GB, so a cold connection re-read the same hot pages for
# every request. These pragmas are applied on every connection instead, and can
# be changed from the environment (see README).
_TEMP_STORE = {'DEFAULT': 0, 'FILE': 1, 'MEMORY': 2}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """A non-negative integer from the environment, or the default if unusable."""
    try:
        value = int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def sqlite_settings() -> dict:
    """The pragmas every catalogue connection gets, as {pragma: value}.

    Read per connection rather than at import so a test or a restart can change
    them without re-importing the module. Values are coerced to integers and
    temp_store is a fixed vocabulary, so an environment typo cannot inject SQL.
    """
    temp = str(os.environ.get('RPG_SQLITE_TEMP_STORE', 'MEMORY')).strip().upper()
    return {
        # negative cache_size is KiB rather than pages: -65536 is 64 MB
        'cache_size': -_env_int('RPG_SQLITE_CACHE_MB', 64, 1) * 1024,
        'mmap_size': _env_int('RPG_SQLITE_MMAP_MB', 256) * 1024 * 1024,
        'temp_store': _TEMP_STORE.get(temp, _TEMP_STORE['MEMORY']),
        'busy_timeout': _env_int('RPG_SQLITE_BUSY_TIMEOUT_MS', 5000),
    }


def _connect(path: str, write: bool) -> sqlite3.Connection:
    """Open the catalogue file, read-only unless the caller needs to change it."""
    if not write and os.path.exists(path):
        uri = 'file:' + quote(os.path.abspath(path)) + '?mode=ro'
        try:
            return sqlite3.connect(uri, uri=True)
        except sqlite3.OperationalError as exc:
            # A WAL catalogue whose -shm is gone (an interrupted writer) cannot
            # be opened read-only. Serving pages from a read-write handle beats
            # failing every request until someone notices.
            print(f'library: {path} is not readable read-only ({exc}); '
                  'falling back to a read-write connection', file=sys.stderr)
    # write=True, or no catalogue on disk yet: opening read-write keeps the app
    # starting so the pages render (empty) instead of failing at connect time.
    return sqlite3.connect(path)


def db(write: bool = False) -> sqlite3.Connection:
    """A catalogue connection carrying the tuned pragmas.

    Reads use a read-only handle, so a GET that tries to mutate the catalogue
    fails with 'attempt to write a readonly database' instead of quietly
    splitting a write across connections. Only routes that change the catalogue
    pass write=True.
    """
    con = _connect(DB, write)
    con.row_factory = sqlite3.Row
    for name, value in sqlite_settings().items():
        con.execute(f'PRAGMA {name}={int(value)}')
    return con


def shelf_label(shelf: str) -> str:
    return SHELF_LABELS.get(shelf, shelf)


def fts_query(q: str) -> str:
    """Turn user input into a safe FTS5 prefix query."""
    terms = re.findall(r"[\w'-]+", q, flags=re.UNICODE)
    # drop FTS operators that would be interpreted as syntax
    terms = [t for t in terms if t.upper() not in ('AND', 'OR', 'NOT', 'NEAR')]
    return ' '.join(f'"{t}"*' for t in terms)


# A prefix match on one or two characters matches most of the library, so the
# files section keeps its substring scan for very short queries only.
FTS_MIN_CHARS = 3


def has_files_index(con) -> bool:
    """Whether catalog.py has built files_fts yet."""
    try:
        return bool(con.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'files_fts'").fetchone())
    except sqlite3.OperationalError:      # pragma: no cover - closed database
        return False


def files_filter(filters: str, params: list, q: str, match: str,
                 use_fts: bool):
    """WHERE clause for the "Books & assets" section of /search.

    Longer queries use files_fts: LIKE '%q%' cannot use an index, so it read
    every row of files and book_classification on each search. Short queries
    keep the substring scan, and callers fall back to it as well when
    files_fts has not been built yet.
    """
    clause, args = filters, list(params)
    if q:
        if use_fts:
            clause += (' AND f.id IN (SELECT CAST(id AS INTEGER) FROM files_fts'
                       ' WHERE files_fts MATCH ?)')
            args.append(match)
        else:
            clause += (' AND (f.filename LIKE ? OR EXISTS(SELECT 1 FROM'
                       ' book_classification b WHERE b.rel_path=f.rel_path AND'
                       ' (b.system LIKE ? OR b.series LIKE ? OR b.campaign LIKE ?)))')
            args += ['%' + q + '%'] * 4
    return clause, args


def highlight(snippet: str) -> str:
    """Escape a snippet, then turn the << >> markers into <mark> tags."""
    escaped = html_lib.escape(snippet or '')
    return escaped.replace('&lt;&lt;', '<mark>').replace('&gt;&gt;', '</mark>')


def content_search(con, q: str, limit: int = 60):
    """Search inside indexed PDF text. Returns [] if the index is absent."""
    if not q:
        return []
    try:
        return con.execute(
            """SELECT f.id AS fid, pp.page AS page, f.filename AS filename,
                      f.collection_id AS cid, c.name AS collection_name,
                      c.shelf AS shelf,
                      snippet(pdf_fts, 0, '<<', '>>', ' … ', 14) AS snip
               FROM pdf_fts
               JOIN pdf_pages pp ON pp.id = pdf_fts.rowid
               JOIN files f ON f.rel_path = pp.rel_path
               JOIN collections c ON c.id = f.collection_id
               WHERE pdf_fts MATCH ?
               ORDER BY rank LIMIT ?""", (fts_query(q), limit)).fetchall()
    except sqlite3.OperationalError:
        return []


@app.get('/shelf', response_class=HTMLResponse)
def shelf_view(request: Request, q: str = '', cat: str = '', wing: str = ''):
    """The library as a wall of bookshelves.

    One book per collection, represented by its core book's first page. This is
    a genuine browse surface, not a picture: every book links to its collection,
    where the full file list and per-file actions live.
    """
    con = db()
    where, params = ['c.n_files > 0'], []
    if q:
        where.append('(c.name LIKE ? OR c.publisher LIKE ? OR c.family LIKE ?)')
        params += [f'%{q}%'] * 3
    if wing:
        where.append('c.shelf = ?'); params.append(wing)
    if cat:
        where.append('EXISTS(SELECT 1 FROM files f WHERE f.collection_id=c.id AND f.category=?)')
        params.append(cat)
    sql = ('SELECT c.id, c.name, c.shelf, c.category, c.n_files, c.n_bytes,'
           ' c.family, c.publisher FROM collections c')
    if where:
        sql += ' WHERE ' + ' AND '.join(where)
    sql += ' ORDER BY c.name'
    rows = con.execute(sql, params).fetchall()
    stats = con.execute(
        'SELECT COUNT(*) n, COALESCE(SUM(n_files), 0) f,'
        ' COALESCE(SUM(n_bytes), 0) b FROM collections').fetchone()
    con.close()

    groups = {}
    for r in rows:
        groups.setdefault(r['shelf'], []).append(r)

    def order(s):
        return (SHELF_ORDER.index(s) if s in SHELF_ORDER else len(SHELF_ORDER), s)

    shelves = [{'shelf': s, 'label': shelf_label(s), 'items': groups[s]}
               for s in sorted(groups, key=order)]

    return templates.TemplateResponse(request, 'shelf.html', {
        'q': q, 'cat': cat, 'shelves': shelves, 'stats': stats,
        'cats': CATEGORY_ORDER, 'category_label': CATEGORY_LABEL,
        'wing':wing, 'shelf_label':shelf_label, 'is_home':request.url.path=='/',
    })


@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    """The shelf is the front door; the old dashboard lives at /browse."""
    return shelf_view(request)


@app.get('/browse', response_class=HTMLResponse)
def index(request: Request):
    con = db()
    shelves = con.execute(
        """SELECT shelf, COUNT(*) n_collections, SUM(n_files) n_files,
                  SUM(n_bytes) n_bytes
           FROM collections GROUP BY shelf ORDER BY shelf""").fetchall()
    stats = con.execute(
        """SELECT COUNT(*) collections FROM collections""").fetchone()
    nfiles = con.execute("SELECT COUNT(*) c FROM files").fetchone()['c']
    nbytes = con.execute("SELECT COALESCE(SUM(size),0) s FROM files").fetchone()['s']
    kinds = con.execute(
        """SELECT kind, COUNT(*) c FROM files GROUP BY kind
           ORDER BY c DESC""").fetchall()
    cats = con.execute(
        """SELECT COALESCE(category,'-') cat, COALESCE(subcategory,'-') sub,
                  COUNT(*) n, COALESCE(SUM(size),0) b
           FROM files GROUP BY cat, sub ORDER BY cat, n DESC""").fetchall()
    cat_tree = {}
    for r in cats:
        cat_tree.setdefault(r['cat'], []).append(r)
    recent = con.execute(
        """SELECT id, name, shelf, n_files FROM collections
           ORDER BY id DESC LIMIT 12""").fetchall()
    con.close()
    return templates.TemplateResponse(request, 'index.html', {
        'shelves': shelves, 'shelf_label': shelf_label,
        'n_collections': stats['collections'], 'n_files': nfiles,
        'n_bytes': nbytes, 'kinds': kinds, 'recent': recent,
        'cat_tree': cat_tree, 'cats': CATEGORY_ORDER,
        'category_label': CATEGORY_LABEL,
        'subcategory_label': SUBCATEGORY_LABEL,
    })


# Search totals are COUNT(*) scans, not page data: the content total walks the
# whole full-text index (16 s on the live HDD catalogue) and the files total is
# a LIKE over every row (2.8 s). Only the tab being rendered needs a number, so
# the count is computed for that tab alone and remembered for a minute -- long
# enough to make tab-switching free, short enough that a rescan shows up.
SEARCH_TOTAL_TTL = 60.0
SEARCH_TOTAL_CACHE_MAX = 128
_search_totals = {}
_search_totals_lock = threading.Lock()
# Bumped by every clear_search_totals(). A count runs outside the lock, so it
# can straddle a rescan; the generation tells it whether the values it saw are
# still the ones the caller asked about.
_search_totals_generation = 0


def search_total(key, compute):
    """Return a cached count, recomputing at most once per TTL window."""
    now = time.monotonic()
    with _search_totals_lock:
        hit = _search_totals.get(key)
        if hit and hit[0] > now:
            return hit[1]
        generation = _search_totals_generation
    value = compute()
    if value is None:
        # The count could not be answered -- the database was busy or being
        # rebuilt. Return the unknown, never a zero and never a remembered
        # value: one hiccup would otherwise read "0 results" for the rest of
        # the TTL, long after the catalogue was healthy again.
        return None
    # The window starts when the count *finishes*, not when it was asked for:
    # the content total takes 16 s on the live HDD catalogue, and timing it from
    # the start would quietly shorten the advertised minute to 44 s for exactly
    # the total this cache exists to avoid recomputing.
    stored = time.monotonic()
    with _search_totals_lock:
        # A rescan that finished while this count was running has already
        # forgotten what it replaced. Storing the value would undo that clear
        # and serve a pre-rescan total for the rest of the TTL, so this request
        # renders what it counted and leaves the cache alone.
        if generation != _search_totals_generation:
            return value
        if len(_search_totals) >= SEARCH_TOTAL_CACHE_MAX:
            for stale in [k for k, v in _search_totals.items() if v[0] <= stored]:
                del _search_totals[stale]
            while len(_search_totals) >= SEARCH_TOTAL_CACHE_MAX:
                _search_totals.pop(next(iter(_search_totals)))
        _search_totals[key] = (stored + SEARCH_TOTAL_TTL, value)
    return value


def clear_search_totals():
    """Forget every cached total; call it after the catalogue changes."""
    global _search_totals_generation
    with _search_totals_lock:
        _search_totals.clear()
        _search_totals_generation += 1


def count_rows(con, sql, args=()):
    """COUNT(*), or None when the database could not answer.

    A missing table or column is the documented "index has not been built yet"
    case and is a real zero. Anything else -- database is locked, disk I/O -- is
    a transient failure, and saying 0 would report an empty library. Returning
    None lets the caller leave the total unstated and remember nothing, so the
    next request asks again.
    """
    try:
        return con.execute(sql, args).fetchone()[0]
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc) or 'no such column' in str(exc):
            return 0
        return None


def has_pdf_index(con) -> bool:
    """True when both halves of the external-content PDF index are present.

    `pdf_fts` is external content over `pdf_pages`; an old-style database still
    has a `pdf_fts` but no content table, and is treated as not indexed until
    `index_pdf_text.py --migrate` has run.
    """
    try:
        names = {row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('pdf_fts', 'pdf_pages')")}
        return {'pdf_fts', 'pdf_pages'} <= names
    except sqlite3.OperationalError:      # pragma: no cover - closed database
        return False


@app.get('/search', response_class=HTMLResponse)
def search(request: Request, q: str = '', shelf: str = '', kind: str = '',
           tag: str = '', cat: str = '', subcat: str = '',
           page: int = 1, section: str = 'collections', sort: str = 'title'):
    section = section if section in ('collections','files','content','campaigns') else 'collections'
    sort = sort if sort in ('title','size','relevance') else 'title'
    page = max(1,page); size=36
    con=db()
    clauses=['1=1']; params=[]
    for column,value in [('f.category',cat),('f.subcategory',subcat),('f.kind',kind),('c.shelf',shelf)]:
        if value:
            clauses.append(column+' = ?'); params.append(value)
    if tag:
        clauses.append('c.id IN (SELECT ct.collection_id FROM collection_tags ct JOIN tags t ON t.id=ct.tag_id WHERE t.name=?)')
        params.append(tag)
    filters=' AND '.join(clauses)
    match=fts_query(q)
    cw='EXISTS(SELECT 1 FROM files f WHERE f.collection_id=c.id AND '+filters+')'
    cp=list(params)
    fw=filters; fp=list(params)
    use_fts=bool(match) and len(q.strip())>=FTS_MIN_CHARS and has_files_index(con)
    if q:
        cw+=' AND (c.name LIKE ? OR c.publisher LIKE ? OR c.family LIKE ?'
        cp += ['%'+q+'%']*3
        if match:
            cw+=' OR c.id IN (SELECT collection_id FROM search WHERE search MATCH ?)'; cp.append(match)
        cw+=')'
        fw,fp=files_filter(filters,params,q,match,use_fts)
    join=' FROM files f JOIN collections c ON c.id=f.collection_id WHERE '
    pw='pdf_fts MATCH ? AND '+filters; pp=[match]+params
    pj=' FROM pdf_fts JOIN pdf_pages pp ON pp.id=pdf_fts.rowid JOIN files f ON f.rel_path=pp.rel_path JOIN collections c ON c.id=f.collection_id WHERE '
    index_available=has_pdf_index(con)
    # Only the rendered tab needs a total; the other three cost seconds and are
    # never displayed (the template omits their badges).
    key=(DB,section,q,cat,subcat,kind,shelf,tag)
    totals={}; campaign_items=[]
    if section=='campaigns':
        import campaigns
        items=campaigns.catalog(con)
        eligible={r[0] for r in con.execute('SELECT c.id FROM collections c WHERE EXISTS(SELECT 1 FROM files f WHERE f.collection_id=c.id AND '+filters+')',params)}
        campaign_items=[c for c in items if (not q or q.casefold() in (c['name']+' '+c['edition']).casefold()) and any(i in eligible for i in c['collections'])]
        totals['campaigns']=len(campaign_items)
    elif section=='collections':
        totals['collections']=search_total(key,lambda: count_rows(con,'SELECT COUNT(*) FROM collections c WHERE '+cw,cp))
    elif section=='files':
        totals['files']=search_total(key,lambda: count_rows(con,'SELECT COUNT(*)'+join+fw,fp))
    elif match and index_available:
        totals['content']=search_total(key,lambda: count_rows(con,'SELECT COUNT(*)'+pj+pw,pp))
    else:
        totals['content']=0
    total=totals[section]
    if total is None:
        # The database would not answer this count. Deriving a page count from
        # it would state a false "0 results" beside a table of hits and rewind a
        # deep page to page 1, so say nothing about the total and leave the
        # requested page alone.
        pages=None
    else:
        pages=max(1,(total+size-1)//size); page=min(page,pages)
    offset=(page-1)*size
    collections=[]; files=[]; content_hits=[]
    if section=='collections':
        order='c.n_bytes DESC,c.name' if sort=='size' else 'c.name COLLATE NOCASE,c.id'
        collections=con.execute('SELECT c.* FROM collections c WHERE '+cw+' ORDER BY '+order+' LIMIT ? OFFSET ?',cp+[size,offset]).fetchall()
    elif section=='files':
        order='f.size DESC,f.id' if sort=='size' else 'f.filename COLLATE NOCASE,f.id'
        files=con.execute('SELECT f.*,c.name collection_name,c.shelf'+join+fw+' ORDER BY '+order+' LIMIT ? OFFSET ?',fp+[size,offset]).fetchall()
    elif match and index_available:
        content_hits=con.execute("SELECT f.id fid,pp.page,f.filename,c.id cid,c.name collection_name,c.shelf,snippet(pdf_fts,0,'<<','>>',' … ',24) snip"+pj+pw+' ORDER BY rank LIMIT ? OFFSET ?',pp+[size,offset]).fetchall()
    campaign_page=campaign_items[offset:offset+size]
    # With no total there is no page count to compare against, so a full page is
    # the only evidence available that another one exists.
    shown={'collections':len(collections),'files':len(files),
           'content':len(content_hits),'campaigns':len(campaign_page)}[section]
    next_page=shown>=size if pages is None else page<pages
    all_tags=con.execute('SELECT name FROM tags ORDER BY name').fetchall()
    shelves=con.execute('SELECT DISTINCT shelf FROM collections ORDER BY shelf').fetchall()
    con.close()
    def result_url(**changes):
        values=dict(q=q,shelf=shelf,kind=kind,tag=tag,cat=cat,subcat=subcat,section=section,sort=sort,page=page)
        values.update(changes)
        return '/search?'+urlencode({k:v for k,v in values.items() if v!=''})
    return templates.TemplateResponse(request,'search.html',dict(
        q=q,shelf=shelf,kind=kind,tag=tag,cat=cat,subcat=subcat,mode='query' if q else 'filter',
        campaigns=campaign_page,collections=collections,files=files,content_hits=content_hits,highlight=highlight,
        all_tags=all_tags,shelves=shelves,shelf_label=shelf_label,kinds=KIND_ORDER,
        cats=CATEGORY_ORDER,subcats=SUBCATEGORY,category_label=CATEGORY_LABEL,
        subcategory_label=SUBCATEGORY_LABEL,totals=totals,page=page,pages=pages,next_page=next_page,
        section=section,sort=sort,result_url=result_url,index_available=index_available))


@app.get('/collection/{cid}', response_class=HTMLResponse)
def collection(request: Request, cid: int):
    con = db()
    col = con.execute('SELECT * FROM collections WHERE id = ?', (cid,)).fetchone()
    if not col:
        con.close()
        raise HTTPException(404, 'Collection not found')
    from metadata import get
    metadata_choice=get(con,col['rel_path'])
    rows = con.execute(
        'SELECT * FROM files WHERE collection_id = ? ORDER BY kind, filename',
        (cid,)).fetchall()
    tags = [r['name'] for r in con.execute(
        """SELECT t.name FROM tags t JOIN collection_tags ct ON ct.tag_id = t.id
           WHERE ct.collection_id = ? ORDER BY t.name""", (cid,))]
    con.close()

    import campaigns
    campaign_con=db()
    try:campaign_items=[c for c in campaigns.catalog(campaign_con) if cid in c['collections']]
    finally:campaign_con.close()
    import classification
    detail_con=db()
    try:classified_groups=classification.grouped(detail_con,related_collection=cid)
    finally:detail_con.close()
    grouped = {}
    for r in rows:
        grouped.setdefault(r['subcategory'] or r['kind'], []).append(r)
    order=['core-rules','expansion','campaign','sheets','maps','art','audio','video','project']
    ordered = [(k, grouped[k]) for k in order if k in grouped]
    for k in grouped:
        if k not in order:
            ordered.append((k, grouped[k]))

    return templates.TemplateResponse(request, 'collection.html', {
        'classified_groups':classified_groups, 'campaigns':campaign_items, 'shelf_options':SHELF_LABELS, 'col': col, 'groups': ordered, 'tags': tags, 'all_files':rows, 'metadata_choice':metadata_choice,
        'inline_kinds': INLINE_KINDS, 'shelf_label': shelf_label,
        'group_labels':SUBCATEGORY_LABEL,
        'primary':next((r for r in sorted(rows,key=lambda r:('core' not in r['filename'].lower(),r['subcategory']!='core-rules',-r['size'])) if r['ext']=='.pdf'),None),
    })


@app.get('/raw/{fid}')
def raw(fid: int, request: Request, download: int = 0):
    con = db()
    f = con.execute('SELECT * FROM files WHERE id = ?', (fid,)).fetchone()
    con.close()
    if not f:
        raise HTTPException(404, 'File not found')

    path = os.path.realpath(os.path.join(LIBRARY, f['rel_path']))
    if not path.startswith(os.path.realpath(LIBRARY) + os.sep):
        raise HTTPException(403, 'Outside library')
    if not os.path.isfile(path):
        raise HTTPException(404, 'Missing on disk')

    ctype, _ = mimetypes.guess_type(path)
    ctype = ctype or 'application/octet-stream'
    if ctype.startswith('text/') or ctype in ('application/javascript',):
        ctype = 'text/plain; charset=utf-8'

    size = os.path.getsize(path)
    range_hdr = request.headers.get('range')
    disposition = 'attachment' if download else 'inline'
    headers = {
        'Accept-Ranges': 'bytes',
        'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "sandbox; default-src 'none'; style-src 'unsafe-inline'",
        'Content-Disposition': f"{disposition}; filename*=UTF-8\'\'{quote(os.path.basename(path))}",
    }

    if range_hdr:
        m = re.fullmatch(r'bytes=(\d*)-(\d*)', range_hdr)
        if m:
            if not m.group(1) and not m.group(2):
                raise HTTPException(416, 'Invalid range', headers={'Content-Range':f'bytes */{size}'})
            start = int(m.group(1)) if m.group(1) else max(0, size-int(m.group(2)))
            end = int(m.group(2)) if m.group(1) and m.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                raise HTTPException(416, 'Range not satisfiable', headers={'Content-Range':f'bytes */{size}'})
            length = end - start + 1

            def gen():
                with open(path, 'rb') as fh:
                    fh.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = fh.read(min(1 << 20, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        yield chunk

            headers['Content-Range'] = f'bytes {start}-{end}/{size}'
            headers['Content-Length'] = str(length)
            return StreamingResponse(gen(), status_code=206,
                                     media_type=ctype, headers=headers)

    headers['Content-Length'] = str(size)
    return FileResponse(path, media_type=ctype, headers=headers)


from concurrent.futures import ProcessPoolExecutor
import multiprocessing
from render_media import render
_RENDER_POOL = None
_RENDER_LOCK = threading.Lock()
def _build_thumbs(src, prefix, widths, page=None):
    global _RENDER_POOL
    with _RENDER_LOCK:
        if _RENDER_POOL is None:
            _RENDER_POOL = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context('spawn'))
    return _RENDER_POOL.submit(render, src, prefix, widths, page).result(timeout=180)

def _make_cover(src, base):
    return _build_thumbs(src, base, [COVER_WIDTH])[COVER_WIDTH]

@app.get('/thumb/{fid}')
def thumb(fid: int, w: int = 320):
    """Cached downscaled preview of an image, or of a PDF's cover page.

    `w` snaps to a known size to bound the size of the cache.
    """
    if pymupdf is None:
        raise HTTPException(503, 'PyMuPDF not available')
    width = min(THUMB_SIZES, key=lambda s: abs(s - w))

    con = db()
    f = con.execute('SELECT * FROM files WHERE id = ?', (fid,)).fetchone()
    con.close()
    if not f or media_of(f['ext']) not in ('image', 'pdf'):
        raise HTTPException(404, 'No thumbnail for this file type')

    path = os.path.realpath(os.path.join(LIBRARY, f['rel_path']))
    if not path.startswith(os.path.realpath(LIBRARY) + os.sep):
        raise HTTPException(403, 'Outside library')
    if not os.path.isfile(path):
        raise HTTPException(404, 'Missing on disk')

    os.makedirs(THUMBS_DIR, exist_ok=True)
    # id + mtime, so editing a file invalidates its thumbnail
    prefix = os.path.join(THUMBS_DIR,
                          f'{fid}-{int(os.path.getmtime(path))}')

    def cached():
        # an alpha source is cached as .png, an opaque one as .jpg
        for ext, ctype in (('jpg', 'image/jpeg'), ('png', 'image/png')):
            p = f'{prefix}-{width}.{ext}'
            if os.path.exists(p):
                return FileResponse(p, media_type=ctype, headers=CACHE_HEADERS)
        return None

    hit = cached()
    if hit:
        return hit

    with _thumbnail_slot():
        hit = cached()                  # another request may have just built it
        if hit:
            return hit
        # Build every size up to the largest we need. The decode dominates and
        # shrinking a decoded pixmap is nearly free, so a list row asking for
        # 160 should also leave the 320 hover snapshot behind. Without this, a
        # hover straight after a list pays the full decode again.
        build = [s for s in THUMB_SIZES if s <= max(width, 320)]
        try:
            made = _build_thumbs(path, prefix, build)
        except Exception:               # noqa: BLE001
            # PyMuPDF cannot decode every format we catalogue (webp, gif,
            # avif, svg). Fall back to the original: browsers render those
            # natively, and a heavy hover image beats a broken one.
            raise HTTPException(404, 'Preview unavailable for this file')
    out, ctype = made[width]
    return FileResponse(out, media_type=ctype, headers=CACHE_HEADERS)


# On a shelf a collection is represented by its core book. Prefer a rulebook,
# then an ebook, then a plain document. For an asset collection prefer an image
# instead, because a map pack's own artwork says more than any PDF inside it.
_COVER_RANK = ("CASE kind WHEN 'rulebook' THEN 0 WHEN 'ebook' THEN 1"
               " WHEN 'document' THEN 2 WHEN 'art' THEN 3"
               " WHEN 'token' THEN 4 WHEN 'map' THEN 5 ELSE 6 END")
_COVER_EXTS = "('.pdf','.jpg','.jpeg','.png','.webp')"


def _cover_candidates(con, cid: int, category: str, limit: int = 8):
    """Files that could represent a collection on a shelf, best first.

    A list rather than one row, because the best-looking candidate may still
    be unrenderable (PyMuPDF cannot read webp), and falling through to the next
    one beats showing nothing.
    """
    order = ("ORDER BY (ext = '.pdf') ASC, size DESC" if category == 'asset'
             else f"ORDER BY (lower(filename) LIKE '%corebook%' OR lower(filename) LIKE '%core rule%' OR lower(filename) LIKE '%core_book%') DESC, {_COVER_RANK}, size DESC")
    return con.execute(
        f'SELECT * FROM files WHERE collection_id = ? AND ext IN {_COVER_EXTS} '
        f'{order} LIMIT ?', (cid, limit)).fetchall()


@app.get('/cover/{cid}')
def cover(cid: int):
    """A shelf cover for a collection: its core book's first page."""
    if pymupdf is None:
        raise HTTPException(503, 'PyMuPDF not available')
    con = db()
    col = con.execute('SELECT * FROM collections WHERE id = ?',
                      (cid,)).fetchone()
    if not col:
        con.close()
        raise HTTPException(404, 'No such collection')
    from metadata import get
    choice=get(con,col['rel_path'])
    candidates = _cover_candidates(con, cid, col['category'])
    if choice.get('cover_file'):
        chosen=con.execute('SELECT * FROM files WHERE collection_id=? AND rel_path=?',(cid,choice['cover_file'])).fetchone()
        if chosen: candidates=[chosen]+[r for r in candidates if r['id']!=chosen['id']]
    con.close()
    if not candidates:
        # an empty collection, or one holding only unrenderable types; the
        # shelf template draws a blank spine rather than a broken image
        raise HTTPException(404, 'No cover for this collection')

    os.makedirs(COVERS_DIR, exist_ok=True)

    def cached(base):
        for ext, ctype in (('jpg', 'image/jpeg'), ('png', 'image/png')):
            p = f'{base}-{COVER_WIDTH}.{ext}'
            if os.path.exists(p):
                return FileResponse(p, media_type=ctype,
                                    headers=CACHE_HEADERS)
        return None

    for f in candidates:
        path = os.path.realpath(os.path.join(LIBRARY, f['rel_path']))
        if not path.startswith(os.path.realpath(LIBRARY) + os.sep):
            continue
        if not os.path.isfile(path):
            continue
        base = os.path.join(COVERS_DIR,
                            f"{cid}-{f['id']}-{int(os.path.getmtime(path))}" + (f"-p{choice.get('cover_page',1)}" if choice.get('cover_file')==f['rel_path'] else ''))
        hit = cached(base)
        if hit:
            return hit
        # a cover renders a whole PDF page, so it shares the memory budget
        # that keeps a burst of requests from decoding several big files
        with _thumbnail_slot():
            hit = cached(base)
            if hit:
                return hit
            try:
                out, ctype = (_build_thumbs(path, base, [COVER_WIDTH], choice.get('cover_page',1)-1)[COVER_WIDTH] if choice.get('cover_file')==f['rel_path'] else _make_cover(path, base))
            except Exception:           # noqa: BLE001
                continue                # unrenderable; try the next candidate
        return FileResponse(out, media_type=ctype, headers=CACHE_HEADERS)

    raise HTTPException(404, 'No renderable cover for this collection')


@app.get('/view/{fid}', response_class=HTMLResponse)
def view(request: Request, fid: int, page: int = 1):
    con = db()
    f = con.execute(
        """SELECT f.*, c.name collection_name, c.shelf, c.id cid
           FROM files f JOIN collections c ON c.id = f.collection_id
           WHERE f.id = ?""", (fid,)).fetchone()
    con.close()
    if not f:
        raise HTTPException(404, 'File not found')
    mode = media_of(f['ext'])
    return templates.TemplateResponse(request, 'view.html', {
        'f': f, 'mode': mode, 'shelf_label': shelf_label, 'page': max(1, page),
    })


@app.get('/api/collections')
def api_collections(shelf: str = '', q: str = ''):
    con = db()
    if q and not fts_query(q):
        con.close()
        return JSONResponse([])
    if q:
        rows = con.execute(
            """SELECT c.id, c.name, c.shelf, c.n_files, c.family, c.publisher
               FROM search s JOIN collections c ON c.id = s.collection_id
               WHERE search MATCH ? AND (?='' OR c.shelf=?) ORDER BY rank LIMIT 100""",
            (fts_query(q),shelf,shelf)).fetchall()
    else:
        sql = ("SELECT id, name, shelf, n_files, family, publisher FROM collections")
        params = []
        if shelf:
            sql += ' WHERE shelf = ?'
            params.append(shelf)
        sql += ' ORDER BY shelf, name'
        rows = con.execute(sql, params).fetchall()
    con.close()
    return JSONResponse([dict(r) for r in rows])


@app.get('/api/stats')
def api_stats():
    con = db()
    rows = con.execute(
        """SELECT kind, COUNT(*) n, SUM(size) bytes FROM files
           GROUP BY kind ORDER BY n DESC""").fetchall()
    con.close()
    return JSONResponse([dict(r) for r in rows])


@app.post('/api/rescan')
def api_rescan(moves: str = ''):
    """Rebuild the catalog on demand (e.g. after dropping files in _Inbox)."""
    import jobs
    if jobs.busy():
        return JSONResponse({'ok':False,'error':'Library maintenance is already running.'},status_code=409)
    global _rescan_running
    with _index_lock:
        if _index_state['running'] or _sync_state['running'] or external_index_running():
            return JSONResponse({
                'ok': False,
                'error': ('PDF indexing is running. Both jobs write to the same '
                          'database, so rebuild the catalog after indexing '
                          'finishes.'),
            }, status_code=409)
        if _rescan_running or _sync_state['running']:
            return JSONResponse({'ok': False, 'error': 'a rebuild is already running'},
                                status_code=409)
        _rescan_running = True
    try:
        cmd = catalog_command()
        if moves:
            return JSONResponse({'ok': False, 'error':'Use the configured provenance source.'}, status_code=400)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return JSONResponse({'ok': False, 'error': r.stderr[-2000:]},
                                status_code=500)
        clear_search_totals()
        return JSONResponse({'ok': True, 'output': r.stdout})
    finally:
        with _index_lock:
            _rescan_running = False


@app.get('/rescan')
def rescan_page():
    return RedirectResponse('/', status_code=303)


# ------------------------------------------------------------ dtrpg syncing ---

SYNC_LOG = os.path.join(BASE, 'dtrpg-sync.log')
TOKEN_FILE = os.path.expanduser(
    os.environ.get('DRPG_TOKEN_FILE', '~/.config/drpg/token'))
SYNC_SCRIPT = os.path.join(BASE, 'sync-dtrpg.sh')
RESCAN_SCRIPT = os.path.join(BASE, 'rescan.sh')
DEDUPE_SCRIPT = os.path.join(BASE, 'dedupe.sh')
MERGE_SCRIPT = os.path.join(BASE, 'merge-dtrpg.sh')
DTRPG_DIR = os.path.join(LIBRARY, '11-DriveThruRPG')

_sync_state = {'running': False, 'started': None, 'finished': None,
               'returncode': None, 'dry_run': None, 'rescan': None}
_sync_lock = threading.Lock()


def _read_log_tail(path: str, lines: int = 200) -> str:
    try:
        with open(path, 'r', errors='replace') as f:
            return ''.join(f.readlines()[-lines:])
    except OSError:
        return ''


def catalog_command():
    return [sys.executable, CATALOG_PY, '--library', LIBRARY, '--db', DB,
            '--provenance', os.environ.get('RPG_PROVENANCE', os.path.join(BASE,'provenance.json')),
            '--moves', os.environ.get('RPG_MOVES','/mnt/disk5/rpg-tools/shelf-moves.json')]

def _run_sync(dry_run, rescan, dedupe, merge):
    import jobs
    rc = 1
    def pipeline(fd):
        stages = [('download', [SYNC_SCRIPT] + (['--dry-run'] if dry_run else []))]
        if not dry_run:
            if dedupe: stages.append(('dedupe', [DEDUPE_SCRIPT]))
            if merge: stages.append(('merge', [MERGE_SCRIPT]))
            if rescan: stages.append(('catalog', catalog_command()))
        with open(SYNC_LOG, 'w') as log:
            for name, command in stages:
                jobs.record('sync', stage=name)
                log.write('Starting ' + name + '\n'); log.flush()
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                        cwd=BASE, **jobs.child_options(fd))
                log.write(name + ' exit=' + str(result.returncode) + '\n'); log.flush()
                if result.returncode:
                    jobs.record('sync', error=name + ' failed', stage=name)
                    return result.returncode
        return 0
    try:
        rc = jobs.run('sync', pipeline)
    except Exception as exc:
        with open(SYNC_LOG,'a') as log: log.write(str(exc)+'\n')
    finally:
        clear_search_totals()
        with _sync_lock:
            _sync_state.update(running=False, finished=time.time(), returncode=rc)


@app.post('/api/dtrpg/sync')
def api_dtrpg_sync(dry_run: int = 0, rescan: int = 1, dedupe: int = 1,
                   merge: int = 1):
    """Kick off a DriveThruRPG library sync in the background.

    On success this also de-duplicates (curated shelves win over the DTRPG
    download) and rebuilds the catalog, so new content is ready to browse.
    """
    import jobs
    if jobs.busy():
        return JSONResponse({'ok':False,'error':'Library maintenance is already running.'},status_code=409)
    with _sync_lock:
        if _sync_state['running']:
            return JSONResponse({'ok': False, 'error': 'a sync is already running'},
                                status_code=409)
        if _index_state['running'] or external_index_running():
            return JSONResponse({
                'ok': False,
                'error': ('PDF indexing is running. A sync ends by de-duplicating '
                          'and rebuilding the catalog, both of which hammer the '
                          'same disk. Wait for indexing to finish.'),
            }, status_code=409)
        if not os.path.exists(TOKEN_FILE):
            return JSONResponse({
                'ok': False,
                'error': ('No DriveThruRPG Library App Key found. Generate one at '
                          'https://www.drivethrurpg.com/en/account/settings '
                          f'(section "Library App Keys") and save it to {TOKEN_FILE} '
                          'with chmod 600.'),
            }, status_code=400)
        _sync_state.update(running=True, started=time.time(), finished=None,
                           returncode=None, dry_run=bool(dry_run),
                           rescan=bool(rescan))
    threading.Thread(target=_run_sync,
                     args=(bool(dry_run), bool(rescan), bool(dedupe), bool(merge)),
                     daemon=True).start()
    return JSONResponse({'ok': True, 'dry_run': bool(dry_run),
                         'rescan': bool(rescan), 'dedupe': bool(dedupe),
                         'merge': bool(merge)})


@app.get('/api/dtrpg/status')
def api_dtrpg_status(lines: int = 150):
    with _sync_lock:
        state = dict(_sync_state)
    import jobs
    state.update(jobs.read('sync'))
    if state.get('running') and not jobs.busy():state.update(running=False,error='Interrupted; safe to retry')
    state['tail'] = _read_log_tail(SYNC_LOG, min(max(lines,1),500))
    state['token_present'] = os.path.exists(TOKEN_FILE)
    state['token_file'] = TOKEN_FILE
    state['target'] = DTRPG_DIR
    state['downloaded_files'] = sum(
        len(f) for _d, _s, f in os.walk(DTRPG_DIR)) if os.path.isdir(DTRPG_DIR) else 0
    return JSONResponse(state)


def _restart_supervisor() -> str:
    """Work out what will bring us back if we exit.

    INVOCATION_ID is NOT reliable: it is set for anything inside any systemd
    unit or scope (including the editor's snap), so it over-reports systemd.
    The precise test is whether OUR pid is the rpg-library service's MainPID.
    """
    try:
        out = subprocess.run(
            ['systemctl', 'show', 'rpg-library', '-p', 'MainPID', '--value'],
            capture_output=True, text=True, timeout=3).stdout.strip()
        if out.isdigit() and int(out) == os.getpid():
            return 'systemd'
    except Exception:  # noqa: BLE001
        pass
    if os.path.exists(os.path.join(BASE, 'rpg-library.pid')):
        return 'manage.sh'
    return 'unknown'


@app.post('/api/restart')
def api_restart(confirm: int = 0):
    """Restart the server WITHOUT sudo.

    We do not restart ourselves -- we exit with a non-zero status and let the
    supervisor bring us back. systemd does this via Restart=on-failure, and
    manage.sh does it with a relaunch loop, so the same endpoint works either
    way and needs no privileges at all.
    """
    if not confirm:
        return JSONResponse({'ok': False, 'error': 'add ?confirm=1 to proceed'},
                            status_code=400)
    supervisor = _restart_supervisor()
    if supervisor == 'unknown':
        return JSONResponse({'ok':False,'error':'No restart supervisor detected.'},status_code=409)

    def _bounce():
        time.sleep(0.75)   # let the response reach the client first
        os._exit(1)        # non-zero so Restart=on-failure engages

    threading.Thread(target=_bounce, daemon=True).start()
    return JSONResponse({
        'ok': True,
        'supervisor': supervisor,
        'note': ('exiting now; the supervisor will bring it back in a moment'
                 if supervisor != 'unknown' else
                 'exiting now - no supervisor detected, so restart it with '
                 './manage.sh start'),
    })


@app.get('/dtrpg', response_class=HTMLResponse)
def dtrpg_page(request: Request):
    return templates.TemplateResponse(request, 'dtrpg.html', {
        'token_present': os.path.exists(TOKEN_FILE),
        'token_file': TOKEN_FILE,
        'target': DTRPG_DIR,
        'index_log': INDEX_LOG,
    })


# --------------------------------------------------------- pdf text index ---

INDEX_LOG = os.path.join(BASE, 'index-pdf.log')
INDEX_SCRIPT = os.path.join(BASE, 'index_pdf_text.py')

_index_state = {'running': False, 'started': None, 'finished': None,
                'returncode': None, 'rebuild': None}
_index_lock = threading.Lock()
# set while a catalog rebuild request is in flight (see api_rescan)
_rescan_running = False


def external_index_running() -> bool:
    """True if an index_pdf_text.py process is running, whoever started it.

    The in-process flag only covers jobs launched from the web UI; an indexer
    started from a terminal would otherwise be reported as 'idle'.
    """
    try:
        pids = os.listdir('/proc')
    except OSError:
        return False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as fh:
                cmd = fh.read().replace(b'\x00', b' ').decode('utf-8', 'replace')
        except OSError:
            continue
        if 'index_pdf_text.py' in cmd:
            return True
    return False


def _run_index(rebuild: bool, workers: int = 3) -> None:
    rc = -1
    try:
        # Keep the indexer from starving the web app: the library is on a
        # single spinning disk, so give the indexing reads low I/O priority.
        import shutil
        launcher = []
        if shutil.which('ionice'):
            # best-effort, lowest priority: yields to interactive I/O but still
            # gets scheduled. NOTE: idle class (-c3) is starved on mq-deadline,
            # which made indexing ~25x slower.
            launcher += ['ionice', '-c2', '-n7']
        if shutil.which('nice'):
            launcher += ['nice', '-n', '19']
        args = launcher + [
            sys.executable, INDEX_SCRIPT, '--library', LIBRARY, '--db', DB,
            '--workers', str(max(1, min(workers, 8))),
        ] + (['--rebuild'] if rebuild else [])
        with open(INDEX_LOG, 'a') as log:
            log.write(f"\n$ {' '.join(args)}\n")
            log.flush()
            proc = subprocess.Popen(args, stdout=log,
                                    stderr=subprocess.STDOUT, cwd=BASE)
            rc = proc.wait()
            log.write(f"\nindex exit={rc}\n")
    except Exception as exc:  # noqa: BLE001
        with open(INDEX_LOG, 'a') as log:
            log.write(f'\nERROR launching indexer: {exc}\n')
        rc = -1
    finally:
        clear_search_totals()
        with _index_lock:
            _index_state.update(running=False, finished=time.time(),
                                returncode=rc)


@app.post('/api/index/build')
def api_index_build(rebuild: int = 0, workers: int = 3):
    """Extract PDF text into the search index, in the background."""
    with _index_lock:
        if _index_state['running'] or external_index_running():
            return JSONResponse({'ok': False, 'error': 'indexing already running'},
                                status_code=409)
        if _rescan_running or _sync_state['running']:
            return JSONResponse({
                'ok': False,
                'error': 'a catalog rebuild is running; try again shortly'},
                status_code=409)
        _index_state.update(running=True, started=time.time(), finished=None,
                            returncode=None, rebuild=bool(rebuild))
    threading.Thread(target=_run_index, args=(bool(rebuild), workers),
                     daemon=True).start()
    return JSONResponse({'ok': True, 'rebuild': bool(rebuild), 'workers': workers})


@app.get('/api/index/status')
def api_index_status(lines: int = 60):
    with _index_lock:
        state = dict(_index_state)
    import jobs
    state.update(jobs.read('index'))
    if state.get('running') and not jobs.busy():state.update(running=False,error='Interrupted; safe to retry')
    ext = external_index_running()
    state['external'] = ext
    state['running'] = state['running'] or ext
    state['tail'] = _read_log_tail(INDEX_LOG, lines)
    try:
        con = db()
        try:
            row = con.execute(
                'SELECT COUNT(*) n, COALESCE(SUM(pages),0) p, '
                'COALESCE(SUM(chars),0) c FROM pdf_status').fetchone()
            state['indexed_files'] = row['n']
            state['indexed_pages'] = row['p']
            state['indexed_chars'] = row['c']
            state['total_pdfs'] = con.execute(
                "SELECT COUNT(*) c FROM files WHERE ext = '.pdf'").fetchone()['c']
        finally:
            con.close()
    except sqlite3.OperationalError:
        # no catalogue, or one that has never been indexed
        state.update(indexed_files=0, indexed_pages=0, indexed_chars=0,
                     total_pdfs=0)
    return JSONResponse(state)

@app.get('/desk', response_class=HTMLResponse)
def reading_desk(request: Request):
    return templates.TemplateResponse(request,'desk.html',{})

@app.get('/api/resolve')
def resolve_file(path: str):
    con=db()
    row=con.execute('SELECT id FROM files WHERE rel_path=?',(path,)).fetchone()
    con.close()
    if not row: raise HTTPException(404,'This file is no longer cataloged')
    return dict(row)

@app.get('/api/maintenance')
def maintenance_status():
    import jobs
    con=db()
    data={'jobs':{name:jobs.read(name) for name in ('catalog','sync','index','thumbnails')},'busy':jobs.busy()}
    data['empty_collections']=con.execute('SELECT count(*) FROM collections WHERE n_files=0').fetchone()[0]
    data['missing_metadata']=con.execute("SELECT count(*) FROM collections WHERE family IS NULL OR family='' ").fetchone()[0]
    try:
        data['needs_ocr']=con.execute('SELECT count(*) FROM pdf_status WHERE chars=0').fetchone()[0]
    except sqlite3.OperationalError: data['needs_ocr']=0
    con.close()
    for job in data['jobs'].values():
        if job.get('running') and not data['busy']:
            job.update(running=False,error='Interrupted; safe to retry')
    return data

@app.post('/api/metadata/{cid}')
async def edit_metadata(cid:int,request:Request):
    import jobs,json
    from metadata import initialize,get
    data=await request.json()
    if not isinstance(data,dict):raise HTTPException(400,'Expected metadata fields')
    clean={}
    for key in ('name','family','publisher','genre','cover_file'):
        value=data.get(key,'')
        if not isinstance(value,str) or len(value)>1000:raise HTTPException(400,'Invalid '+key)
        clean[key]=value.strip()
    if not clean['name']:raise HTTPException(400,'A title is required')
    if 'shelf' in data:
        if data['shelf'] not in SHELF_LABELS:raise HTTPException(400,'Choose a known shelf')
        clean['shelf']=data['shelf']
    if 'tags' in data:
        if not isinstance(data['tags'],str) or len(data['tags'])>2000:raise HTTPException(400,'Invalid tags')
        clean['tags']=sorted(set(t.strip() for t in data['tags'].split(',') if t.strip()))
    try:clean['cover_page']=max(1,min(int(data.get('cover_page',1)),10000))
    except (ValueError,TypeError):raise HTTPException(400,'Invalid cover page')
    try:
        with jobs.lease():
            con=db(write=True)
            try:
                row=con.execute('SELECT rel_path,shelf FROM collections WHERE id=?',(cid,)).fetchone()
                if not row:raise HTTPException(404,'Collection not found')
                if clean['cover_file'] and not con.execute('SELECT 1 FROM files WHERE collection_id=? AND rel_path=?',(cid,clean['cover_file'])).fetchone():raise HTTPException(400,'Choose a file from this collection')
                initialize(con)
                clean={**get(con,row['rel_path']),**clean}
                clean.setdefault('shelf',row['shelf'])
                clean.setdefault('tags',[r[0] for r in con.execute('SELECT t.name FROM tags t JOIN collection_tags ct ON t.id=ct.tag_id WHERE ct.collection_id=?',(cid,))])
                con.execute('INSERT INTO metadata_overrides VALUES (?,?) ON CONFLICT(rel_path) DO UPDATE SET data=excluded.data',(row['rel_path'],json.dumps(clean)))
                con.execute('UPDATE collections SET name=?,sort_name=?,family=?,publisher=?,genre=? WHERE id=?',(clean['name'],clean['name'].lower(),clean['family'],clean['publisher'],clean['genre'],cid))
                con.execute('UPDATE collections SET shelf=? WHERE id=?',(clean['shelf'],cid))
                con.execute('DELETE FROM collection_tags WHERE collection_id=?',(cid,))
                for tag in sorted(set(clean['tags'])|{x for x in (clean['family'],clean['genre']) if x}):
                    con.execute('INSERT OR IGNORE INTO tags(name) VALUES (?)',(tag,))
                    con.execute('INSERT OR IGNORE INTO collection_tags VALUES (?,(SELECT id FROM tags WHERE name=?))',(cid,tag))
                con.execute('UPDATE search SET name=?,shelf=?,tags=? WHERE collection_id=?',(clean['name'],clean['shelf'],' '.join(clean['tags']+[clean['family'],clean['genre'],clean['publisher']]),cid))
                import taxonomy
                taxonomy.initialize(con)
                con.execute('UPDATE classification_audit SET assigned_shelf=?,rule="manual",needs_review=0 WHERE rel_path=?',(clean['shelf'],row['rel_path']))
                con.commit()
            finally:con.close()
        # A renamed collection, family or publisher changes what the collections
        # tab counts, so the totals cached under the old name are now wrong.
        clear_search_totals()
    except RuntimeError as exc:raise HTTPException(409,str(exc))
    return {'ok':True}

@app.get('/classification')
def classification_review(request:Request,all:bool=False):
    import taxonomy
    con=db()
    try:
        # Creating the audit table is a no-op once it exists, which is the
        # normal case: catalog.py builds it. A catalogue old enough to lack it
        # needs one writable connection to catch up, not a writable route.
        if not con.execute("SELECT 1 FROM sqlite_master WHERE name='classification_audit'").fetchone():
            con.close();con=db(write=True);taxonomy.initialize(con)
        rows=con.execute("SELECT c.*,a.source_shelf,a.rule,a.needs_review FROM collections c JOIN classification_audit a USING(rel_path) WHERE (? OR a.needs_review=1) ORDER BY c.name",(all,)).fetchall()
    finally:con.close()
    return templates.TemplateResponse(request,'classification.html',{'rows':rows,'all':all,'shelf_label':shelf_label})


def search_return(request):
    from urllib.parse import urlsplit
    value=request.query_params.get('return_to','')
    parts=urlsplit(value)
    return value if not parts.scheme and not parts.netloc and parts.path=='/search' and not any(c in value for c in ('\\','\r','\n')) else ''

def browse_url(request,path):
    from urllib.parse import urlsplit,parse_qsl,urlunsplit
    back=str(request.url.path)+('?' + request.url.query if request.url.query else '') if request.url.path=='/search' else search_return(request)
    if not back:return path
    p=urlsplit(path);q=dict(parse_qsl(p.query));q['return_to']=back
    return urlunsplit(('', '',p.path,urlencode(q),p.fragment))

templates.env.globals.update(search_return=search_return,browse_url=browse_url)

@app.get('/campaigns')
def campaign_directory(request:Request,q:str=''):
    import campaigns
    con=db()
    try:items=campaigns.catalog(con)
    finally:con.close()
    if q:items=[c for c in items if q.casefold() in (c['name']+' '+c['edition']).casefold()]
    return templates.TemplateResponse(request,'campaigns.html',{'campaigns':items,'q':q})

@app.get('/campaign/{system}/{slug}')
def campaign_detail(request:Request,system:str,slug:str):
    import campaigns
    con=db()
    try:item=next((c for c in campaigns.catalog(con) if c['system']==system and c['slug']==slug),None)
    finally:con.close()
    if item is None:raise HTTPException(404,'Campaign not found')
    return templates.TemplateResponse(request,'campaign.html',{'campaign':item})

@app.get('/explore')
def explore(request:Request,q:str='',system:str='',edition:str='',role:str='',series:str='',collection_id:int=0,page:int=1,exact_edition:bool=False):
    import classification
    con=db()
    try:
        groups=classification.grouped(con,q,system,edition,role,collection_id or None,exact_edition=exact_edition)
        if series:groups=[g for g in groups if g['name']==series]
        systems=con.execute('SELECT DISTINCT system FROM book_classification ORDER BY system').fetchall()
        editions=con.execute('SELECT DISTINCT edition FROM book_classification WHERE edition!="" AND (?="" OR system=?) ORDER BY edition',(system,system)).fetchall()
    finally:con.close()
    if not system and not q and not collection_id:
        summary={}
        for g in groups:
            item=summary.setdefault(g['system'],dict(system=g['system'],count=0,series=0))
            item['count']+=len(g['files']);item['series']+=1
        system_cards=list(summary.values())
    else:system_cards=[]
    count=len(groups);pages=max(1,(count+23)//24);page=max(1,min(page,pages))
    return templates.TemplateResponse(request,'explore.html',dict(groups=groups[(page-1)*24:page*24],system_cards=system_cards,systems=systems,editions=editions,roles=classification.ROLES,q=q,system=system,edition=edition,role=role,series=series,collection_id=collection_id,page=page,pages=pages,total=count))

@app.post('/api/book-metadata/{fid}')
async def edit_book_metadata(fid:int,request:Request):
    import catalog,classification,jobs,json
    data=await request.json()
    if not isinstance(data,dict):raise HTTPException(400,'Expected classification fields')
    fields={}
    for key in ('system','edition','series','campaign','role'):
        value=data.get(key,'')
        if not isinstance(value,str) or len(value)>500:raise HTTPException(400,'Invalid '+key)
        fields[key]=value.strip()
    if not fields['system'] or fields['role'] not in classification.ROLES:raise HTTPException(400,'Choose a system and valid role')
    try:fields['volume']=int(data['volume']) if data.get('volume') else None
    except (ValueError,TypeError):raise HTTPException(400,'Invalid volume')
    if fields['volume'] is not None and not 1<=fields['volume']<=9999:raise HTTPException(400,'Invalid volume')
    try:
        with jobs.lease():
            con=db(write=True)
            try:
                classification.initialize(con)
                row=con.execute('SELECT rel_path FROM files WHERE id=?',(fid,)).fetchone()
                if not row:raise HTTPException(404,'File not found')
                con.execute('INSERT OR REPLACE INTO book_overrides VALUES (?,?)',(row['rel_path'],json.dumps(fields)))
                con.execute('INSERT OR REPLACE INTO book_classification VALUES (?,?,?,?,?,?,?,?,?,?)',(row['rel_path'],fields['system'],fields['edition'],fields['series'],fields['campaign'],fields['volume'],fields['role'],'manual','Keeper annotation',classification.VERSION))
                catalog.reindex_file(con,fid)
                con.commit()
            finally:con.close()
        # The system, series and campaign feed the files tab's own filter, so a
        # total cached before this edit no longer matches the catalogue.
        clear_search_totals()
    except RuntimeError as exc:raise HTTPException(409,str(exc))
    return {'ok':True}

def book_details(fid):
    con=db()
    try:return con.execute('SELECT b.* FROM book_classification b JOIN files f USING(rel_path) WHERE f.id=?',(fid,)).fetchone()
    except sqlite3.OperationalError:return None
    finally:con.close()
templates.env.globals['book_details']=book_details

# -------------------------------------------------------------- arrivals ---
@app.get('/inbox')
def inbox_page(request:Request):
    import inbox
    return templates.TemplateResponse(request,'inbox.html',{'entries':inbox.inventory(LIBRARY),'shelves':{k:v for k,v in SHELF_LABELS.items() if not k.startswith('_') and k!='11-DriveThruRPG'}})

@app.get('/api/inbox/status')
def inbox_status():
    import jobs
    state=jobs.read('inbox')
    if state.get('running') and state.get('pid')!=os.getpid() and not jobs.busy():
        state=jobs.record('inbox',running=False,error='Import was interrupted. Check the destination and retained originals before trying again.',message='Import interrupted')
    return state

@app.post('/api/inbox/upload')
async def inbox_upload(request:Request,filename:str):
    import inbox,jobs,tempfile
    from pathlib import Path
    temporary=None
    try:
        name=inbox.component(filename)
        with jobs.lease():
            root=Path(LIBRARY)/'_Inbox';root.mkdir(parents=True,exist_ok=True)
            target=root/name
            if target.exists():raise HTTPException(409,'An inbox item already has that name. Rename the upload first.')
            with tempfile.NamedTemporaryFile(prefix='.upload-',dir=root,delete=False) as output:
                temporary=Path(output.name);size=0
                async for chunk in request.stream():
                    size+=len(chunk)
                    if size>inbox.MAX_UPLOAD:raise HTTPException(413,'Each upload must be 2 GB or smaller.')
                    output.write(chunk)
            if not size:raise HTTPException(400,'The uploaded file is empty.')
            try:os.link(temporary,target)
            except FileExistsError:raise HTTPException(409,'An inbox item already has that name.')
            return {'ok':True,'name':name,'bytes':size}
    except ValueError as exc:raise HTTPException(400,str(exc))
    except RuntimeError as exc:raise HTTPException(409,str(exc))
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)

@app.get('/api/inbox/plan')
def inbox_plan():
    import inbox
    con=db()
    try:return inbox.plan(LIBRARY,con)
    finally:con.close()

@app.post('/api/inbox/process')
async def inbox_process(request:Request):
    import inbox,jobs
    data=await request.json()
    if not isinstance(data,dict) or data.get('automatic') is not True:raise HTTPException(400,'Preview automatic placement first.')
    lease=jobs.lease()
    try:fd=lease.__enter__()
    except RuntimeError as exc:raise HTTPException(409,str(exc))
    try:
        con=db()
        try:plan=inbox.plan(LIBRARY,con)
        finally:con.close()
        if not plan['groups']:raise HTTPException(400,'No arrivals can be placed automatically. Review the uncertain items first.')
        # Refuse to import a different batch than the one the user reviewed.
        if data.get('groups')!=plan['groups']:raise HTTPException(409,'The inbox or catalog changed. Refresh the placement preview before importing.')
        jobs.record('inbox',running=True,pid=os.getpid(),started=time.time(),finished=None,error=None,message='Starting automatic import…',completed=0,total=plan['count'],destination=None,originals=None,collection_id=None,results=[],result_collections=[],errors=[],batch_completed=0,batch_total=len(plan['groups']))
        def work():
            try:
                results,errors=inbox.process_batch(LIBRARY,plan['groups'],catalog_command(),fd)
                con=db()
                try:
                    links=[dict(row) for path in results for row in con.execute('SELECT id,name FROM collections WHERE rel_path=?',(path,))]
                finally:con.close()
                jobs.record('inbox',running=False,finished=time.time(),result_collections=links,message=f'Imported {len(results)} collections. {len(plan["review"])} inbox items still need review.',error='; '.join(errors) or None)
            except Exception as exc:jobs.record('inbox',running=False,finished=time.time(),error=str(exc),message='Import needs attention')
            finally:
                lease.__exit__(None,None,None)
                # The import rebuilt the catalogue in a child process, so the
                # totals cached by this one describe collections that have moved.
                clear_search_totals()
        threading.Thread(target=work,daemon=True).start()
    except Exception:
        lease.__exit__(None,None,None)
        raise
    return JSONResponse({'ok':True},status_code=202)
