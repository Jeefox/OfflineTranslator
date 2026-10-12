"""Non-destructive migration, recovery and CSV formula neutralization."""
import csv
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import user_paths
from settings import Settings
from history import HistoryManager


class UserDataSafetyTests(unittest.TestCase):
    def test_migration_preserves_new_files_and_committed_wal(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"XDG_CACHE_HOME": directory + "/cache", "XDG_DATA_HOME": directory + "/data"}), \
                patch.object(user_paths.sys, "platform", "linux"):
            old = user_paths.legacy_data_dir()
            old.mkdir(parents=True)
            (old / "settings.json").write_text('{"theme":"light"}')
            (old / "dictionary.json").write_text('{"hello":"привет"}')
            (old / "cache").mkdir()
            (old / "cache" / "weights").write_bytes(b"keep cache here")
            database = sqlite3.connect(old / "history.db")
            try:
                database.execute("PRAGMA journal_mode=WAL")
                database.execute("PRAGMA wal_autocheckpoint=0")
                database.execute("CREATE TABLE example(value TEXT)")
                database.execute("INSERT INTO example VALUES ('committed WAL')")
                database.commit()
                new = Path(user_paths.user_data_dir())
                self.assertEqual(new, Path(directory) / "data/OfflineTranslator")
                with sqlite3.connect(new / "history.db") as copied:
                    self.assertEqual(copied.execute("SELECT value FROM example").fetchone()[0], "committed WAL")
                self.assertEqual((new / "dictionary.json").read_bytes(), (old / "dictionary.json").read_bytes())
                (new / "settings.json").write_text('{"theme":"dark"}')
                self.assertEqual(user_paths.user_data_dir(), str(new))
                self.assertEqual(json.loads((new / "settings.json").read_text())["theme"], "dark")
                self.assertEqual(json.loads((old / "settings.json").read_text())["theme"], "light")
                self.assertEqual(user_paths.model_cache_dir(), str(old / "cache"))
                self.assertTrue((old / "cache/weights").is_file())
                self.assertFalse(list(new.glob(".migration-*")))
            finally:
                database.close()

    def test_failed_atomic_publication_keeps_legacy_usable(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"XDG_CACHE_HOME": directory + "/cache", "XDG_DATA_HOME": directory + "/data"}), \
                patch.object(user_paths.sys, "platform", "linux"):
            old = user_paths.legacy_data_dir()
            old.mkdir(parents=True)
            (old / "settings.json").write_bytes(b'{"theme":"light"}')
            with patch.object(user_paths.os, "link", side_effect=OSError("no hard links")):
                self.assertEqual(user_paths.user_data_dir(), str(old))
            self.assertEqual((old / "settings.json").read_bytes(), b'{"theme":"light"}')
            self.assertFalse(list((Path(directory) / "data/OfflineTranslator").iterdir()))

    def test_platform_paths(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HOME": directory, "LOCALAPPDATA": directory}), \
                patch.object(user_paths.sys, "platform", "darwin"):
            self.assertEqual(user_paths.user_data_dir(), str(Path(directory) / "Library/Application Support/OfflineTranslator"))
            with patch.object(user_paths.sys, "platform", "win32"):
                self.assertEqual(user_paths.user_data_dir(), str(Path(directory) / "OfflineTranslator"))

    def test_partial_migration_does_not_preserve_stale_settings(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"XDG_CACHE_HOME": directory + "/cache", "XDG_DATA_HOME": directory + "/data"}), \
                patch.object(user_paths.sys, "platform", "linux"):
            old = user_paths.legacy_data_dir()
            old.mkdir(parents=True)
            (old / "settings.json").write_text('{"theme":"light"}')
            (old / "history.db").write_bytes(b"corrupt database")
            self.assertEqual(user_paths.user_data_dir(), str(old))
            new = Path(directory) / "data/OfflineTranslator"
            self.assertFalse((new / "settings.json").exists())
            (old / "settings.json").write_text('{"theme":"dark"}')
            (old / "history.db").unlink()
            self.assertEqual(user_paths.user_data_dir(), str(new))
            self.assertEqual(json.loads((new / "settings.json").read_text())["theme"], "dark")

    def test_corrupt_settings_backed_up_before_recovery(self):
        for content in (b'{"unfinished":', b'\xffbroken encoding', b'[]'):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "settings.json"
                path.write_bytes(content)
                settings = Settings(path)
                backups = list(path.parent.glob("settings.json.corrupt-*.bak"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), content)
                self.assertEqual(json.loads(path.read_text())["theme"], "dark")
                self.assertTrue(settings.save())

    def test_failed_backup_blocks_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_bytes(b'broken original')
            with patch("settings.tempfile.NamedTemporaryFile", side_effect=OSError("disk full")):
                settings = Settings(path)
            self.assertFalse(settings.save())
            self.assertEqual(path.read_bytes(), b'broken original')

    def test_csv_neutralizes_formulas_without_mutating_history(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = HistoryManager(str(Path(directory) / "history.db"))
            values = ("=1+1", "+SUM(A1)", "-cmd", "@SUM(A1)", "  =1+1", "\tplain", "\n=1+1", "ordinary text")
            for value in values:
                self.assertTrue(manager.add_translation(value, value, "en-ru"))
            before = manager.get_history()
            path = Path(directory) / "export.csv"
            self.assertTrue(manager.export_to_csv(path))
            with path.open(newline="") as file:
                rows = list(csv.reader(file))[1:]
            for row, entry in zip(rows, before):
                expected = entry[1] if entry[1] == "ordinary text" else "'" + entry[1]
                self.assertEqual(row[1:3], [expected, expected])
            self.assertEqual(manager.get_history(), before)


if __name__ == "__main__":
    unittest.main()
