import os, pymupdf
from jobs import lease
COVER_WIDTH=340
def media_of(ext):
    return "pdf" if ext.lower()==".pdf" else "image"

def _shrink_to(pix, width: int):
    """Halve a pixmap until its long edge is about `width`."""
    steps = 0
    while max(pix.width, pix.height) // (2 ** (steps + 1)) >= width:
        steps += 1
    if steps:
        pix.shrink(min(steps, 24))
    return pix


def _build_thumbs(src: str, prefix: str,
                  widths: list[int], page_index=None) -> dict[int, tuple[str, str]]:
    """Build thumbnails at several widths from a single decode.

    Decoding is the whole cost here -- a 116 MB map scan takes about 3.5 s --
    while shrinking an already-decoded pixmap is nearly free. List rows want
    160 px and hover snapshots want 320 px, so building both together costs
    barely more than building either one alone.

    Returns {width: (path, content_type)}. Opaque sources become JPEG; sources
    with an alpha channel stay PNG, because many tokens and art pieces are
    transparent and flattening them onto black would ruin them.
    """
    widths = sorted(set(widths), reverse=True)
    biggest = widths[0]

    if media_of(os.path.splitext(src)[1]) == 'pdf':
        doc = pymupdf.open(src)
        try:
            if doc.page_count < 1:
                raise ValueError('empty document')
            page = doc[min(max(0,page_index),doc.page_count-1) if page_index is not None else _cover_page_index(doc)]
            zoom = biggest / max(page.rect.width, 1.0)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
        finally:
            doc.close()
        keep_alpha = False
    else:
        pix = pymupdf.Pixmap(src)
        keep_alpha = bool(pix.alpha)
        _shrink_to(pix, biggest)

    made: dict[int, tuple[str, str]] = {}
    for width in widths:
        _shrink_to(pix, width)          # shrink mutates, so go large -> small
        ext = 'png' if keep_alpha else 'jpg'
        dst = f'{prefix}-{width}.{ext}'
        # pymupdf infers the format from the extension and the temp file ends
        # in .tmp, so the output format has to be named explicitly
        tmp = f'{dst}.{os.getpid()}.tmp'
        if keep_alpha:
            pix.save(tmp, output='png')
        else:
            pix.save(tmp, output='jpg', jpg_quality=82)
        os.replace(tmp, dst)            # atomic: never serve a half-written file
        made[width] = (dst, 'image/png' if keep_alpha else 'image/jpeg')
    return made


_COVER_STOPWORDS = (
    'table of contents', 'contents', 'credits', 'legal', 'license', 'licence',
    'index', 'acknowledg', 'dedication', 'copyright', 'playtesters',
    'open game licence', 'open game license', 'ogl',
)


def _cover_page_index(doc, limit: int = 6) -> int:
    """Pick the page that looks most like cover art.

    Plenty of PDFs lead with a table of contents, a credits page or a licence,
    which turns a whole shelf into identical grey spines. Score the opening
    pages on how much of each is covered by artwork and how little text it
    carries, and take the best. Page 1 wins ties, so ordinary books whose first
    page really is the cover are unaffected.
    """
    best_i, best_score = 0, -1.0
    for i in range(min(limit, doc.page_count)):
        try:
            page = doc[i]
            area = abs(page.rect.width * page.rect.height) or 1.0
            art = 0.0
            for info in page.get_image_info():
                r = pymupdf.Rect(info['bbox'])
                art += abs(r.width * r.height)
            text = page.get_text().strip()
        except Exception:               # noqa: BLE001 - never lose the whole cover
            continue
        score = min(art / area, 1.0) - min(len(text) / 1500.0, 0.6)
        if any(w in text[:400].lower() for w in _COVER_STOPWORDS):
            score -= 0.5                  # front matter is never a cover
        if score > best_score + 1e-6:
            best_i, best_score = i, score
    return best_i


def _make_cover(src: str, base: str) -> tuple[str, str]:
    """A shelf cover: the shared thumbnail builder at cover width.

    Delegating matters beyond avoiding duplication -- the route looks the
    result up as `{base}-{COVER_WIDTH}.jpg`, so a private naming scheme here
    would write a file that is never found and re-render on every request.
    """
    return _build_thumbs(src, base, [COVER_WIDTH])[COVER_WIDTH]



def render(src, prefix, widths, page=None):
    with lease('render', blocking=True):
        return _build_thumbs(src, prefix, widths, page)
