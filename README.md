# Kindle Covers Fixer

A KUAL extension that repairs missing or invalid Kindle cover thumbnails using local cover images, embedded ebook covers, and Google Books as a fallback.

The extension runs in KTerm, displays progress for each book, and writes a log for troubleshooting. The current menu label is `Fix Covers`; runtime messages are in French.

## Features

- Skips existing thumbnails that are fully readable and exactly 500 pixels high.
- Tries three cover sources in order: `p_cover`, an embedded ebook cover, then Google Books.
- Extracts covers directly from EPUB files.
- Uses Bokai to convert KFX, AZW3/KF8, and MOBI/AZW files to temporary EPUB files for cover extraction.
- Generates grayscale JPEG thumbnails with a height of 500 pixels, proportional width, and quality 80.
- Saves generated thumbnails as `/mnt/us/system/thumbnails/<p_uuid>.jpg`.
- Updates the corresponding `Entries.p_thumbnail` database field.
- Removes temporary working files after processing each book.
- Reports totals for updated, already valid, unchanged, and failed entries.

## Warning

This script modifies `/var/local/cc.db` and writes to the Kindle thumbnail directory. It does not create an automatic database backup, provide a restore command, or ask for confirmation before processing.

Back up your database and thumbnail directory before use. Database updates are committed per book, so changes made earlier in a run remain if a later book fails. A database rollback does not undo a thumbnail file already written.

At startup, the script also deletes top-level `.jpg` and `.jpeg` files in `/mnt/base-us/documents` as legacy temporary-file cleanup. It does not check whether those images were actually created by this extension. Review or back up that directory before running the script.

The conversion workflow is intended for DRM-free sideloaded books. This extension does not implement DRM removal.

## Requirements

- A Kindle configured to run KUAL extensions.
- KUAL installed.
- KTerm installed at `/mnt/us/extensions/kterm`.
- Python 3.10 or newer at `/usr/bin/python3` for the supplied launcher and type annotations.
- The `requests` and `Pillow` Python packages, with a Pillow version that provides `Image.Resampling.LANCZOS`.
- Access to the Kindle database and write access to the thumbnail and temporary directories.

Optional dependencies:

- Bokai at `/mnt/us/extensions/bokai/bin/bokai` for embedded-cover extraction from supported non-EPUB formats.
- Internet access for Google Books searches and downloads.
- A Google Books API key, supplied through `GOOGLE_BOOKS_API_KEY`, if desired.

Bokai is not needed for direct EPUB extraction or for a usable local `p_cover`. Google Books is attempted only after local sources fail.

Compatibility depends on the device environment and database schema. No tested device or firmware compatibility list is supplied with these files.

## Installation

Copy the extension files to the following structure:

```text
/mnt/us/extensions/fixcovers/
├── config.xml
├── menu.json
└── bin/
    ├── run_in_kterm.sh
    └── fix_covers.py
```

The folder name and paths matter: the launcher uses hard-coded paths.

From a Kindle shell, make the launcher executable:

```sh
chmod +x /mnt/us/extensions/fixcovers/bin/run_in_kterm.sh
```

Ensure that `/usr/bin/python3` can import the required packages:

```sh
/usr/bin/python3 -c "import requests; from PIL import Image; print(Image.Resampling.LANCZOS)"
```

If your Python installation uses another path, edit `PYTHON_BIN` in `bin/run_in_kterm.sh`. Install the dependencies in that interpreter's environment.

## Usage

### From KUAL

1. Open KUAL.
2. Select `Fix Covers`.
3. Follow progress in KTerm.
4. Review the final summary.
5. Press Enter to close the terminal window.

Processing starts immediately; there is no confirmation prompt.

### From a terminal

```sh
/usr/bin/python3 /mnt/us/extensions/fixcovers/bin/fix_covers.py
```

The script uses configuration constants rather than command-line options. It does not offer a dry-run mode, book selector, or backup/restore mode.

### Optional Google Books API key

For a direct terminal run:

```sh
export GOOGLE_BOOKS_API_KEY="YOUR_API_KEY"
/usr/bin/python3 /mnt/us/extensions/fixcovers/bin/fix_covers.py
```

For a KUAL launch, the variable must be present in the launcher's environment. Exporting it in a separate terminal session does not configure that environment automatically.

## How it works

### 1. Select database entries

The script reads `Entries` from `/var/local/cc.db` using:

```sql
WHERE p_isArchived = 0
  AND p_contentSize > 0
```

Entries without a UUID or title are counted as errors and skipped. Selection is based on these database fields, not on a separate DRM or sideloaded-book check.

### 2. Validate the current thumbnail

A thumbnail is accepted if the image can be fully decoded and its height is exactly 500 pixels. Its width is not checked.

An accepted thumbnail is left unchanged, even if it is not named after the UUID or does not show the expected cover artwork.

### 3. Try cover sources in order

1. Local `p_cover`: use the referenced image if it is readable and at least 100 × 100 pixels.
2. Embedded cover: read EPUB cover declarations, or convert a supported non-EPUB file to a temporary EPUB using Bokai before extracting its cover.
3. Google Books: search using the title, author, and publication year when available, then require matching metadata before downloading a cover.

Google Books requires a title, author, and publisher. Matching uses normalized titles, author matching with a reversed-name allowance, publisher matching with limited prefix normalization, and an exact year match when the local year is known. This conservative matching can leave some books unchanged.

### 4. Generate and register a thumbnail

The chosen cover is orientation-corrected, resized to 500 pixels high while preserving its aspect ratio, and saved as a grayscale JPEG at quality 80. Already monochrome images remain monochrome, but are still resized and re-encoded.

The thumbnail is written to:

```text
/mnt/us/system/thumbnails/<p_uuid>.jpg
```

The script updates `p_thumbnail` for that UUID and commits the database change. If no usable cover is found, the book is left unchanged.

## Files

| File | Purpose |
|---|---|
| `config.xml` | KUAL extension metadata; version 1.0. |
| `menu.json` | Defines the `Fix Covers` menu action. |
| `bin/run_in_kterm.sh` | Checks KTerm and script paths, then starts Python inside KTerm. |
| `bin/fix_covers.py` | Finds covers, generates thumbnails, updates the database, and logs progress. |

## Configuration and paths

| Setting | Default |
|---|---|
| Database | `/var/local/cc.db` |
| Thumbnail directory | `/mnt/us/system/thumbnails` |
| Temporary working root | `/mnt/base-us` |
| Log file | `/mnt/base-us/fixcovers.log` |
| Bokai executable | `/mnt/us/extensions/bokai/bin/bokai` |
| Bokai conversion timeout | 300 seconds |
| Thumbnail height | 500 pixels |
| JPEG quality | 80 |
| Minimum source dimensions | 100 × 100 pixels |
| HTTP connect/read timeouts | 10 / 30 seconds |

Edit the constants in `bin/fix_covers.py` to change these settings. Launcher paths are configured separately in `bin/run_in_kterm.sh`.

## Troubleshooting

- KTerm is missing: check that `/mnt/us/extensions/kterm/bin/kterm.sh` exists and is executable.
- Fix Covers script is missing: verify the installation structure and folder name.
- Python or imports fail: check the interpreter path and install `requests` and Pillow for that interpreter.
- Bokai extraction fails: verify the executable path, supported input format, and conversion output in the log. A conversion can time out after 300 seconds.
- Google Books finds nothing: check connectivity and metadata. Missing author or publisher information prevents this fallback, and strict matching can reject other editions.
- A wrong cover is skipped: the existing-thumbnail check validates decoding and height, not the artwork itself.
- Database operations fail: inspect the log for SQLite errors and confirm that the database contains the expected columns. The script does not implement the trigger-removal workaround used by some other Kindle tools.

The script does not restart the Kindle or explicitly refresh the library interface after processing.

## Privacy

When the Google Books fallback is used, the script sends search metadata, including title, author, and publication year when available, to Google Books and downloads a cover image. Local-source processing does not require these requests.

## License

No license file was included with the supplied extension files. Add a `LICENSE` file specifying your chosen terms before distributing this project.
