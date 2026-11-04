# The Great Library — October 2026

The web app now has a custom illustrated grand hall, local book typography, browsable wings and cover shelves, an asset gallery, and responsive layouts. Collections group rulebooks, supplements, campaigns, sheets, maps, art, and media. Search combines text with filters and offers separate collection, file, and inside-book results with totals and pagination.

The Reading Desk saves collections, PDF positions, bookmarks, and notes **in the current browser**. Export/import JSON moves these between browsers or keeps a backup. It does not synchronize across devices automatically.

## Reliability and maintenance

- Catalog rescans are transactional, preserve existing path IDs, and never recycle removed IDs. Source books and folders are not moved by classification.
- PDF indexing replaces each processed book's old pages, with a rowid map for efficient cleanup. A failed extraction is reported and retains the prior index for that book.
- PyMuPDF rendering runs in an isolated process under a cross-process lock. Busy thumbnail requests return a retryable response instead of blocking the request pool. Browser thumbnails retry briefly. Bulk thumbnail jobs are not automatically started.
- Maintenance jobs use one filesystem lease across CLI and web tasks, with durable JSON records. Sync stops on the first failed stage.
- Mutating APIs require the same-origin interface header. Maintenance and metadata changes require loopback access, or `RPG_ADMIN_TOKEN` and an `X-Library-Admin` header. LAN browsing remains available. Use the host's localhost URL for editing.
- Byte range suffix requests, Unicode download names, safe raw-file headers, and restart supervisor checks are repaired.
- `requirements.txt` pins the installed runtime packages. Run `./.venv/bin/python -m unittest discover -s tests -v` for regression checks.

## Catalog taxonomy

The original catalog equated a folder's location with a shelf, and used exact folder names for metadata. `taxonomy.py` now normalizes names and recognizes specific series. The first classification pass corrects 219 shelf assignments. OSE titles and explicitly OSE-compatible supplements belong to OSR, tagged `osr` and `old-school-essentials`, with B/X as their rules family. Shelf, rules family, genre, and publisher remain separate fields.

Other corrections include Shadowdark, Pirate Borg, Vaesen, Dragonbane, Forbidden Lands, and several licensed series. Early numbered DCC adventures retain their D&D edition classification; the later DCC RPG belongs in OSR. Rules never infer a third-party supplement's publisher from its target system.

The Keeper's Office links to `/classification`, where unresolved folder-only classifications are explicitly marked for review. `/classification?all=true` shows every decision. A title rule is evidence-based automation, not a guarantee that every document inside a mixed collection is compatible with the same system. Use the collection's Keeper annotations to choose a shelf, family, genre, publisher, tags, and cover; overrides survive rescans. These are virtual catalog shelves and do not rename source files.

Primary references checked for corrections:
- https://necroticgnome.com/pages/about-old-school-essentials
- https://www.daggerheart.com/welcome-to-daggerheart/
- https://freeleaguepublishing.com/shop/dragonbane/rulebook/
- https://freeleaguepublishing.com/games/vaesen/
- https://freeleaguepublishing.com/games/into-the-odd/
- https://www.limithron.com/pirateborg
- https://www.runehammer.online/crown-skull-rpg
- https://www.goodman-games.com/images/dccrpg.html

## Backups and restoration

`backup.py SOURCE.db NEW_DESTINATION.db` uses SQLite's online backup API. Choose another disk and a new filename. Before this release, a consistent database and app-source backup was saved under `/mnt/disk4/RPG-Library-Backups/`; the exact path is in `DEPLOYMENT.json`. This is **not** a backup of the 117 GB source book/media library. Existing source archives remain separate. No recurring backup schedule was installed.

To roll back, stop `rpg-library`, restore the saved application files, templates, static directory and database together, then restart it. Preserve the current database separately if newer annotations or catalog changes must be recovered. Reading Desk data remains in browsers and can be exported.

## Artwork

`static/great-library.png` was generated using the built-in image-generation tool. Its exact production prompt is saved in `ART-PROMPT.txt`. Fonts and PDF.js remain served locally.
