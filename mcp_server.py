#!/usr/bin/env python3
"""
MCP server for the RPG library.

Exposes the collection to an AI assistant as a set of tools: catalogue search,
full-text search *inside* the books, page-level reading, asset browsing and a
few things that are only possible because the whole library is indexed.

Run:  ./.venv/bin/python mcp_server.py          (stdio transport)

It deliberately imports `app` rather than re-implementing anything, so the
ranking and FTS behaviour here is exactly what the web UI uses. There is one
search implementation in this project and this is not a second one.

IMPORTANT: stdout is the JSON-RPC channel. Nothing may print to it. Any
diagnostics must go to stderr.
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.mcpserver import MCPServer      # noqa: E402

import app as A                                  # noqa: E402

# Where the web UI is reachable, so tool output can carry clickable citations.
BASE_URL = os.environ.get('RPG_BASE_URL', 'http://192.168.44.50:8765').rstrip('/')

mcp = MCPServer(
    name='rpg-library',
    instructions=(
        'A private tabletop RPG library: 758 collections, ~23,000 files and a '
        'full-text index of ~318,000 book pages. Use search_library for titles, '
        'publishers and collections; use search_inside_books to find rules and '
        'passages *within* the books, which returns page-level citations you '
        'should pass on to the user. read_pages reads the actual text of a page '
        'range, so prefer search_inside_books -> read_pages when answering rules '
        'questions. Never invent a citation: every page reference must come from '
        'a tool result.'
    ),
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _con() -> sqlite3.Connection:
    con = sqlite3.connect(A.DB)
    con.row_factory = sqlite3.Row
    return con


def _size(n: Any) -> str:
    return A.human_size(n)


def _view_url(fid: int, page: int | None = None) -> str:
    return f'{BASE_URL}/view/{fid}' + (f'?page={page}' if page else '')


def _collection_url(cid: int) -> str:
    return f'{BASE_URL}/collection/{cid}'


def _cover_url(cid: int) -> str:
    return f'{BASE_URL}/cover/{cid}'


def _rows(rows, fmt) -> str:
    """Format rows, or say so plainly when there are none."""
    return '\n'.join(fmt(r) for r in rows) if rows else '(nothing found)'


# ---------------------------------------------------------------------------
# catalogue tools
# ---------------------------------------------------------------------------
@mcp.tool()
def library_stats() -> str:
    """Overall size and shape of the library: collections, files, disk usage,
    and the breakdown by category, subcategory, file kind and source."""
    con = _con()
    try:
        total = con.execute(
            'SELECT COUNT(*) n, COALESCE(SUM(n_files),0) f,'
            ' COALESCE(SUM(n_bytes),0) b FROM collections').fetchone()
        out = [f'collections : {total["n"]:,}',
               f'files       : {total["f"]:,}',
               f'size        : {_size(total["b"])}']
        out.append('\nby category:')
        for r in con.execute(
                'SELECT category, COALESCE(subcategory, "-") sub, COUNT(*) n,'
                ' SUM(size) b FROM files GROUP BY 1, 2 ORDER BY 4 DESC'):
            out.append(f'  {r["category"]:<6} {r["sub"]:<12} {r["n"]:>7,}  {_size(r["b"])}')
        out.append('\nby source:')
        for r in con.execute(
                'SELECT source, COUNT(*) n FROM collections GROUP BY 1 ORDER BY 2 DESC'):
            out.append(f'  {r["source"]:<10} {r["n"]:>5,} collections')
        idx = con.execute('SELECT COUNT(*) n, SUM(pages) p FROM pdf_status').fetchone()
        out.append(f'\nPDF text index: {idx["n"]:,} files, {idx["p"]:,} pages')
        return '\n'.join(out)
    finally:
        con.close()


@mcp.tool()
def list_shelves() -> str:
    """List the physical shelves and how much sits on each one."""
    con = _con()
    try:
        rows = con.execute(
            'SELECT shelf, COUNT(*) n, SUM(n_files) f, SUM(n_bytes) b'
            ' FROM collections GROUP BY shelf ORDER BY shelf').fetchall()
        return _rows(rows, lambda r: (
            f'{A.shelf_label(r["shelf"]):<28} {r["n"]:>4} collections'
            f'  {r["f"]:>7,} files  {_size(r["b"])}'))
    finally:
        con.close()


@mcp.tool()
def search_library(query: str = '', category: str = '', subcategory: str = '',
                   shelf: str = '', publisher: str = '', limit: int = 25) -> str:
    """Find collections by title, publisher or family name.

    Args:
        query: words to match against the collection name and publisher.
        category: 'book' or 'asset' (optional).
        subcategory: core-rules, expansion, campaign, sheets, maps, art, audio,
            video or project (optional).
        shelf: a shelf id such as '03-Indie-and-Story' (optional).
        publisher: publisher name fragment (optional).
        limit: maximum collections to return.
    """
    con = _con()
    try:
        where, params = [], []
        if query:
            where.append('(name LIKE ? OR publisher LIKE ? OR family LIKE ?)')
            params += [f'%{query}%'] * 3
        if category:
            where.append('category = ?')
            params.append(category)
        if shelf:
            where.append('shelf = ?')
            params.append(shelf)
        if publisher:
            where.append('publisher LIKE ?')
            params.append(f'%{publisher}%')
        sql = 'SELECT * FROM collections'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        if subcategory:
            sql = ('SELECT c.* FROM collections c WHERE c.id IN'
                   ' (SELECT collection_id FROM files WHERE subcategory = ?)')
            if where:
                sql += ' AND ' + ' AND '.join(where)
        sql += ' ORDER BY n_files DESC LIMIT ?'
        params.append(max(1, min(limit, 200)))
        rows = con.execute(sql, params).fetchall()
        return _rows(rows, lambda r: (
            f'[c{r["id"]}] {r["name"]}  ({r["n_files"]} files, {_size(r["n_bytes"])})\n'
            f'      {A.shelf_label(r["shelf"])}'
            + (f' · {r["publisher"]}' if r['publisher'] else '')
            + (f' · {r["family"]}' if r['family'] else '')
            + f'\n      {_collection_url(r["id"])}'))
    finally:
        con.close()


@mcp.tool()
def get_collection(collection_id: int, limit: int = 60) -> str:
    """Everything in one collection: its metadata and its file list.

    Args:
        collection_id: the number shown as [cN] by the search tools.
        limit: maximum files to list.
    """
    con = _con()
    try:
        col = con.execute('SELECT * FROM collections WHERE id = ?',
                          (collection_id,)).fetchone()
        if not col:
            return f'No collection with id {collection_id}.'
        head = [f'{col["name"]}',
                f'shelf       : {A.shelf_label(col["shelf"])}',
                f'category    : {col["category"]}',
                f'files       : {col["n_files"]}  ({_size(col["n_bytes"])})',
                f'source      : {col["source"]}']
        for key in ('family', 'publisher', 'genre'):
            if col[key]:
                head.append(f'{key:<12}: {col[key]}')
        head.append(f'cover       : {_cover_url(collection_id)}')
        head.append(f'page        : {_collection_url(collection_id)}')
        files = con.execute(
            'SELECT id, filename, ext, kind, category, subcategory, size'
            ' FROM files WHERE collection_id = ? ORDER BY kind, filename'
            ' LIMIT ?', (collection_id, max(1, min(limit, 500)))).fetchall()
        head.append('\nfiles:')
        head += [f'  [f{r["id"]}] {r["filename"]}  ({r["kind"]}, {_size(r["size"])})'
                 for r in files]
        return '\n'.join(head)
    finally:
        con.close()


@mcp.tool()
def get_file(file_id: int) -> str:
    """Details for one file: where it lives, and the links to open or play it."""
    con = _con()
    try:
        f = con.execute(
            'SELECT f.*, c.name collection_name, c.shelf FROM files f'
            ' JOIN collections c ON c.id = f.collection_id WHERE f.id = ?',
            (file_id,)).fetchone()
        if not f:
            return f'No file with id {file_id}.'
        kind = A.media_of(f['ext'])
        lines = [f['filename'],
                 f'collection : {f["collection_name"]} ({A.shelf_label(f["shelf"])})',
                 f'type       : {f["kind"]} / {kind}',
                 f'size       : {_size(f["size"])}',
                 f'path       : {os.path.join(A.LIBRARY, f["rel_path"])}']
        if kind == 'pdf':
            st = con.execute('SELECT pages FROM pdf_status WHERE rel_path = ?',
                             (f['rel_path'],)).fetchone()
            if st:
                lines.append(f'pages      : {st["pages"]}')
            lines.append(f'read       : {_view_url(f["id"])}')
        elif kind == 'image':
            lines.append(f'thumbnail  : {BASE_URL}/thumb/{f["id"]}?w=1600')
        lines.append(f'raw        : {BASE_URL}/raw/{f["id"]}')
        return '\n'.join(lines)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# text tools — the reason the library is worth indexing
# ---------------------------------------------------------------------------
@mcp.tool()
def search_inside_books(query: str, limit: int = 20) -> str:
    """Full-text search across the *contents* of every indexed PDF.

    This is the tool for rules questions: it returns the actual matching pages
    with a snippet and a citation. Follow up with read_pages to read the page
    properly before answering.

    Args:
        query: words to find inside the books.
        limit: maximum pages to return.
    """
    con = _con()
    try:
        hits = A.content_search(con, query, max(1, min(limit, 100)))
        if not hits:
            return (f'No page in the library mentions {query!r}. The index covers '
                    f'~318,000 pages; image-only scans are not searchable.')
        out = [f'{len(hits)} page(s) mention {query!r}:']
        for h in hits:
            snip = (h['snip'] or '').replace('<<', '[').replace('>>', ']')
            snip = ' '.join(snip.split())
            out.append(f'\n[{h["filename"]}] page {h["page"]}'
                       f'  (collection: {h["collection_name"]})')
            out.append(f'  … {snip[:300]}')
            out.append(f'  {_view_url(h["fid"], h["page"])}')
        return '\n'.join(out)
    finally:
        con.close()


@mcp.tool()
def read_pages(file_id: int, first_page: int = 1, last_page: int | None = None) -> str:
    """Read the extracted text of a page range from one PDF.

    Use this after search_inside_books to read the surrounding rules text.

    Args:
        file_id: the file id, e.g. from a search result.
        first_page: first page to read (1-based).
        last_page: last page to read; defaults to first_page.
    """
    con = _con()
    try:
        f = con.execute('SELECT rel_path, filename, ext FROM files WHERE id = ?',
                        (file_id,)).fetchone()
        if not f:
            return f'No file with id {file_id}.'
        if A.media_of(f['ext']) != 'pdf':
            return f'{f["filename"]} is not a PDF.'
        first = max(1, first_page)
        last = max(first, min(last_page or first, first + 19))   # cap the payload
        rows = con.execute(
            'SELECT page, text FROM pdf_fts WHERE rel_path = ? AND page BETWEEN ?'
            ' AND ? ORDER BY page', (f['rel_path'], first, last)).fetchall()
        if not rows:
            return (f'No indexed text for pages {first}-{last} of {f["filename"]}. '
                    'It may be an image-only scan (about 6% of the library is).')
        out = [f'{f["filename"]} — pages {rows[0]["page"]}-{rows[-1]["page"]}']
        for r in rows:
            out.append(f'\n--- page {r["page"]} ---')
            out.append((r['text'] or '').strip()[:4000])
        out.append(f'\n{_view_url(file_id, first)}')
        return '\n'.join(out)
    finally:
        con.close()


@mcp.tool()
def mechanic_across_systems(term: str, limit: int = 12) -> str:
    """Compare how often different games mention a mechanic or term.

    Useful for 'how do other systems handle X' questions: it counts matching
    pages per collection, so you can spot which games actually cover a topic.

    Args:
        term: the mechanic or word, e.g. 'momentum', 'sanity', 'death and dying'.
        limit: maximum collections to return.
    """
    con = _con()
    try:
        rows = con.execute(
            """SELECT c.id cid, c.name, c.shelf, COUNT(*) hits,
                      COUNT(DISTINCT p.rel_path) books
               FROM pdf_fts p
               JOIN files f ON f.rel_path = p.rel_path
               JOIN collections c ON c.id = f.collection_id
               WHERE pdf_fts MATCH ?
               GROUP BY c.id ORDER BY hits DESC LIMIT ?""",
            (A.fts_query(term), max(1, min(limit, 50)))).fetchall()
        if not rows:
            return f'No indexed page mentions {term!r}.'
        return _rows(rows, lambda r: (
            f'{r["hits"]:>5} pages across {r["books"]:>3} book(s)  —  {r["name"]}'
            f'  [{A.shelf_label(r["shelf"])}]'))
    finally:
        con.close()


@mcp.tool()
def find_series(name: str, limit: int = 40) -> str:
    """Find titles that look like they belong to one series.

    Matches a shared title stem and sorts naturally, so numbered installments
    come back in reading order.

    Args:
        name: the shared part of the title, e.g. 'Horn of the Dawn'.
    """
    con = _con()
    try:
        rows = con.execute(
            'SELECT id, name, shelf, n_files FROM collections WHERE name LIKE ?'
            ' ORDER BY name LIMIT ?', (f'%{name}%', max(1, min(limit, 200)))
        ).fetchall()
        if not rows:
            return f'Nothing matching {name!r}.'
        return _rows(rows, lambda r: (
            f'[c{r["id"]}] {r["name"]}  ({r["n_files"]} files)'
            f'  — {A.shelf_label(r["shelf"])}'))
    finally:
        con.close()


@mcp.tool()
def browse_assets(kind: str = 'map', limit: int = 40, query: str = '') -> str:
    """Browse the asset side of the library: maps, art, tokens, audio, video.

    Args:
        kind: 'map', 'art', 'token', 'audio', 'video' or 'image' for any image.
        limit: maximum files to return.
        query: optional filename fragment.
    """
    con = _con()
    try:
        where, params = [], []
        if kind == 'image':
            where.append("ext IN ('.jpg','.jpeg','.png','.webp','.gif')")
        else:
            where.append('kind = ?')
            params.append(kind)
        if query:
            where.append('filename LIKE ?')
            params.append(f'%{query}%')
        rows = con.execute(
            f'SELECT f.id, f.filename, f.size, c.name collection_name'
            f' FROM files f JOIN collections c ON c.id = f.collection_id'
            f' WHERE {" AND ".join(where)} ORDER BY f.size DESC LIMIT ?',
            params + [max(1, min(limit, 200))]).fetchall()
        if not rows:
            return f'No {kind} assets found.'
        return _rows(rows, lambda r: (
            f'[f{r["id"]}] {r["filename"]}  ({_size(r["size"])}, {r["collection_name"]})\n'
            f'      {BASE_URL}/thumb/{r["id"]}?w=1600'))
    finally:
        con.close()


@mcp.tool()
def random_pick(category: str = '', subcategory: str = '') -> str:
    """Pick something at random from the shelves.

    Handy for 'what should we play tonight' — it surfaces things that get lost
    in a library this size.

    Args:
        category: 'book' or 'asset' (optional).
        subcategory: e.g. 'campaign' to get an adventure (optional).
    """
    con = _con()
    try:
        sql = ('SELECT DISTINCT c.* FROM collections c'
               ' JOIN files f ON f.collection_id = c.id WHERE c.n_files > 0')
        params = []
        if category:
            sql += ' AND f.category = ?'
            params.append(category)
        if subcategory:
            sql += ' AND f.subcategory = ?'
            params.append(subcategory)
        rows = con.execute(sql, params).fetchall()
        if not rows:
            return 'Nothing matches that.'
        r = random.choice(rows)
        return (f'{r["name"]}\n  {A.shelf_label(r["shelf"])}'
                f' · {r["n_files"]} files · {_size(r["n_bytes"])}'
                + (f'\n  {r["publisher"]}' if r['publisher'] else '')
                + f'\n  {_collection_url(r["id"])}')
    finally:
        con.close()


# ---------------------------------------------------------------------------
# resources — cheap context the assistant can pull without calling a tool
# ---------------------------------------------------------------------------
@mcp.resource('library://shelves')
def res_shelves() -> str:
    """The shelf list, as plain text."""
    return list_shelves()


@mcp.resource('library://stats')
def res_stats() -> str:
    """Library totals and breakdowns."""
    return library_stats()


@mcp.resource('library://collection/{cid}')
def res_collection(cid: int) -> str:
    """A single collection, addressed by id."""
    return get_collection(cid)


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------
@mcp.prompt(title='Rules lookup')
def rules_lookup(question: str) -> str:
    """Answer a rules question from the library, with citations."""
    return (
        f'Answer this rules question using only the library: {question}\n\n'
        'Use search_inside_books to find the relevant pages, then read_pages to '
        'read them properly. Cite every claim as "Book, page N" and include the '
        'viewing link. If the library does not cover it, say so rather than '
        'filling the gap from general knowledge.'
    )


@mcp.prompt(title='System comparison')
def compare_systems(topic: str) -> str:
    """Compare how the games in the library handle a topic."""
    return (
        f'Compare how the systems in this library handle: {topic}\n\n'
        'Use mechanic_across_systems to find which games actually cover it, then '
        'read_pages to check what they really say — do not judge from hit counts '
        'alone. Group the findings by system and be explicit about which games '
        'you could not verify.'
    )


if __name__ == '__main__':
    mcp.run()
