#!/mnt/us/python3/python/bin/python3
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath
from contextlib import closing
from datetime import datetime

import requests
from PIL import Image, ImageChops, ImageOps

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
DB_PATH = "/var/local/cc.db"
BACKUP_DIR = Path("/mnt/us/cc_db_backups")
THUMBNAILS_DIR = Path("/mnt/us/system/thumbnails")
TEMP_ROOT_DIR = Path("/mnt/base-us")
LOG_PATH = "/mnt/base-us/fixcovers.log"

BOKAI_BIN = "/mnt/us/extensions/bokai/bin/bokai"
BOKAI_TIMEOUT_SECONDS = 300

GOOGLE_BOOKS_SEARCH_URL = "https://www.googleapis.com/books/v1/volumes"
GOOGLE_BOOKS_COVER_URL = (
    "https://books.google.com/books/content"
    "?id={volume_id}&printsec=frontcover&img=1&zoom=1"
)
GOOGLE_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY", "")

# An existing thumbnail is valid if the image is readable and its height is
# exactly that of the observed Amazon thumbnails. Its width is ignored.
THUMBNAIL_HEIGHT = 500

# Minimum source cover dimensions. File size in bytes is not checked.
MIN_SOURCE_WIDTH = 100
MIN_SOURCE_HEIGHT = 100

# The final thumbnail is a 500 px high JPEG with quality 80.
THUMBNAIL_JPEG_QUALITY = 80
REQUEST_TIMEOUT = (10, 30)

OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


# -----------------------------------------------------------------------------
# Initialization, logging and KTerm output
# -----------------------------------------------------------------------------
TEMP_ROOT_DIR.mkdir(parents=True, exist_ok=True)
THUMBNAILS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    encoding="utf-8",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    force=True,
)
logger = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({"User-Agent": "KindleCoverFixer-GoogleBooks/1.0"})


def report(message: str, level: str = "info"):
    """Immediately display progress in KTerm and in the log."""
    print(message, flush=True)
    if level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
    else:
        logger.info(message)


# -----------------------------------------------------------------------------
# Metadata, paths and images
# -----------------------------------------------------------------------------
def normalize_text(value: str) -> str:
    if not value:
        return ""

    value = unicodedata.normalize("NFKC", str(value)).casefold().strip()
    value = re.sub(r"[\u2018\u2019\u201A\u201B]", "'", value)
    value = re.sub(r"[\u201C\u201D\u201E\u201F]", '"', value)
    value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def parse_year_from_db(raw_date):
    if raw_date is None:
        return None
    match = re.search(r"\b(\d{4})\b", str(raw_date))
    return int(match.group(1)) if match else None


def extract_year(published_date: str):
    if not published_date:
        return None
    match = re.match(r"^(\d{4})", str(published_date).strip())
    return int(match.group(1)) if match else None


def resolve_location(raw_location) -> Path | None:
    if not raw_location:
        return None
    try:
        path = Path(str(raw_location))
        return path if path.is_file() else None
    except OSError:
        return None


def path_from_value(raw_path) -> Path | None:
    if not raw_path:
        return None
    try:
        return Path(str(raw_path))
    except OSError:
        return None


def uuid_thumbnail_path(uuid_value) -> Path:
    """Final thumbnails are named exclusively after p_uuid."""
    return THUMBNAILS_DIR / f"{uuid_value}.jpg"


def image_dimensions(path: Path):
    """Return width/height only if the image can be fully decoded."""
    try:
        with Image.open(path) as image:
            image.load()
            return image.size
    except Exception:
        return None


def is_valid_source_cover(path: Path | None) -> bool:
    """A source cover must be readable and at least 100 × 100 px."""
    try:
        if path is None or not path.is_file():
            return False
        dimensions = image_dimensions(path)
        if not dimensions:
            return False
        width, height = dimensions
        return width >= MIN_SOURCE_WIDTH and height >= MIN_SOURCE_HEIGHT
    except OSError:
        return False


def is_valid_thumbnail(path: Path | None) -> bool:
    """
    An existing thumbnail is valid only if it is readable and its height
    is exactly 500 px. Its width is deliberately never checked.
    """
    try:
        if path is None or not path.is_file():
            return False
        dimensions = image_dimensions(path)
        if not dimensions:
            return False
        _, height = dimensions
        return height == THUMBNAIL_HEIGHT
    except OSError:
        return False


def copy_valid_cover(source: Path, destination: Path) -> bool:
    """Copy a source cover to the temporary directory and validate it."""
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        shutil.copy2(source, temporary)
        if not is_valid_source_cover(temporary):
            return False
        temporary.replace(destination)
        return True
    except OSError as exc:
        report(f"  Cover copy error: {exc}", "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


def is_grayscale_or_black_and_white(image: Image.Image) -> bool:
    """
    Detect a truly monochrome image, even when stored in RGB/RGBA mode.

    Modes 1 and L are inherently black-and-white or grayscale. For RGB/RGBA
    images, check that the red, green and blue channels are identical;
    no pixel list is constructed in memory.
    """
    if image.mode in ("1", "L", "I", "I;16", "F", "LA"):
        return True

    rgb = image.convert("RGB")
    red, green, blue = rgb.split()

    # An empty difference means all corresponding channel values are
    # identical: the RGB image is therefore visually grayscale.
    return ImageChops.difference(red, green).getbbox() is None and ImageChops.difference(red, blue).getbbox() is None


def write_resized_thumbnail(source_cover: Path, uuid_value) -> Path | None:
    """
    Generate /mnt/us/system/thumbnails/<p_uuid>.jpg with:
    - a height of exactly 500 px;
    - a width calculated to preserve the original aspect ratio;
    - JPEG quality 80;
    - grayscale/black-and-white appearance preserved if already monochrome;
    - conversion to grayscale only if the source is originally in color.
    """
    destination = uuid_thumbnail_path(uuid_value)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        with Image.open(source_cover) as image:
            image = ImageOps.exif_transpose(image)
            source_is_grayscale = is_grayscale_or_black_and_white(image)

            width, height = image.size
            if height <= 0:
                report("  Source image has an invalid height.", "warning")
                return None

            new_width = max(1, round(width * THUMBNAIL_HEIGHT / height))
            image = image.resize(
                (new_width, THUMBNAIL_HEIGHT),
                Image.Resampling.LANCZOS,
            )

            if source_is_grayscale:
                report("  Source image is already grayscale/black-and-white: preserving it.")
                # JPEG accepts 1/L, but special numeric modes are
                # converted to L while preserving the monochrome appearance.
                if image.mode not in ("1", "L"):
                    image = image.convert("L")
            else:
                report("  Source image is in color: converting to grayscale.")
                image = image.convert("L")

            image.save(
                temporary,
                format="JPEG",
                quality=THUMBNAIL_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )

        if not is_valid_thumbnail(temporary):
            report("  Final thumbnail is invalid after conversion.", "warning")
            return None

        temporary.replace(destination)
        final_width, final_height = image_dimensions(destination)
        report(
            f"  Thumbnail created: {destination.name} "
            f"({final_width} × {final_height}, JPEG quality {THUMBNAIL_JPEG_QUALITY})."
        )
        return destination

    except (OSError, ValueError) as exc:
        report(f"  Could not create thumbnail: {exc}", "warning")
        return None
    finally:
        temporary.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Source 1: p_cover
# -----------------------------------------------------------------------------
def get_local_cover_from_p_cover(raw_p_cover) -> Path | None:
    path = path_from_value(raw_p_cover)
    return path if is_valid_source_cover(path) else None


# -----------------------------------------------------------------------------
# Source 2: embedded cover in EPUB or an ebook converted by Bokai
# -----------------------------------------------------------------------------
def epub_member_path(opf_path: str, relative_path: str) -> str:
    return str(PurePosixPath(opf_path).parent.joinpath(PurePosixPath(relative_path)))


def find_epub_cover_member(epub: zipfile.ZipFile) -> str | None:
    """Find the declared cover in an EPUB 2 or EPUB 3 file."""
    try:
        container = ET.fromstring(epub.read("META-INF/container.xml"))
        rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
        if rootfile is None:
            return None

        opf_path = rootfile.get("full-path")
        if not opf_path:
            return None

        package = ET.fromstring(epub.read(opf_path))
        manifest = package.find(f"{{{OPF_NS}}}manifest")
        metadata = package.find(f"{{{OPF_NS}}}metadata")
        if manifest is None:
            return None

        items = {}
        for item in manifest.findall(f"{{{OPF_NS}}}item"):
            item_id = item.get("id")
            href = item.get("href")
            if item_id and href:
                items[item_id] = item

        # EPUB 3: properties="cover-image".
        for item in items.values():
            if "cover-image" in item.get("properties", "").split():
                return epub_member_path(opf_path, item.get("href"))

        # EPUB 2: <meta name="cover" content="cover-id"/>.
        if metadata is not None:
            for meta in metadata.findall(f"{{{OPF_NS}}}meta"):
                if meta.get("name") == "cover":
                    cover_item = items.get(meta.get("content"))
                    if cover_item is not None:
                        return epub_member_path(opf_path, cover_item.get("href"))

        # Last resort: an image whose manifest entry suggests a cover.
        for item in items.values():
            media_type = item.get("media-type", "")
            item_id = item.get("id", "").casefold()
            href = item.get("href", "").casefold()
            if media_type.startswith("image/") and ("cover" in item_id or "cover" in href):
                return epub_member_path(opf_path, item.get("href"))

    except (KeyError, ET.ParseError, zipfile.BadZipFile, OSError) as exc:
        report(f"  EPUB parsing error: {exc}", "warning")

    return None


def extract_cover_from_epub(epub_path: Path, destination: Path) -> bool:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(epub_path, "r") as epub:
            member = find_epub_cover_member(epub)
            if not member:
                report("  EPUB: no declared cover.")
                return False

            with epub.open(member) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=64 * 1024)

        if not is_valid_source_cover(temporary):
            report("  EPUB: cover is missing, unreadable or too small.")
            return False

        temporary.replace(destination)
        width, height = image_dimensions(destination)
        report(f"  EPUB: cover extracted ({width} × {height}).")
        return True

    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        report(f"  EPUB: extraction failed: {exc}", "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


def bokai_input_format(location: Path, mime_type) -> str | None:
    """Determine the input format to pass to Bokai."""
    suffix = location.suffix.casefold()
    mime = str(mime_type or "").casefold()

    if suffix == ".kfx" or "kfx" in mime:
        return "kfx"
    if suffix == ".epub" or "epub" in mime:
        return "epub"
    if suffix in (".azw3", ".kf8") or "azw3" in mime:
        return "azw3"
    # Bokai lists mobi as a supported format; legacy AZW files are tried this way.
    if suffix in (".mobi", ".azw") or "mobipocket" in mime or "mobi" in mime:
        return "mobi"
    return None


def convert_to_epub_with_bokai(source: Path, source_format: str, epub_output: Path) -> bool:
    """Convert KFX/AZW3/MOBI/AZW to EPUB using Bokai."""
    if not Path(BOKAI_BIN).is_file():
        report(f"  Bokai not found: {BOKAI_BIN}", "warning")
        return False

    command = [
        BOKAI_BIN,
        "convert",
        "--from",
        source_format,
        "--to",
        "epub",
        "--quiet",
        str(source),
        str(epub_output),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=BOKAI_TIMEOUT_SECONDS,
            check=False,
        )

        if result.returncode != 0:
            details = (result.stderr or result.stdout).strip()
            report(
                f"  Bokai: conversion failed (code {result.returncode})"
                + (f" : {details}" if details else ""),
                "warning",
            )
            return False

        if not epub_output.is_file() or epub_output.stat().st_size == 0:
            report("  Bokai: no usable EPUB was produced.", "warning")
            return False

        report("  Bokai: conversion completed.")
        return True

    except subprocess.TimeoutExpired:
        report("  Bokai: timed out.", "warning")
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        report(f"  Bokai: execution failed: {exc}", "warning")
        return False


def extract_cover_from_ebook(location: Path | None, mime_type, destination: Path) -> bool:
    """
    Try an embedded cover: EPUB directly; KFX/AZW3/MOBI/AZW through
    temporary Bokai conversion. All temporary Bokai files are removed.
    """
    if location is None:
        report("  Ebook file not found using p_location.")
        return False

    input_format = bokai_input_format(location, mime_type)
    if input_format is None:
        report(f"  Unsupported format: {location.suffix or 'unknown'}.")
        return False

    if input_format == "epub":
        report("  Step 2: extract the EPUB cover directly.")
        return extract_cover_from_epub(location, destination)

    temporary_dir = Path(tempfile.mkdtemp(prefix="fixcovers_bokai_", dir=str(TEMP_ROOT_DIR)))
    epub_path = temporary_dir / "book.epub"

    try:
        report(f"  Step 2: converting {input_format.upper()} → EPUB with Bokai...")
        if not convert_to_epub_with_bokai(location, input_format, epub_path):
            return False

        report("  Step 2: extracting the cover from the temporary EPUB...")
        return extract_cover_from_epub(epub_path, destination)

    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        report("  Temporary Bokai files removed.")


# -----------------------------------------------------------------------------
# Source 3: Google Books
# -----------------------------------------------------------------------------
def build_google_books_query(title: str, author: str, year: int | None) -> dict:
    parts = [f'intitle:"{title}"', f'inauthor:"{author}"']
    if year is not None:
        parts.append(f"publishedDate:{year}")

    params = {
        "q": " ".join(parts),
        "maxResults": 40,
        "printType": "books",
        "projection": "full",
    }
    if GOOGLE_API_KEY:
        params["key"] = GOOGLE_API_KEY
    return params


def authors_match(local_author: str, remote_authors: list[str]) -> bool:
    local = normalize_text(local_author)
    remote_values = [normalize_text(value) for value in remote_authors]

    if not local or not remote_values:
        return False
    if local in remote_values:
        return True

    local_parts = local.split()
    if len(local_parts) < 2:
        return False

    for remote in remote_values:
        remote_parts = remote.split()
        if len(remote_parts) >= 2 and local_parts == remote_parts[::-1]:
            return True
    return False


def publisher_matches(local_publisher: str, remote_publisher: str) -> bool:
    local = normalize_text(local_publisher)
    remote = normalize_text(remote_publisher)

    if not local or not remote:
        return False
    if local == remote:
        return True

    prefixes = (
        "edition ",
        "éditions ",
        "editions ",
        "publisher ",
        "publishing ",
    )
    for prefix in prefixes:
        if local.startswith(prefix):
            local = local[len(prefix):]
        if remote.startswith(prefix):
            remote = remote[len(prefix):]

    return bool(local) and local == remote


def best_google_image_url(item: dict) -> str | None:
    """Prefer the Google Books zoom=1 URL, then alternative images."""
    volume_id = item.get("id")
    if volume_id:
        return GOOGLE_BOOKS_COVER_URL.format(volume_id=volume_id)

    image_links = item.get("volumeInfo", {}).get("imageLinks", {})
    for key in ("extraLarge", "large", "medium", "small", "thumbnail", "smallThumbnail"):
        url = image_links.get(key)
        if url:
            return url.replace("http://", "https://", 1)
    return None


def find_google_books_cover(title: str, author: str, year: int | None, publisher: str) -> str | None:
    """Require title/author/publisher; require the year only when it is known."""
    if not title or not author or not publisher:
        report("  Google Books skipped: missing title, author or publisher.")
        return None

    try:
        response = session.get(
            GOOGLE_BOOKS_SEARCH_URL,
            params=build_google_books_query(title, author, year),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as exc:
        report(f"  Google Books: search failed: {exc}", "warning")
        return None

    local_title = normalize_text(title)

    for item in data.get("items", []):
        volume = item.get("volumeInfo", {})
        title_match = local_title == normalize_text(volume.get("title", ""))
        author_match = authors_match(author, volume.get("authors", []))
        remote_year = extract_year(volume.get("publishedDate", ""))
        year_match = year is None or remote_year == year
        publisher_match = publisher_matches(publisher, volume.get("publisher", ""))

        if all((title_match, author_match, year_match, publisher_match)):
            image_url = best_google_image_url(item)
            if image_url:
                report("  Google Books: match accepted.")
                return image_url

    report("  Google Books: no matching result.")
    return None


def download_google_cover(url: str, destination: Path) -> bool:
    """Download and validate a temporary Google Books cover."""
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").casefold()
        if not content_type.startswith("image/"):
            report(f"  Google Books: non-image response ({content_type}).", "warning")
            return False

        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    output.write(chunk)

        if not is_valid_source_cover(temporary):
            report("  Google Books: image is invalid or too small.", "warning")
            return False

        temporary.replace(destination)
        width, height = image_dimensions(destination)
        report(f"  Google Books: cover downloaded ({width} × {height}).")
        return True

    except (requests.RequestException, OSError) as exc:
        report(f"  Google Books: download failed: {exc}", "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Legacy cleanup, processing and SQLite database
# -----------------------------------------------------------------------------
def cleanup_legacy_document_jpgs():
    """
    Remove only legacy temporary JPG/JPEG files at the top level of
    /mnt/base-us/documents. Ebooks and subdirectories are left untouched;
    /mnt/us/documents is never modified.
    """
    legacy_dir = TEMP_ROOT_DIR / "documents"
    if not legacy_dir.is_dir():
        return 0

    removed = 0
    for path in legacy_dir.iterdir():
        try:
            if path.is_file() and path.suffix.casefold() in (".jpg", ".jpeg"):
                path.unlink()
                removed += 1
                report(f"Cleanup: removed {path}")
        except OSError as exc:
            report(f"Cleanup: could not remove {path} : {exc}", "warning")
    return removed


def update_thumbnail(connection, uuid_value, thumbnail_path: Path):
    """Update a single Kindle entry using p_uuid."""
    connection.execute(
        "UPDATE Entries SET p_thumbnail = ? WHERE p_uuid = ?",
        (str(thumbnail_path), uuid_value),
    )


def process_book(
    connection,
    index,
    total,
    uuid_value,
    title,
    raw_location,
    mime_type,
    raw_p_cover,
    raw_thumbnail,
    author,
    raw_date,
    publisher,
):
    report("")
    report(f"[{index}/{total}] {title}")

    existing_thumbnail = path_from_value(raw_thumbnail)
    if is_valid_thumbnail(existing_thumbnail):
        width, height = image_dimensions(existing_thumbnail)
        report(f"  Valid existing thumbnail ({width} × {height}) : book skipped.")
        return "skipped"

    report("  Current thumbnail: missing, unreadable or height differs from 500 px.")

    # All intermediate covers are removed after each book.
    work_dir = Path(tempfile.mkdtemp(prefix="fixcovers_work_", dir=str(TEMP_ROOT_DIR)))
    candidate_path = work_dir / "cover.jpg"

    try:
        report("  Step 1/3: checking p_cover...")
        local_cover = get_local_cover_from_p_cover(raw_p_cover)
        if local_cover:
            width, height = image_dimensions(local_cover)
            report(f"  Valid p_cover ({width} × {height}).")
            if copy_valid_cover(local_cover, candidate_path):
                thumbnail_path = write_resized_thumbnail(candidate_path, uuid_value)
                if thumbnail_path:
                    update_thumbnail(connection, uuid_value, thumbnail_path)
                    connection.commit()
                    report("  Kindle database updated from p_cover.")
                    return "updated"
            report("  p_cover: could not copy cover or create thumbnail.", "warning")
        else:
            report("  p_cover is missing or invalid.")

        report("  Step 2/3: looking for an embedded cover in the ebook...")
        location = resolve_location(raw_location)
        candidate_path.unlink(missing_ok=True)
        if extract_cover_from_ebook(location, mime_type, candidate_path):
            thumbnail_path = write_resized_thumbnail(candidate_path, uuid_value)
            if thumbnail_path:
                update_thumbnail(connection, uuid_value, thumbnail_path)
                connection.commit()
                report("  Kindle database updated from the embedded cover.")
                return "updated"
        else:
            report("  No usable embedded cover.")

        report("  Step 3/3: searching Google Books...")
        candidate_path.unlink(missing_ok=True)
        year = parse_year_from_db(raw_date)
        url = find_google_books_cover(title, author, year, publisher)
        if url and download_google_cover(url, candidate_path):
            thumbnail_path = write_resized_thumbnail(candidate_path, uuid_value)
            if thumbnail_path:
                update_thumbnail(connection, uuid_value, thumbnail_path)
                connection.commit()
                report("  Kindle database updated from Google Books.")
                return "updated"

        report("  No usable cover: book unchanged.")
        return "unchanged"

    except sqlite3.Error as exc:
        connection.rollback()
        report(f"  SQLite error: {exc}", "error")
        return "error"
    except Exception as exc:
        connection.rollback()
        logger.exception("Unexpected error for %r", title)
        report(f"  Unexpected error: {exc}", "error")
        return "error"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        report("  Temporary book files removed.")


def backup_database() -> Path:
    """Create and verify a SQLite snapshot before any database changes.

    Use SQLite's backup API instead of copying the live database file.
    Stop processing if the backup cannot be created or verified.
    """
    source_path = Path(DB_PATH)
    if not source_path.is_file():
        raise FileNotFoundError(f"Kindle database not found: {source_path}")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"cc.db.{timestamp}.", suffix=".tmp", dir=str(BACKUP_DIR)
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    destination = temporary_path.with_suffix(".bak")
    report(f"Backing up Kindle database: {destination}")

    try:
        with closing(sqlite3.connect(
            source_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30
        )) as source:
            with closing(sqlite3.connect(str(temporary_path))) as backup:
                source.backup(backup)
                results = backup.execute("PRAGMA integrity_check").fetchall()
                if results != [("ok",)]:
                    raise sqlite3.DatabaseError(
                        f"Backup integrity check failed: {results}"
                    )
        with temporary_path.open("rb") as backup_file:
            os.fsync(backup_file.fileno())
        temporary_path.replace(destination)
        report(f"Database backup completed and verified: {destination}")
        return destination
    except Exception:
        temporary_path.unlink(missing_ok=True)
        report("Database backup failed. No book updates will be performed.", "error")
        raise


def main():
    report("=== Fix Covers started ===")

    backup_database()

    removed = cleanup_legacy_document_jpgs()
    if removed:
        report(f"Cleanup completed: {removed} temporary JPG file(s) removed.")
    else:
        report("Cleanup: no legacy temporary JPG files found.")

    try:
        with sqlite3.connect(DB_PATH) as connection:
            rows = connection.execute(
                """
                SELECT
                    p_uuid,
                    p_titles_0_nominal,
                    p_location,
                    p_mimeType,
                    p_cover,
                    p_thumbnail,
                    p_credits_0_name_collation,
                    p_publicationDate,
                    p_publisher
                FROM Entries
                WHERE p_isArchived = 0
                  AND p_contentSize > 0
                """
            ).fetchall()
    except (sqlite3.Error, OSError) as exc:
        report(f"Could not read the Kindle database: {exc}", "error")
        raise

    total = len(rows)
    report(f"{total} books detected.")
    counts = {"updated": 0, "skipped": 0, "unchanged": 0, "error": 0}

    with sqlite3.connect(DB_PATH) as connection:
        for index, row in enumerate(rows, start=1):
            (
                uuid_value,
                raw_title,
                raw_location,
                mime_type,
                raw_p_cover,
                raw_thumbnail,
                raw_author,
                raw_date,
                raw_publisher,
            ) = row

            title = str(raw_title).strip() if raw_title else ""
            author = str(raw_author).strip() if raw_author else ""
            publisher = str(raw_publisher).strip() if raw_publisher else ""

            if not uuid_value or not title:
                report(f"[{index}/{total}] Entry has no UUID or title: skipped.", "warning")
                counts["error"] += 1
                continue

            result = process_book(
                connection,
                index,
                total,
                uuid_value,
                title,
                raw_location,
                mime_type,
                raw_p_cover,
                raw_thumbnail,
                author,
                raw_date,
                publisher,
            )
            counts[result] += 1

    report("")
    report("=== Fix Covers finished ===")
    report(
        "Summary: "
        f"{counts['updated']} updated, "
        f"{counts['skipped']} already valid, "
        f"{counts['unchanged']} unchanged, "
        f"{counts['error']} errors."
    )


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except Exception as exc:
        exit_code = 1
        logger.exception("Fatal error")
        print("", flush=True)
        print(f"FATAL ERROR: {exc}", flush=True)
    finally:
        print("", flush=True)
        print("========================================", flush=True)
        print("Processing finished.", flush=True)
        print("Press Enter to close this window.", flush=True)
        print("========================================", flush=True)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
