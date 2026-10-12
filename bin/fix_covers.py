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

import requests
from PIL import Image, ImageChops, ImageOps

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
DB_PATH = "/var/local/cc.db"
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

# Un thumbnail existant est valide si l'image est lisible et si sa hauteur est
# exactement celle des thumbnails Amazon observés. Sa largeur est ignorée.
THUMBNAIL_HEIGHT = 500

# Taille minimale pour une couverture source. Aucun test de taille en octets.
MIN_SOURCE_WIDTH = 100
MIN_SOURCE_HEIGHT = 100

# Le thumbnail final est un JPEG de hauteur 500 px, qualité 80.
THUMBNAIL_JPEG_QUALITY = 80
REQUEST_TIMEOUT = (10, 30)

OPF_NS = "http://www.idpf.org/2007/opf"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"


# -----------------------------------------------------------------------------
# Initialisation, journal et affichage KTerm
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
    """Affiche immédiatement la progression dans KTerm et dans le journal."""
    print(message, flush=True)
    if level == "warning":
        logger.warning(message)
    elif level == "error":
        logger.error(message)
    else:
        logger.info(message)


# -----------------------------------------------------------------------------
# Métadonnées, chemins et images
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
    """Les thumbnails finalisés portent exclusivement le nom du p_uuid."""
    return THUMBNAILS_DIR / f"{uuid_value}.jpg"


def image_dimensions(path: Path):
    """Retourne largeur/hauteur seulement si l'image est intégralement décodable."""
    try:
        with Image.open(path) as image:
            image.load()
            return image.size
    except Exception:
        return None


def is_valid_source_cover(path: Path | None) -> bool:
    """Une couverture source doit être lisible et au moins 100 × 100 px."""
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
    Un thumbnail existant est valide seulement s'il est lisible et si sa hauteur
    est exactement 500 px. Sa largeur n'est volontairement jamais vérifiée.
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
    """Copie une couverture source dans le répertoire temporaire et la valide."""
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        shutil.copy2(source, temporary)
        if not is_valid_source_cover(temporary):
            return False
        temporary.replace(destination)
        return True
    except OSError as exc:
        report(f"  Erreur de copie de couverture : {exc}", "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


def is_grayscale_or_black_and_white(image: Image.Image) -> bool:
    """
    Détecte une image réellement monochrome, même lorsqu'elle est enregistrée
    dans un conteneur RGB/RGBA.

    Les modes 1 et L sont intrinsèquement noir/blanc ou niveaux de gris. Pour
    les images RGB/RGBA, la fonction vérifie que les canaux rouge, vert et bleu
    sont identiques; aucune liste de pixels n'est construite en mémoire.
    """
    if image.mode in ("1", "L", "I", "I;16", "F", "LA"):
        return True

    rgb = image.convert("RGB")
    red, green, blue = rgb.split()

    # Une différence vide signifie que toutes les valeurs de chaque canal sont
    # identiques : l'image RGB est donc visuellement en niveaux de gris.
    return ImageChops.difference(red, green).getbbox() is None and ImageChops.difference(red, blue).getbbox() is None


def write_resized_thumbnail(source_cover: Path, uuid_value) -> Path | None:
    """
    Génère /mnt/us/system/thumbnails/<p_uuid>.jpg avec :
    - hauteur exactement 500 px;
    - largeur calculée pour garder le ratio original;
    - JPEG de qualité 80;
    - conservation de l'image telle quelle si elle est déjà en gris/noir-blanc;
    - conversion en niveaux de gris seulement si elle est initialement couleur.
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
                report("  Image source avec hauteur invalide.", "warning")
                return None

            new_width = max(1, round(width * THUMBNAIL_HEIGHT / height))
            image = image.resize(
                (new_width, THUMBNAIL_HEIGHT),
                Image.Resampling.LANCZOS,
            )

            if source_is_grayscale:
                report("  Image source déjà en niveaux de gris/noir et blanc : conservation.")
                # JPEG accepte 1/L, mais des modes numériques particuliers sont
                # convertis vers L sans modifier le caractère monochrome.
                if image.mode not in ("1", "L"):
                    image = image.convert("L")
            else:
                report("  Image source en couleur : conversion en niveaux de gris.")
                image = image.convert("L")

            image.save(
                temporary,
                format="JPEG",
                quality=THUMBNAIL_JPEG_QUALITY,
                optimize=True,
                progressive=True,
            )

        if not is_valid_thumbnail(temporary):
            report("  Thumbnail final invalide après conversion.", "warning")
            return None

        temporary.replace(destination)
        final_width, final_height = image_dimensions(destination)
        report(
            f"  Thumbnail créé : {destination.name} "
            f"({final_width} × {final_height}, JPEG qualité {THUMBNAIL_JPEG_QUALITY})."
        )
        return destination

    except (OSError, ValueError) as exc:
        report(f"  Création du thumbnail impossible : {exc}", "warning")
        return None
    finally:
        temporary.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Source 1 : p_cover
# -----------------------------------------------------------------------------
def get_local_cover_from_p_cover(raw_p_cover) -> Path | None:
    path = path_from_value(raw_p_cover)
    return path if is_valid_source_cover(path) else None


# -----------------------------------------------------------------------------
# Source 2 : couverture intégrée à EPUB ou ebook converti par Bokai
# -----------------------------------------------------------------------------
def epub_member_path(opf_path: str, relative_path: str) -> str:
    return str(PurePosixPath(opf_path).parent.joinpath(PurePosixPath(relative_path)))


def find_epub_cover_member(epub: zipfile.ZipFile) -> str | None:
    """Trouve la couverture déclarée dans un EPUB 2 ou EPUB 3."""
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

        # EPUB 3 : properties="cover-image".
        for item in items.values():
            if "cover-image" in item.get("properties", "").split():
                return epub_member_path(opf_path, item.get("href"))

        # EPUB 2 : <meta name="cover" content="cover-id"/>.
        if metadata is not None:
            for meta in metadata.findall(f"{{{OPF_NS}}}meta"):
                if meta.get("name") == "cover":
                    cover_item = items.get(meta.get("content"))
                    if cover_item is not None:
                        return epub_member_path(opf_path, cover_item.get("href"))

        # Dernier recours : image dont le manifest suggère une couverture.
        for item in items.values():
            media_type = item.get("media-type", "")
            item_id = item.get("id", "").casefold()
            href = item.get("href", "").casefold()
            if media_type.startswith("image/") and ("cover" in item_id or "cover" in href):
                return epub_member_path(opf_path, item.get("href"))

    except (KeyError, ET.ParseError, zipfile.BadZipFile, OSError) as exc:
        report(f"  Erreur pendant l'analyse EPUB : {exc}", "warning")

    return None


def extract_cover_from_epub(epub_path: Path, destination: Path) -> bool:
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        with zipfile.ZipFile(epub_path, "r") as epub:
            member = find_epub_cover_member(epub)
            if not member:
                report("  EPUB : aucune couverture déclarée.")
                return False

            with epub.open(member) as source, temporary.open("wb") as output:
                shutil.copyfileobj(source, output, length=64 * 1024)

        if not is_valid_source_cover(temporary):
            report("  EPUB : couverture absente, illisible ou trop petite.")
            return False

        temporary.replace(destination)
        width, height = image_dimensions(destination)
        report(f"  EPUB : couverture extraite ({width} × {height}).")
        return True

    except (OSError, KeyError, zipfile.BadZipFile) as exc:
        report(f"  EPUB : extraction impossible : {exc}", "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


def bokai_input_format(location: Path, mime_type) -> str | None:
    """Détermine le format à transmettre à Bokai."""
    suffix = location.suffix.casefold()
    mime = str(mime_type or "").casefold()

    if suffix == ".kfx" or "kfx" in mime:
        return "kfx"
    if suffix == ".epub" or "epub" in mime:
        return "epub"
    if suffix in (".azw3", ".kf8") or "azw3" in mime:
        return "azw3"
    # Bokai annonce mobi comme format supporté; les AZW historiques sont testés ainsi.
    if suffix in (".mobi", ".azw") or "mobipocket" in mime or "mobi" in mime:
        return "mobi"
    return None


def convert_to_epub_with_bokai(source: Path, source_format: str, epub_output: Path) -> bool:
    """Convertit KFX/AZW3/MOBI/AZW vers EPUB au moyen de Bokai."""
    if not Path(BOKAI_BIN).is_file():
        report(f"  Bokai introuvable : {BOKAI_BIN}", "warning")
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
                f"  Bokai : conversion échouée (code {result.returncode})"
                + (f" : {details}" if details else ""),
                "warning",
            )
            return False

        if not epub_output.is_file() or epub_output.stat().st_size == 0:
            report("  Bokai : aucun EPUB utilisable produit.", "warning")
            return False

        report("  Bokai : conversion terminée.")
        return True

    except subprocess.TimeoutExpired:
        report("  Bokai : délai dépassé.", "warning")
        return False
    except (OSError, subprocess.SubprocessError) as exc:
        report(f"  Bokai : exécution impossible : {exc}", "warning")
        return False


def extract_cover_from_ebook(location: Path | None, mime_type, destination: Path) -> bool:
    """
    Essaie une couverture intégrée : EPUB directement; KFX/AZW3/MOBI/AZW via
    conversion temporaire Bokai. Tous les fichiers temporaires Bokai sont supprimés.
    """
    if location is None:
        report("  Fichier e-book introuvable via p_location.")
        return False

    input_format = bokai_input_format(location, mime_type)
    if input_format is None:
        report(f"  Format non pris en charge : {location.suffix or 'inconnu'}.")
        return False

    if input_format == "epub":
        report("  Étape 2 : extraction directe de la couverture EPUB.")
        return extract_cover_from_epub(location, destination)

    temporary_dir = Path(tempfile.mkdtemp(prefix="fixcovers_bokai_", dir=str(TEMP_ROOT_DIR)))
    epub_path = temporary_dir / "book.epub"

    try:
        report(f"  Étape 2 : conversion {input_format.upper()} → EPUB avec Bokai…")
        if not convert_to_epub_with_bokai(location, input_format, epub_path):
            return False

        report("  Étape 2 : extraction de la couverture depuis l'EPUB temporaire…")
        return extract_cover_from_epub(epub_path, destination)

    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        report("  Fichiers temporaires Bokai supprimés.")


# -----------------------------------------------------------------------------
# Source 3 : Google Books
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
    """Privilégie l'URL Google Books zoom=1, puis les images alternatives."""
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
    """Exige titre/auteur/éditeur; l'année est exigée seulement lorsqu'elle est connue."""
    if not title or not author or not publisher:
        report("  Google Books ignoré : titre, auteur ou éditeur manquant.")
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
        report(f"  Google Books : recherche échouée : {exc}", "warning")
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
                report("  Google Books : correspondance acceptée.")
                return image_url

    report("  Google Books : aucune correspondance conforme.")
    return None


def download_google_cover(url: str, destination: Path) -> bool:
    """Télécharge et valide une couverture Google Books temporaire."""
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)

    try:
        response = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").casefold()
        if not content_type.startswith("image/"):
            report(f"  Google Books : réponse non-image ({content_type}).", "warning")
            return False

        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if chunk:
                    output.write(chunk)

        if not is_valid_source_cover(temporary):
            report("  Google Books : image invalide ou trop petite.", "warning")
            return False

        temporary.replace(destination)
        width, height = image_dimensions(destination)
        report(f"  Google Books : couverture téléchargée ({width} × {height}).")
        return True

    except (requests.RequestException, OSError) as exc:
        report(f"  Google Books : téléchargement échoué : {exc}", "warning")
        return False
    finally:
        temporary.unlink(missing_ok=True)


# -----------------------------------------------------------------------------
# Nettoyage historique, traitement et base SQLite
# -----------------------------------------------------------------------------
def cleanup_legacy_document_jpgs():
    """
    Supprime uniquement les JPG/JPEG temporaires historiques placés au premier
    niveau de /mnt/base-us/documents. Les e-books et les sous-répertoires ne
    sont pas touchés; /mnt/us/documents n'est jamais modifié.
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
                report(f"Nettoyage : supprimé {path}")
        except OSError as exc:
            report(f"Nettoyage : impossible de supprimer {path} : {exc}", "warning")
    return removed


def update_thumbnail(connection, uuid_value, thumbnail_path: Path):
    """Met à jour une seule entrée Kindle en utilisant p_uuid."""
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
        report(f"  Thumbnail existant valide ({width} × {height}) : livre ignoré.")
        return "skipped"

    report("  Thumbnail actuel : absent, illisible ou hauteur différente de 500 px.")

    # Toutes les couvertures intermédiaires sont effacées après chaque livre.
    work_dir = Path(tempfile.mkdtemp(prefix="fixcovers_work_", dir=str(TEMP_ROOT_DIR)))
    candidate_path = work_dir / "cover.jpg"

    try:
        report("  Étape 1/3 : vérification de p_cover…")
        local_cover = get_local_cover_from_p_cover(raw_p_cover)
        if local_cover:
            width, height = image_dimensions(local_cover)
            report(f"  p_cover valide ({width} × {height}).")
            if copy_valid_cover(local_cover, candidate_path):
                thumbnail_path = write_resized_thumbnail(candidate_path, uuid_value)
                if thumbnail_path:
                    update_thumbnail(connection, uuid_value, thumbnail_path)
                    connection.commit()
                    report("  Base Kindle mise à jour depuis p_cover.")
                    return "updated"
            report("  p_cover : copie ou création du thumbnail impossible.", "warning")
        else:
            report("  p_cover absent ou non valide.")

        report("  Étape 2/3 : recherche de couverture intégrée au fichier e-book…")
        location = resolve_location(raw_location)
        candidate_path.unlink(missing_ok=True)
        if extract_cover_from_ebook(location, mime_type, candidate_path):
            thumbnail_path = write_resized_thumbnail(candidate_path, uuid_value)
            if thumbnail_path:
                update_thumbnail(connection, uuid_value, thumbnail_path)
                connection.commit()
                report("  Base Kindle mise à jour depuis la couverture intégrée.")
                return "updated"
        else:
            report("  Aucune couverture intégrée exploitable.")

        report("  Étape 3/3 : recherche Google Books…")
        candidate_path.unlink(missing_ok=True)
        year = parse_year_from_db(raw_date)
        url = find_google_books_cover(title, author, year, publisher)
        if url and download_google_cover(url, candidate_path):
            thumbnail_path = write_resized_thumbnail(candidate_path, uuid_value)
            if thumbnail_path:
                update_thumbnail(connection, uuid_value, thumbnail_path)
                connection.commit()
                report("  Base Kindle mise à jour depuis Google Books.")
                return "updated"

        report("  Aucune couverture utilisable : livre inchangé.")
        return "unchanged"

    except sqlite3.Error as exc:
        connection.rollback()
        report(f"  Erreur SQLite : {exc}", "error")
        return "error"
    except Exception as exc:
        connection.rollback()
        logger.exception("Unexpected error for %r", title)
        report(f"  Erreur inattendue : {exc}", "error")
        return "error"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        report("  Fichiers temporaires du livre supprimés.")


def main():
    report("=== Fix Covers démarré ===")

    removed = cleanup_legacy_document_jpgs()
    if removed:
        report(f"Nettoyage terminé : {removed} JPG temporaire(s) supprimé(s).")
    else:
        report("Nettoyage : aucun ancien JPG temporaire trouvé.")

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
        report(f"Impossible de lire la base Kindle : {exc}", "error")
        raise

    total = len(rows)
    report(f"{total} livres détectés.")
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
                report(f"[{index}/{total}] Entrée sans UUID ou titre : ignorée.", "warning")
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
    report("=== Fix Covers terminé ===")
    report(
        "Résumé : "
        f"{counts['updated']} mis à jour, "
        f"{counts['skipped']} déjà valides, "
        f"{counts['unchanged']} inchangés, "
        f"{counts['error']} erreurs."
    )


if __name__ == "__main__":
    exit_code = 0
    try:
        main()
    except Exception as exc:
        exit_code = 1
        logger.exception("Fatal error")
        print("", flush=True)
        print(f"ERREUR FATALE : {exc}", flush=True)
    finally:
        print("", flush=True)
        print("========================================", flush=True)
        print("Traitement terminé.", flush=True)
        print("Appuyez sur Entrée pour fermer cette fenêtre.", flush=True)
        print("========================================", flush=True)
        try:
            input()
        except (EOFError, KeyboardInterrupt):
            pass
    raise SystemExit(exit_code)
