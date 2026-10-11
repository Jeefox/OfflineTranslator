"""Persistent data paths; copy legacy data without moving the model cache."""
import logging
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import time

logger = logging.getLogger(__name__)


def legacy_data_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return Path(base) / "OfflineTranslator"


def model_cache_dir():
    return str(legacy_data_dir() / "cache")


def _copy_legacy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".migration-", delete=False) as file:
        temporary = Path(file.name)
    try:
        if source.name == "history.db":
            # SQLite backup includes committed WAL data; copying only .db can
            # silently lose it when an older application is still running.
            deadline = time.monotonic() + 5
            def progress(_status, _remaining, _total):
                if time.monotonic() > deadline:
                    raise OSError("History migration timed out")
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as old:
                with closing(sqlite3.connect(temporary)) as new:
                    old.backup(new, pages=128, progress=progress)
        else:
            with source.open("rb") as old, temporary.open("wb") as new:
                shutil.copyfileobj(old, new)
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        try:
            # Atomic publication without overwriting existing/newer user data.
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True
    finally:
        temporary.unlink(missing_ok=True)


def user_data_dir():
    legacy = legacy_data_dir()
    if sys.platform == "win32":
        target = legacy
    elif sys.platform == "darwin":
        target = Path.home() / "Library" / "Application Support" / "OfflineTranslator"
    else:
        target = Path(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")) / "OfflineTranslator"
    if target == legacy:
        return str(target)
    published = []
    try:
        for name in ("settings.json", "dictionary.json", "history.db"):
            source, destination = legacy / name, target / name
            if source.is_file() and not destination.exists():
                if _copy_legacy(source.resolve(), destination):
                    published.append((destination, destination.stat()))
    except (OSError, sqlite3.Error):
        # A partial migration must not leave stale copies that override later
        # changes saved in the legacy fallback directory on the next launch.
        for destination, original in published:
            try:
                current = destination.stat()
                if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) == (
                        original.st_dev, original.st_ino, original.st_mtime_ns, original.st_size):
                    destination.unlink()
            except OSError:
                logger.warning("Cannot remove partial migration file %s", destination, exc_info=True)
        # Retain the old directory as a usable fallback, not fresh defaults.
        logger.warning("Cannot migrate user data; using legacy directory", exc_info=True)
        return str(legacy)
    return str(target)
