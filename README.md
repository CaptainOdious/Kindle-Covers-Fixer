# Kindle Covers Fixer

An interactive Python script for repairing Kindle book thumbnails using the local `cc.db` catalog.

The script creates a database backup before scanning, lists affected books, and asks for confirmation before repairing them. It separates missing or unreadable thumbnails from readable thumbnails with a height below 500 pixels.

## Features

- Explicit confirmation before backing up and scanning the catalog.
- Automatic backup of `cc.db` before any repair.
- Two independently confirmed repair phases:
  1. Missing or unreadable thumbnails.
  2. Readable thumbnails with a height below 500 pixels.
- Cover retrieval from three sources, in this order:
  1. The local cover referenced by `p_cover`.
  2. A cover embedded in the book file.
  3. Google Books.
- Temporary EPUB conversion through Bokai for supported non-EPUB formats.
- Generated thumbnails saved as 500-pixel-high grayscale JPEG images.
- Database updates grouped into one transaction per repair phase.
- A persistent journal recording created or replaced thumbnail files.
- Console output and file logging.
- Optional Kindle restart at the end.

## Requirements

The script is intended to run directly on a Kindle with access to its local catalog and filesystem.

It requires:

- A jailbroken Kindle
- KUAL/KUALA/KUAL Next
- KTerm
- Python 3.10 or later.
- The `requests` Python package.
- Pillow, imported as `PIL`.
- Read and write access to `/var/local/cc.db`.
- Write access to the backup, thumbnail, and temporary directories.

Optional:

- Bokai for extracting covers from KFX, AZW3, KF8, MOBI, and AZW files through temporary EPUB conversion.
- An internet connection for the Google Books fallback.
- A Google Books API key supplied through `GOOGLE_BOOKS_API_KEY`.

The default interpreter path is:

```text
/mnt/us/python3/python/bin/python3
```

Package installation depends on the Python environment installed on your Kindle.

## Installation

1. Copy `fix_covers` folder to your Kindle into `/mnt/us/extensions`.
2. Verify that the required Python packages are available.
3. Review the configuration section and adjust paths if necessary.
4. If you intend to use Bokai, verify that its executable is available at:

   ```text
   /mnt/us/extensions/bokai/bin/bokai
   ```

5. Open the script from KUAL or directly with an interactive terminal, such as KTerm, and run the script.

## Usage

From the directory containing the script:

```bash
/mnt/us/python3/python/bin/python3 fix_covers.py
```

Alternatively, if `python3` points to a compatible interpreter:

```bash
python3 fix_covers.py
```

To supply an optional Google Books API key:

```bash
export GOOGLE_BOOKS_API_KEY="YOUR_API_KEY"
python3 fix_covers.py
```

Only `y` or `yes`, case-insensitively, confirms an action. Pressing Enter without an answer declines it.

## Workflow

### Initial confirmation and backup

The script first asks:

```text
Do you want to back up cc.db and scan for books with missing or unreadable thumbnails? [y/N]:
```

If you decline, the backup, catalog scan, and repair phases do not run.

If you accept, the script creates a database backup using SQLite's backup API. It then compares the number of entries in the backup with the source database.

This row-count comparison is a basic verification, not a full database integrity check.

If backup creation or verification fails, the script stops before repairing any books.

### Phase 1: Missing or unreadable thumbnails

The script reads catalog entries matching:

```sql
p_isArchived = 0 AND p_contentSize > 0
```

Entries without a UUID or title are excluded from the repair lists.

A book is selected when:

- Its `p_thumbnail` field is empty.
- The referenced thumbnail file does not exist.
- The referenced file cannot be opened and fully decoded as an image.

Thumbnail height is not a selection criterion in this phase.

The script displays the selected books and asks:

```text
Do you want to repair these N books? [y/N]:
```

Only the confirmed list is processed. Declining this phase does not prevent the script from offering phase 2.

### Phase 2: Readable thumbnails below 500 pixels

The script separately identifies thumbnails that can be decoded and whose height is strictly below 500 pixels.

- Height below 500 pixels: eligible.
- Height exactly 500 pixels: excluded.
- Height above 500 pixels: excluded.
- Missing or unreadable image: excluded from this phase.

The selected books are listed, and the script asks for confirmation before processing them.

The current implementation uses the catalog rows loaded during the initial scan rather than reading `cc.db` again between phases. It checks image dimensions when building the second list.

### Database update and cleanup

For each accepted repair phase:

1. The script attempts to generate replacement thumbnails.
2. Successful thumbnail paths are collected.
3. The corresponding `p_thumbnail` values are updated in one SQLite transaction.
4. A phase summary is displayed.
5. The legacy JPG cleanup runs if the phase completes without a database update failure.

If no replacement thumbnails are generated, the current implementation still runs the cleanup after an accepted phase.

### Optional restart

At the end of a completed run, the script asks:

```text
Restart the Kindle now? [y/N]:
```

Accepting invokes `reboot`. Declining leaves the Kindle running and displays a reminder to restart it to reload the catalog.

The script then waits for Enter before closing the terminal window.

## Cover Retrieval

### 1. Local cover: `p_cover`

The script first checks the file referenced by `p_cover`.

A source cover must:

- Exist.
- Be readable as an image.
- Have a width of at least 100 pixels.
- Have a height of at least 100 pixels.

### 2. Embedded book cover

For EPUB files, the script looks for:

- An EPUB 3 `cover-image` manifest property.
- An EPUB 2 cover metadata declaration.
- As a fallback, an image manifest entry whose ID or filename contains `cover`.

For supported non-EPUB formats, the script uses Bokai to create a temporary EPUB and attempts to extract its cover.

| Book format | Extraction method |
| --- | --- |
| EPUB | Direct cover extraction |
| KFX | Bokai conversion to EPUB |
| AZW3 / KF8 | Bokai conversion to EPUB |
| MOBI / AZW | Bokai conversion to EPUB |

Extraction is not guaranteed. It depends on the input file and, where applicable, Bokai's ability to convert it.

### 3. Google Books

If no usable local or embedded cover is found, the script searches Google Books.

A result is accepted only when its metadata matches the local book:

- Title.
- Author.
- Publisher.
- Publication year, when the local year is available.

If the title, author, or publisher is missing, the Google Books search is skipped.

These matching rules are intentionally restrictive. A book may remain unchanged even when Google Books has a visually suitable cover with different metadata.

## Generated Thumbnails

Replacement thumbnails are written to:

```text
/mnt/us/system/thumbnails/<p_uuid>.jpg
```

Output settings:

| Property | Value |
| --- | --- |
| Format | JPEG |
| Height | 500 pixels |
| Width | Calculated to preserve the source aspect ratio |
| Color | Grayscale |
| JPEG quality | 80 |
| Optimization | Enabled |
| Progressive encoding | Enabled |

The script applies EXIF orientation before resizing.

The 500-pixel output height remains a generation setting. It does not determine whether a thumbnail is missing or unreadable in phase 1.

## Paths and Configuration

The main settings are defined near the top of the script.

| Setting | Default |
| --- | --- |
| Database | `/var/local/cc.db` |
| Catalog table | `Entries` |
| Database backup directory | `/mnt/us/documents/ccdb_reset_backups` |
| Thumbnail directory | `/mnt/us/system/thumbnails` |
| Temporary root | `/mnt/base-us` |
| Log file | `/mnt/base-us/fixcovers.log` |
| Rollback journal directory | `/mnt/base-us/fixcovers_backups` |
| Bokai executable | `/mnt/us/extensions/bokai/bin/bokai` |
| Bokai timeout | 300 seconds |
| Generated thumbnail height | 500 pixels |
| Minimum source dimensions | 100 × 100 pixels |

Database backups use this naming pattern:

```text
cc.db.backup-YYYYMMDD-HHMMSS.sqlite
```

## Backups and Recovery Journal

Each run that passes the initial confirmation creates:

- A backup of `cc.db`.
- A run-specific JSON journal.
- Copies of existing destination thumbnails before they are replaced, when applicable.

The journal records information including:

- The database backup path and SHA-256 hash.
- Thumbnail destination paths.
- Whether a thumbnail was newly created.
- The location and hash of an original thumbnail backup.
- The expected hash of the replacement image.
- Whether installation of the image completed.

The script does not provide an automatic restore command.

If a database transaction fails, SQLite rolls back that transaction. However, thumbnail files may already have been created or replaced before the database update started. Database rollback does not automatically restore those filesystem changes.

Keep the database backup and the associated journal together if recovery is needed.

## Temporary Files and Cleanup

Per-book working directories and temporary Bokai conversion directories are removed after use.

The separate legacy cleanup targets:

```text
/mnt/base-us/documents
```

It deletes top-level files with a `.jpg` or `.jpeg` extension, case-insensitively.

Important:

- This cleanup does not distinguish extracted temporary images from other JPG files in that directory.
- Do not store images you want to keep in the cleanup directory.
- Subdirectories are not traversed.
- Database backups and rollback journal files are retained.

Review this path on your device before running the script, including whether it aliases another documents directory.

## Database Handling

The Kindle catalog uses SQLite features that may not be available in the bundled Python SQLite environment.

The script therefore:

- Uses `NOT INDEXED` for catalog reads.
- Avoids `PRAGMA integrity_check` and `PRAGMA quick_check`.
- Temporarily removes the table's UPDATE triggers inside the update transaction.
- Restores those triggers before committing.
- Checks that the expected number of rows was updated.
- Checks that the expected trigger names were restored.

Trigger removal, thumbnail-path updates, and trigger restoration occur within the same transaction.

## Logging

Progress appears in the terminal and is written to:

```text
/mnt/base-us/fixcovers.log
```

Each repair phase reports:

- Updated books.
- Books left unchanged because no usable cover was found.
- Errors.

Books outside the confirmed repair lists are not passed to the cover-repair function.

## Limitations and Safety Notes

- The script directly modifies the Kindle catalog and thumbnail files.
- The initial scan still reads eligible catalog entries to identify affected books; it does not repair the entire library.
- Filesystem replacement and database updates are not one atomic operation.
- The backup verification checks row counts, not full database integrity.
- The script does not automatically restore a backup after a failure.
- The legacy cleanup deletes all matching top-level JPG/JPEG files in its target directory.
- Initialization creates working directories and configures file logging before the first confirmation prompt.
- Compatibility depends on the Kindle filesystem layout, catalog schema, Python environment, and available conversion tools.

Review the configuration and keep the generated backups before using the script on your library.
