#!/mnt/us/python3/python/bin/python3
# -*- coding: utf-8 -*-
"""Restore cc.db and undo the thumbnail changes made by fix_covers.py.

Usage: python3 restore_covers.py [path/to/created_thumbnails.json]
Without argument, the rollback journals are listed and one can be selected.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
from pathlib import Path

DB_PATH = Path("/var/local/cc.db")
DB_TABLE = "Entries"
BACKUP_DIR = Path("/mnt/us/documents/ccdb_reset_backups")
THUMBNAILS_DIR = Path("/mnt/us/system/thumbnails")
ROLLBACK_ROOT = Path("/mnt/base-us/fixcovers_backups")
BACKUP_NAME = re.compile(r"cc\.db\.backup-\d{8}-\d{6}\.sqlite")


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checked_file(path, expected):
    if path.is_symlink() or not path.is_file() or file_sha256(path) != expected:
        raise RuntimeError(f"Missing, unsafe, or changed file: {path}")


def check_backup_readable(backup):
    """Collation-free check. PRAGMA integrity_check must NOT be used: cc.db has
    indexes using the Kindle's "icu" collation, unknown to Python's sqlite3
    ("no such collation sequence: icu"). The SHA-256 already guarantees the
    file is exactly the one created by fix_covers.py."""
    connection = sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        connection.execute(
            'SELECT COUNT(*) FROM "{}" NOT INDEXED'.format(DB_TABLE)).fetchone()
    finally:
        connection.close()


def load_and_validate(manifest):
    if manifest.is_symlink():
        raise RuntimeError("Symbolic-link journals are not supported.")
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("version") != 1:
        raise RuntimeError("Unsupported journal version.")
    if data["database"] != str(DB_PATH.resolve()):
        raise RuntimeError("The journal refers to a different database.")
    if data["thumbnails_dir"] != str(THUMBNAILS_DIR.resolve()):
        raise RuntimeError("The journal refers to a different thumbnails directory.")
    backup = Path(data["backup"])
    # Current location: BACKUP_DIR/cc.db.backup-YYYYMMDD-HHMMSS.sqlite
    # (legacy journals: <run folder>/cc.db.backup).
    current = backup.parent == BACKUP_DIR and BACKUP_NAME.fullmatch(backup.name)
    legacy = backup == manifest.parent / "cc.db.backup"
    if not (current or legacy):
        raise RuntimeError("Unexpected database backup location.")
    checked_file(backup, data["backup_sha256"])
    check_backup_readable(backup)
    seen = set()
    for item in data["images"]:
        path = Path(item["path"])
        if (not path.is_absolute() or path.parent.resolve() != THUMBNAILS_DIR.resolve()
                or path.name != item["name"] or path.suffix.lower() != ".jpg"
                or path.is_symlink() or str(path) in seen):
            raise RuntimeError(f"Unsafe or duplicate thumbnail path: {path}")
        seen.add(str(path))
        original = item["original_backup"]
        if item["newly_created"] != (original is None):
            raise RuntimeError("Inconsistent thumbnail journal.")
        if original:
            original = Path(original)
            if original.parent != manifest.parent / "original_thumbnails":
                raise RuntimeError("Unsafe original thumbnail backup location.")
            checked_file(original, item["original_sha256"])
        if path.exists():
            allowed = {item["expected_sha256"]}
            if original:
                allowed.add(item["original_sha256"])
            if file_sha256(path) not in allowed:
                raise RuntimeError(f"Thumbnail changed after this run; refusing restoration: {path}")
    return data


def main():
    parser = argparse.ArgumentParser(description="Restore cc.db and undo thumbnail changes.")
    parser.add_argument("manifest", nargs="?", help="Path to created_thumbnails.json")
    args = parser.parse_args()
    if args.manifest:
        manifest = Path(args.manifest).absolute()
    else:
        manifests = sorted(ROLLBACK_ROOT.glob("run_*/created_thumbnails.json"),
                           key=lambda path: path.stat().st_mtime, reverse=True)
        if not manifests:
            raise RuntimeError("No rollback journals found.")
        for index, path in enumerate(manifests, 1):
            print(f"{index}: {path}")
        selection = int(input("Select the backup number: "))
        if not 1 <= selection <= len(manifests):
            raise ValueError("Invalid backup number.")
        manifest = manifests[selection - 1].absolute()
    data = load_and_validate(manifest)
    if data.get("restored"):
        print("This backup has already been restored.")
        return
    print(f"Database backup: {data['backup']}")
    print(f"Thumbnail entries: {len(data['images'])}")
    print("WARNING: Database changes made after this backup will be lost.")
    print("Stop the cover fixer and all applications writing to cc.db or thumbnails first.")
    print("Temporary document JPG deletions cannot be undone by this script.")
    if input("Type RESTORE to continue: ").strip() != "RESTORE":
        print("Restoration cancelled.")
        return
    data = load_and_validate(manifest)
    if not DB_PATH.is_file():
        raise FileNotFoundError(f"Database not found: {DB_PATH}")
    # Restore through SQLite so existing WAL state is handled by SQLite itself.
    source = sqlite3.connect(Path(data["backup"]).as_uri() + "?mode=ro", uri=True)
    try:
        destination = sqlite3.connect(str(DB_PATH), timeout=30)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    print("Database restored.")
    for item in data["images"]:
        path = Path(item["path"])
        if item["newly_created"]:
            path.unlink(missing_ok=True)
            print(f"Removed new thumbnail: {path}")
        else:
            temporary = path.with_name(path.name + ".restore.tmp")
            try:
                shutil.copy2(item["original_backup"], temporary)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            print(f"Restored original thumbnail: {path}")
    data["restored"] = True
    temporary = manifest.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(manifest)
    print("Restoration completed. The backup and journal were retained.")
    print("Remember to restart the Kindle to reload the catalog.")


if __name__ == "__main__":
    try:
        main()
    except (Exception, KeyboardInterrupt) as exc:
        print(f"RESTORATION ERROR: {exc}")
        raise SystemExit(1)
