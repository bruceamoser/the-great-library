# Search, TIFF, and deep catalog update — October 7, 2026

## Search and navigation

Search forms and result-page navigation show a live status banner immediately, with a spinner, a longer-running message, and Cancel. Browser back/forward restores the normal state. Result links carry the exact search URL (query, filters, sort, result section, and page) into collections, readers, and campaign pages, including new tabs. The reader preserves this URL when changing PDF pages. Return URLs are restricted to local `/search` routes.

## TIFF

`.tif` and `.tiff` are registered as images. Existing isolated PyMuPDF rendering converts them to cached browser-compatible JPEG/PNG previews for lists, modals, and the full viewer. The viewer shows preparation feedback; original TIFF downloads remain unchanged. The display is a downscaled preview of the first image, not a lossless TIFF editor or multipage TIFF reader. Verified with a real 4808 × 4808 library TIFF.

## File-level catalog hierarchy

`classification.py` maintains a new `book_classification` table keyed by stable file path. Every tracked file receives separate fields for game system, edition, series, campaign, volume, resource role, confidence, and evidence. `book_title_evidence` stores bounded text from the first two already-indexed PDF pages. `book_overrides` holds persistent manual choices.

The classifier uses nested folder context, named game signals, product-package paths, numbered publications, filenames, and opening-page text. It separates games inside mixed engine folders and keeps editions separate. Campaign resource roles are independent of membership: maps, guides, sheets, and tokens remain attached to their campaign. Shared numbered publication names group across collection folders. Specific Pathfinder product-name rules complement the generic hierarchy; they are not its scope.

`/explore` browses systems → editions → series/campaigns → volumes/resources. Filters search metadata and filenames and select resource roles. Collection pages show their internal groups. File pages show classification evidence and editable fields. Main file search also matches system, series, and campaign metadata. `/campaigns` is a campaign-focused view across systems.

Manual classification changes require the same maintenance authorization as other metadata changes. Catalog rescans recompute automatic metadata while preserving manual choices. To enrich the library from available opening-page text, run:

```sh
RPG_STATE_DIR=/mnt/disk5/rpg-app /mnt/disk5/rpg-app/.venv/bin/python /mnt/disk5/rpg-app/classification.py --db /mnt/disk5/rpg-app/library.db --extract-titles
```

This is an evidence-based classification layer, not a claim that every title has been authoritatively identified. Unclear game identity is shown as **Unassigned system**; ambiguous roles as **unclassified**; incomplete evidence is marked **review**. No source folders are moved, no books are merged or deleted, and grouped duplicates/variants remain visible. The original collection shelf ledger still exists separately.

## Validation

Regression checks cover existing catalog behavior plus nested games, campaign/map roles, numbered series, edition separation, uncertain classifications, opening-text evidence, safe search-return URLs, persistent file overrides, and TIFF viewer routing. Browser checks cover a real TIFF and returning to the exact filtered search. The deployment record names the fresh pre-update database and source backup.
