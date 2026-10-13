#!/mnt/us/python3/python/bin/python3
# -*- coding: utf-8 -*-
"""Fix missing, unreadable, or low-height Kindle thumbnails.

Phase 1:
  - Ask for permission.
  - Back up cc.db.
  - Find books with a missing or unreadable thumbnail.
  - Show the list and ask for confirmation.
  - Repair only the confirmed books.
  - Update cc.db.
  - Clean temporary JPG files from the documents folder.

Phase 2:
  - Find books whose existing thumbnail is readable but lower than 500 px.
  - Show the list and ask for confirmation.
  - Repair only the confirmed books.
  - Update cc.db.
  - Clean temporary JPG files from the documents folder.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path, PurePosixPath

import requests
from PIL import Image, ImageChops, ImageOps

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

DB_PATH = "/var/local/cc.db"
DB_TABLE = "Entries"
BACKUP_DIR = Path("/mnt/us/documents/ccdb_reset_backups")
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

THUMBNAIL_HEIGHT = 500
MIN_SOURCE_WIDTH = 100
MIN_SOURCE_HEIGHT = 100
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
    """Display progress in KTerm and write it to the log."""
    print(message, flush=True)

    if level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
    else:
        logger.info(message)


def qi(name):
    return '"' + name.replace('"', '""') + '"'


def ask(question: str) -> bool:
    """Ask a yes/no question. Only explicit confirmation continues."""
    try:
        answer = input(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False

    return answer in ("y", "yes")


# -----------------------------------------------------------------------------
# Database access
# -----------------------------------------------------------------------------

def open_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def count_entries(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM {} NOT INDEXED".format(qi(DB_TABLE))
    ).fetchone()[0]


def update_trigger_rows(conn, table):
    """Return UPDATE triggers, which use Kindle-specific SQL functions."""
    rows = conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='trigger' AND lower(tbl_name)=lower(?) "
        "AND sql IS NOT NULL",
        (table,),
    ).fetchall()

    result = []

    for name, sql in rows:
        header = re.split(r"\bBEGIN\b", sql, maxsplit=1, flags=re.I)[0]
        if re.search(r"\bUPDATE\b", header, flags=re.I):
            result.append((name, sql))

    return result


def read_entries():
    conn = open_db()

    try:
        return conn.execute(
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
            FROM {} NOT INDEXED
            WHERE p_isArchived = 0
              AND p_contentSize > 0
            """.format(qi(DB_TABLE))
        ).fetchall()
    finally:
        conn.close()


def apply_thumbnail_updates(updates: dict) -> int:
    """Apply thumbnail database updates in one transaction."""
    if not updates:
        return 0

    conn = open_db()
    in_transaction = False

    try:
        triggers = update_trigger_rows(conn, DB_TABLE)

        report(
            "Temporarily removing UPDATE triggers: {}".format(
                ", ".join(name for name, _ in triggers) or "none"
            )
        )

        sql = "UPDATE {} SET {}=? WHERE {}=?".format(
            qi(DB_TABLE),
            qi("p_thumbnail"),
            qi("p_uuid"),
        )

        conn.execute("BEGIN IMMEDIATE")
        in_transaction = True

        for name, _ in triggers:
            conn.execute("DROP TRIGGER {}".format(qi(name)))

        updated = 0

        for uuid_value, thumbnail_path in updates.items():
            updated += conn.execute(
                sql,
                (str(thumbnail_path), uuid_value),
            ).rowcount

        for _, trigger_sql in triggers:
            conn.execute(trigger_sql)

        if updated != len(updates):
            raise RuntimeError(
                "Unexpected number of updated rows: "
                "{}/{}".format(updated, len(updates))
            )

        restored = {
            name for name, _ in update_trigger_rows(conn, DB_TABLE)
        }

        if restored != {name for name, _ in triggers}:
            raise RuntimeError("UPDATE triggers were not restored correctly")

        conn.execute("COMMIT")
        in_transaction = False

        report("UPDATE triggers restored: {}".format(len(triggers)))
        return updated

    except Exception:
        if in_transaction:
            try:
                conn.execute("ROLLBACK")
                report("ROLLBACK completed: cc.db was not changed.", "warning")
            except Exception as rollback_error:
                report(
                    "ROLLBACK error: {}".format(rollback_error),
                    "error",
                )

        raise
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Backup and rollback journal
# -----------------------------------------------------------------------------

ROLLBACK_ROOT = TEMP_ROOT_DIR / "fixcovers_backups"
ROLLBACK_MANIFEST = None
ROLLBACK_DATA = None


def file_sha256(path):
    digest = hashlib.sha256()

    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def save_rollback_manifest():
    temporary = ROLLBACK_MANIFEST.with_suffix(".json.tmp")

    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(ROLLBACK_DATA, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())

    temporary.replace(ROLLBACK_MANIFEST)


def create_backup() -> Path:
    """Create and verify a backup of cc.db."""
    database = Path(DB_PATH)

    if not database.is_file():
        raise FileNotFoundError("Database not found: {}".format(database))

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / "cc.db.backup-{}.sqlite".format(timestamp)

    source = open_db()

    try:
        target = sqlite3.connect(str(backup_path))

        try:
            source.backup(target)
        finally:
            target.close()

        check = sqlite3.connect(str(backup_path))

        try:
            backup_count = count_entries(check)
        finally:
            check.close()

        source_count = count_entries(source)

        if backup_count != source_count:
            raise RuntimeError(
                "Backup verification failed: {} rows instead of {}".format(
                    backup_count,
                    source_count,
                )
            )

    finally:
        source.close()

    report("Backup created: {}".format(backup_path))
    return backup_path


def prepare_rollback(backup_path: Path):
    """Prepare the rollback journal for thumbnail files."""
    global ROLLBACK_MANIFEST, ROLLBACK_DATA

    ROLLBACK_ROOT.mkdir(parents=True, exist_ok=True)

    run_dir = Path(
        tempfile.mkdtemp(prefix="run_", dir=str(ROLLBACK_ROOT))
    ).resolve()

    ROLLBACK_MANIFEST = run_dir / "created_thumbnails.json"
    ROLLBACK_DATA = {
        "version": 1,
        "database": str(Path(DB_PATH).resolve()),
        "backup": str(backup_path.resolve()),
        "backup_sha256": file_sha256(backup_path),
        "thumbnails_dir": str(THUMBNAILS_DIR.resolve()),
        "restored": False,
        "images": [],
    }

    save_rollback_manifest()
    report("Rollback journal: {}".format(ROLLBACK_MANIFEST))


def record_thumbnail_before_install(destination: Path, temporary: Path):
    """Record a thumbnail before replacing or creating it."""
    if ROLLBACK_DATA is None:
        raise RuntimeError(
            "Database backup and rollback journal are not initialized."
        )

    destination = destination.absolute()

    if (
        destination.parent.resolve() != THUMBNAILS_DIR.resolve()
        or destination.is_symlink()
    ):
        raise RuntimeError(
            "Unsafe thumbnail destination: {}".format(destination)
        )

    records = ROLLBACK_DATA["images"]
    record = next(
        (
            item
            for item in records
            if item["path"] == str(destination)
        ),
        None,
    )

    if record is None:
        original = None

        if destination.exists():
            original_dir = ROLLBACK_MANIFEST.parent / "original_thumbnails"
            original_dir.mkdir(exist_ok=True)
            original = original_dir / "{}.jpg".format(len(records))
            shutil.copy2(destination, original)

        record = {
            "name": destination.name,
            "path": str(destination),
            "newly_created": original is None,
            "original_backup": str(original) if original else None,
            "original_sha256": (
                file_sha256(original) if original else None
            ),
            "installed": False,
        }

        records.append(record)

    record["expected_sha256"] = file_sha256(temporary)
    record["installed"] = False
    save_rollback_manifest()

    return record


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


def resolve_location(raw_location):
    if not raw_location:
        return None

    try:
        path = Path(str(raw_location))
        return path if path.is_file() else None
    except OSError:
        return None


def path_from_value(raw_path):
    if not raw_path:
        return None

    try:
        return Path(str(raw_path))
    except OSError:
        return None


def uuid_thumbnail_path(uuid_value) -> Path:
    return THUMBNAILS_DIR / "{}.jpg".format(uuid_value)


def image_dimensions(path: Path):
    """Return image dimensions only if the image can be decoded."""
    try:
        with Image.open(path) as image:
            image.load()
            return image.size
    except Exception:
        return None


def is_readable_image(path: Path) -> bool:
    """Return True only if the file exists and can be decoded."""
    if path is None or not path.is_file():
        return False

    return image_dimensions(path) is not None


def is_valid_source_cover(path: Path) -> bool:
    """A source cover must be readable and at least 100 x 100 px."""
    if path is None or not path.is_file():
        return False

    dimensions = image_dimensions(path)

    if not dimensions:
        return False

    width, height = dimensions
    return (
        width >= MIN_SOURCE_WIDTH
        and height >= MIN_SOURCE_HEIGHT
    )


def copy_valid_cover(source: Path, destination: Path) -> bool:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        shutil.copy2(source, temporary)

        if not is_valid_source_cover(temporary):
            return False

        temporary.replace(destination)
        return True

    except OSError as error:
        report("Cover copy error: {}".format(error), "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


def is_grayscale_or_black_and_white(image: Image.Image) -> bool:
    if image.mode in ("1", "L", "I", "I;16", "F", "LA"):
        return True

    rgb = image.convert("RGB")
    red, green, blue = rgb.split()

    return (
        ImageChops.difference(red, green).getbbox() is None
        and ImageChops.difference(red, blue).getbbox() is None
    )


def write_resized_thumbnail(
    source_cover: Path,
    uuid_value,
) -> Path | None:
    """Create a 500 px high grayscale JPEG thumbnail."""
    destination = uuid_thumbnail_path(uuid_value)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        with Image.open(source_cover) as image:
            image = ImageOps.exif_transpose(image)
            source_is_grayscale = is_grayscale_or_black_and_white(image)

            width, height = image.size

            if height <= 0:
                report("Source image has an invalid height.", "warning")
                return None

            new_width = max(
                1,
                round(width * THUMBNAIL_HEIGHT / height),
            )

            image = image.resize(
                (new_width, THUMBNAIL_HEIGHT),
                Image.Resampling.LANCZOS,
            )

            if source_is_grayscale:
                report(
                    "Source image is already grayscale/black and white: "
                    "preserving it."
                )

                if image.mode not in ("1", "L"):
                    image = image.convert("L")
            else:
                report(
                    "Source image is in color: converting to grayscale."
                )
                image = image.convert("L")

            image.save(
                temporary,
                format="JPEG",
                quality=THUMBNAIL_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )

        if not is_readable_image(temporary):
            report(
                "Final thumbnail is invalid after conversion.",
                "warning",
            )
            return None

        record = record_thumbnail_before_install(destination, temporary)
        temporary.replace(destination)
        record["installed"] = True
        save_rollback_manifest()

        final_width, final_height = image_dimensions(destination)

        report(
            "Thumbnail created: {} ({} x {}, JPEG quality {}).".format(
                destination.name,
                final_width,
                final_height,
                THUMBNAIL_JPEG_QUALITY,
            )
        )

        return destination

    except (OSError, ValueError) as error:
        report("Unable to create thumbnail: {}".format(error), "warning")
        return None
    finally:
        temporary.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Source 1: p_cover
# -----------------------------------------------------------------------------

def get_local_cover_from_p_cover(raw_p_cover):
    path = path_from_value(raw_p_cover)
    return path if is_valid_source_cover(path) else None


# -----------------------------------------------------------------------------
# Source 2: embedded EPUB cover
# -----------------------------------------------------------------------------

def epub_member_path(opf_path: str, relative_path: str) -> str:
    return str(
        PurePosixPath(opf_path).parent.joinpath(
            PurePosixPath(relative_path)
        )
    )


def find_epub_cover_member(epub: zipfile.ZipFile):
    try:
        container = ET.fromstring(epub.read("META-INF/container.xml"))
        rootfile = container.find(
            ".//{{{}}}rootfile".format(CONTAINER_NS)
        )

        if rootfile is None:
            return None

        opf_path = rootfile.get("full-path")

        if not opf_path:
            return None

        package = ET.fromstring(epub.read(opf_path))
        manifest = package.find("{{{}}}manifest".format(OPF_NS))
        metadata = package.find("{{{}}}metadata".format(OPF_NS))

        if manifest is None:
            return None

        items = {}

        for item in manifest.findall("{{{}}}item".format(OPF_NS)):
            item_id = item.get("id")
            href = item.get("href")

            if item_id and href:
                items[item_id] = item

        for item in items.values():
            properties = item.get("properties", "").split()

            if "cover-image" in properties:
                return epub_member_path(opf_path, item.get("href"))

        if metadata is not None:
            for meta in metadata.findall("{{{}}}meta".format(OPF_NS)):
                if meta.get("name") == "cover":
                    cover_item = items.get(meta.get("content"))

                    if cover_item is not None:
                        return epub_member_path(
                            opf_path,
                            cover_item.get("href"),
                        )

        for item in items.values():
            media_type = item.get("media-type", "")
            item_id = item.get("id", "").casefold()
            href = item.get("href", "").casefold()

            if media_type.startswith("image/") and (
                "cover" in item_id or "cover" in href
            ):
                return epub_member_path(opf_path, item.get("href"))

    except (
        KeyError,
        ET.ParseError,
        zipfile.BadZipFile,
        OSError,
    ) as error:
        report("EPUB parsing error: {}".format(error), "warning")

    return None


def extract_cover_from_epub(epub_path: Path, destination: Path) -> bool:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(epub_path, "r") as epub:
            member = find_epub_cover_member(epub)

            if not member:
                report("EPUB: no cover declared.")
                return False

            with epub.open(member) as source:
                with temporary.open("wb") as output:
                    shutil.copyfileobj(source, output, length=64 * 1024)

        if not is_valid_source_cover(temporary):
            report(
                "EPUB: cover missing, unreadable, or too small."
            )
            return False

        temporary.replace(destination)

        width, height = image_dimensions(destination)
        report(
            "EPUB: cover extracted ({} x {}).".format(width, height)
        )
        return True

    except (OSError, KeyError, zipfile.BadZipFile) as error:
        report(
            "EPUB: unable to extract cover: {}".format(error),
            "warning",
        )
        return False
    finally:
        temporary.unlink(missing_ok=True)


def bokai_input_format(location: Path, mime_type):
    suffix = location.suffix.casefold()
    mime = str(mime_type or "").casefold()

    if suffix == ".kfx" or "kfx" in mime:
        return "kfx"

    if suffix == ".epub" or "epub" in mime:
        return "epub"

    if suffix in (".azw3", ".kf8") or "azw3" in mime:
        return "azw3"

    if (
        suffix in (".mobi", ".azw")
        or "mobipocket" in mime
        or "mobi" in mime
    ):
        return "mobi"

    return None


def convert_to_epub_with_bokai(
    source: Path,
    source_format: str,
    epub_output: Path,
) -> bool:
    if not Path(BOKAI_BIN).is_file():
        report("Bokai not found: {}".format(BOKAI_BIN), "warning")
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
                "Bokai: conversion failed (code {}).{}".format(
                    result.returncode,
                    " {}".format(details) if details else "",
                ),
                "warning",
            )
            return False

        if not epub_output.is_file() or epub_output.stat().st_size == 0:
            report("Bokai: no usable EPUB produced.", "warning")
            return False

        report("Bokai: conversion completed.")
        return True

    except subprocess.TimeoutExpired:
        report("Bokai: timed out.", "warning")
        return False
    except (OSError, subprocess.SubprocessError) as error:
        report("Bokai: unable to run: {}".format(error), "warning")
        return False


def extract_cover_from_ebook(
    location: Path | None,
    mime_type,
    destination: Path,
) -> bool:
    if location is None:
        report("E-book file not found through p_location.")
        return False

    input_format = bokai_input_format(location, mime_type)

    if input_format is None:
        report(
            "Unsupported format: {}.".format(
                location.suffix or "unknown"
            )
        )
        return False

    if input_format == "epub":
        report("Step 2: extracting the EPUB cover directly.")
        return extract_cover_from_epub(location, destination)

    temporary_dir = Path(
        tempfile.mkdtemp(
            prefix="fixcovers_bokai_",
            dir=str(TEMP_ROOT_DIR),
        )
    )
    epub_path = temporary_dir / "book.epub"

    try:
        report(
            "Step 2: converting {} to EPUB with Bokai...".format(
                input_format.upper()
            )
        )

        if not convert_to_epub_with_bokai(
            location,
            input_format,
            epub_path,
        ):
            return False

        report(
            "Step 2: extracting the cover from the temporary EPUB..."
        )
        return extract_cover_from_epub(epub_path, destination)

    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        report("Temporary Bokai files deleted.")


# -----------------------------------------------------------------------------
# Source 3: Google Books
# -----------------------------------------------------------------------------

def build_google_books_query(title, author, year):
    parts = [
        'intitle:"{}"'.format(title),
        'inauthor:"{}"'.format(author),
    ]

    if year is not None:
        parts.append("publishedDate:{}".format(year))

    params = {
        "q": " ".join(parts),
        "maxResults": 40,
        "printType": "books",
        "projection": "full",
    }

    if GOOGLE_API_KEY:
        params["key"] = GOOGLE_API_KEY

    return params


def authors_match(local_author: str, remote_authors: list) -> bool:
    local = normalize_text(local_author)
    remote_values = [
        normalize_text(value) for value in remote_authors
    ]

    if not local or not remote_values:
        return False

    if local in remote_values:
        return True

    local_parts = local.split()

    if len(local_parts) < 2:
        return False

    for remote in remote_values:
        remote_parts = remote.split()

        if (
            len(remote_parts) >= 2
            and local_parts == remote_parts[::-1]
        ):
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
        "édition ",
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


def best_google_image_url(item: dict):
    volume_id = item.get("id")

    if volume_id:
        return GOOGLE_BOOKS_COVER_URL.format(volume_id=volume_id)

    image_links = item.get("volumeInfo", {}).get("imageLinks", {})

    for key in (
        "extraLarge",
        "large",
        "medium",
        "small",
        "thumbnail",
        "smallThumbnail",
    ):
        url = image_links.get(key)

        if url:
            return url.replace("http://", "https://", 1)

    return None


def find_google_books_cover(title, author, year, publisher):
    if not title or not author or not publisher:
        report(
            "Google Books skipped: missing title, author, or publisher."
        )
        return None

    try:
        response = session.get(
            GOOGLE_BOOKS_SEARCH_URL,
            params=build_google_books_query(title, author, year),
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()

    except (requests.RequestException, ValueError) as error:
        report(
            "Google Books: search failed: {}".format(error),
            "warning",
        )
        return None

    local_title = normalize_text(title)

    for item in data.get("items", []):
        volume = item.get("volumeInfo", {})

        title_match = local_title == normalize_text(
            volume.get("title", "")
        )
        author_match = authors_match(
            author,
            volume.get("authors", []),
        )
        remote_year = extract_year(volume.get("publishedDate", ""))
        year_match = year is None or remote_year == year
        publisher_match = publisher_matches(
            publisher,
            volume.get("publisher", ""),
        )

        if all(
            (
                title_match,
                author_match,
                year_match,
                publisher_match,
            )
        ):
            image_url = best_google_image_url(item)

            if image_url:
                report("Google Books: match accepted.")
                return image_url

    report("Google Books: no matching result.")
    return None


def download_google_cover(url: str, destination: Path) -> bool:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            stream=True,
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).casefold()

        if not content_type.startswith("image/"):
            report(
                "Google Books: non-image response ({}).".format(
                    content_type
                ),
                "warning",
            )
            return False

        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    output.write(chunk)

        if not is_valid_source_cover(temporary):
            report(
                "Google Books: image invalid or too small.",
                "warning",
            )
            return False

        temporary.replace(destination)

        width, height = image_dimensions(destination)
        report(
            "Google Books: cover downloaded ({} x {}).".format(
                width,
                height,
            )
        )
        return True

    except (requests.RequestException, OSError) as error:
        report(
            "Google Books: download failed: {}".format(error),
            "warning",
        )
        return False
    finally:
        temporary.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Temporary JPG cleanup
# -----------------------------------------------------------------------------

def cleanup_legacy_document_jpgs() -> int:
    """Delete temporary JPG/JPEG files in /mnt/base-us/documents."""
    legacy_dir = TEMP_ROOT_DIR / "documents"

    if not legacy_dir.is_dir():
        return 0

    removed = 0

    for path in legacy_dir.iterdir():
        try:
            if (
                path.is_file()
                and path.suffix.casefold() in (".jpg", ".jpeg")
            ):
                path.unlink()
                removed += 1
                report("Cleanup: deleted {}".format(path))

        except OSError as error:
            report(
                "Cleanup: unable to delete {}: {}".format(
                    path,
                    error,
                ),
                "warning",
            )

    return removed


# -----------------------------------------------------------------------------
# Book processing
# -----------------------------------------------------------------------------

def process_book(
    index,
    total,
    uuid_value,
    title,
    raw_location,
    mime_type,
    raw_p_cover,
    author,
    raw_date,
    publisher,
):
    """Repair one book and return (status, thumbnail_path_or_None)."""
    report("")
    report("[{}/{}] {}".format(index, total, title))

    work_dir = Path(
        tempfile.mkdtemp(
            prefix="fixcovers_work_",
            dir=str(TEMP_ROOT_DIR),
        )
    )
    candidate_path = work_dir / "cover.jpg"

    try:
        report("Step 1/3: checking p_cover...")

        local_cover = get_local_cover_from_p_cover(raw_p_cover)

        if local_cover:
            width, height = image_dimensions(local_cover)
            report(
                "p_cover valid ({} x {}).".format(width, height)
            )

            if copy_valid_cover(local_cover, candidate_path):
                thumbnail_path = write_resized_thumbnail(
                    candidate_path,
                    uuid_value,
                )

                if thumbnail_path:
                    report("Thumbnail ready from p_cover.")
                    return "updated", thumbnail_path

            report(
                "p_cover: unable to copy cover or create thumbnail.",
                "warning",
            )
        else:
            report("p_cover missing or invalid.")

        report(
            "Step 2/3: looking for a cover embedded in the e-book file..."
        )

        location = resolve_location(raw_location)
        candidate_path.unlink(missing_ok=True)

        if extract_cover_from_ebook(
            location,
            mime_type,
            candidate_path,
        ):
            thumbnail_path = write_resized_thumbnail(
                candidate_path,
                uuid_value,
            )

            if thumbnail_path:
                report(
                    "Thumbnail ready from the embedded cover."
                )
                return "updated", thumbnail_path
        else:
            report("No usable embedded cover.")

        report("Step 3/3: searching Google Books...")

        candidate_path.unlink(missing_ok=True)
        year = parse_year_from_db(raw_date)

        url = find_google_books_cover(
            title,
            author,
            year,
            publisher,
        )

        if url and download_google_cover(url, candidate_path):
            thumbnail_path = write_resized_thumbnail(
                candidate_path,
                uuid_value,
            )

            if thumbnail_path:
                report("Thumbnail ready from Google Books.")
                return "updated", thumbnail_path

        report("No usable cover: book unchanged.")
        return "unchanged", None

    except Exception as error:
        logger.exception("Unexpected error for %r", title)
        report("Unexpected error: {}".format(error), "error")
        return "error", None

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        report("Temporary book files deleted.")


# -----------------------------------------------------------------------------
# Selection helpers
# -----------------------------------------------------------------------------

def format_book_row(index, total, title, author):
    line = "[{}/{}] {}".format(index, total, title)

    if author:
        line += " — {}".format(author)

    return line


def select_missing_or_unreadable(rows):
    """Select books with no thumbnail or an unreadable thumbnail file."""
    selected = []

    for row in rows:
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

        if not uuid_value or not title:
            continue

        thumbnail_path = path_from_value(raw_thumbnail)

        if not is_readable_image(thumbnail_path):
            selected.append(
                {
                    "uuid": uuid_value,
                    "title": title,
                    "author": author,
                    "location": raw_location,
                    "mime_type": mime_type,
                    "p_cover": raw_p_cover,
                    "thumbnail": raw_thumbnail,
                    "date": raw_date,
                    "publisher": raw_publisher,
                    "reason": (
                        "missing"
                        if thumbnail_path is None
                        else "unreadable"
                    ),
                }
            )

    return selected


def select_low_height(rows):
    """Select readable thumbnails with a height below 500 px."""
    selected = []

    for row in rows:
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

        if not uuid_value or not title:
            continue

        thumbnail_path = path_from_value(raw_thumbnail)
        dimensions = image_dimensions(thumbnail_path)

        if not dimensions:
            continue

        width, height = dimensions

        if height < THUMBNAIL_HEIGHT:
            selected.append(
                {
                    "uuid": uuid_value,
                    "title": title,
                    "author": author,
                    "location": raw_location,
                    "mime_type": mime_type,
                    "p_cover": raw_p_cover,
                    "thumbnail": raw_thumbnail,
                    "date": raw_date,
                    "publisher": raw_publisher,
                    "height": height,
                }
            )

    return selected


def show_selected_books(books, reason_key=None):
    report("")
    report("Books selected: {}".format(len(books)))

    for index, book in enumerate(books, start=1):
        line = format_book_row(
            index,
            len(books),
            book["title"],
            book["author"],
        )

        if reason_key and reason_key in book:
            line += " [{}]".format(book[reason_key])

        report(line)


def run_repair_phase(books, phase_name: str) -> bool:
    """Repair a confirmed list of books and update cc.db."""
    if not books:
        report("No books to repair in {}.".format(phase_name))
        return True

    show_selected_books(books, "reason" if phase_name == "phase 1" else None)

    if not ask(
        "\nDo you want to repair these {} books? [y/N]: ".format(
            len(books)
        )
    ):
        report("{} skipped by user.".format(phase_name.capitalize()))
        return True

    counts = {
        "updated": 0,
        "unchanged": 0,
        "error": 0,
    }
    pending_updates = {}

    for index, book in enumerate(books, start=1):
        status, thumbnail_path = process_book(
            index,
            len(books),
            book["uuid"],
            book["title"],
            book["location"],
            book["mime_type"],
            book["p_cover"],
            book["author"],
            book["date"],
            book["publisher"],
        )

        if status == "updated":
            pending_updates[book["uuid"]] = str(thumbnail_path)
        else:
            counts[status] += 1

    success = True

    if pending_updates:
        report("")
        report(
            "Updating cc.db with {} thumbnail(s)...".format(
                len(pending_updates)
            )
        )

        try:
            counts["updated"] = apply_thumbnail_updates(pending_updates)
        except Exception as error:
            success = False
            counts["error"] += len(pending_updates)
            logger.exception("Database update failed")
            report("ERROR: {}".format(error), "error")
            report(
                "Backup: {}".format(ROLLBACK_DATA["backup"])
            )

    report("")
    report(
        "{} summary: {} updated, {} unchanged, {} errors.".format(
            phase_name.capitalize(),
            counts["updated"],
            counts["unchanged"],
            counts["error"],
        )
    )

    if success:
        removed = cleanup_legacy_document_jpgs()

        if removed:
            report(
                "Cleanup completed: {} temporary JPG file(s) deleted.".format(
                    removed
                )
            )
        else:
            report("Cleanup: no temporary JPG files found.")

    return success


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

def main():
    report("=== Fix Covers started ===")
    report("Database: {}".format(DB_PATH))

    if not os.path.isfile(DB_PATH):
        report(
            "ERROR: database not found: {}".format(DB_PATH),
            "error",
        )
        return 1

    if not ask(
        "Do you want to back up cc.db and scan for books with "
        "missing or unreadable thumbnails? [y/N]: "
    ):
        report("Operation cancelled by user.")
        return 0

    try:
        backup_path = create_backup()
    except Exception as error:
        report("ERROR: backup failed: {}".format(error), "error")
        return 1

    prepare_rollback(backup_path)

    try:
        rows = read_entries()
    except (sqlite3.Error, OSError) as error:
        report(
            "Unable to read the Kindle database: {}".format(error),
            "error",
        )
        raise

    report("Books detected in cc.db: {}".format(len(rows)))

    missing_books = select_missing_or_unreadable(rows)

    if not missing_books:
        report("No books with missing or unreadable thumbnails.")
    else:
        if not run_repair_phase(missing_books, "phase 1"):
            return 1

    low_height_books = select_low_height(rows)

    if not low_height_books:
        report("No readable thumbnails with a height below 500 px.")
    else:
        report("")
        report(
            "Found {} readable thumbnail(s) with a height below {} px.".format(
                len(low_height_books),
                THUMBNAIL_HEIGHT,
            )
        )

        if not run_repair_phase(low_height_books, "phase 2"):
            return 1

    report("")
    report("=== Fix Covers finished ===")

    if ask("\nRestart the Kindle now? [y/N]: "):
        report("Restarting...")
        sys.stdout.flush()
        subprocess.call(["reboot"])
    else:
        report(
            "Remember to restart the Kindle to reload the catalog."
        )

    return 0


if __name__ == "__main__":
    exit_code = 0

    try:
        exit_code = main()

    except Exception as error:
        exit_code = 1
        logger.exception("Fatal error")

        print("", flush=True)
        print("FATAL ERROR: {}".format(error), flush=True)

    finally:
        print("", flush=True)
        print("========================================", flush=True)
        print("Processing completed.", flush=True)
        print("Press Enter to close this window.", flush=True)
        print("========================================", flush=True)

        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass

        sys.exit(exit_code)