"""Global hotkey safety and settings persistence regressions."""
from pathlib import Path
import sys
import tempfile
import json
from unittest.mock import patch
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settings import Settings, normalize_hotkey


class SettingsIntegrityTests(unittest.TestCase):
    def test_atomic_save_preserves_previous_file_on_io_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = Settings(path)
            original = path.read_bytes()
            settings.set("theme", "light")
            for operation in ("os.replace", "os.fsync"):
                with self.subTest(operation=operation), \
                        patch("settings." + operation, side_effect=OSError("disk failure")), \
                        self.assertLogs("offline_translate.settings", level="ERROR"):
                    self.assertFalse(settings.save())
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(Path(directory).glob(".settings-*.tmp")), [])
            self.assertTrue(settings.save())
            self.assertEqual(json.loads(path.read_text())["theme"], "light")

    def test_global_hotkeys_require_non_shift_modifier(self):
        for text in ("a", "f12", "space", "shift+a", "shift+f12", "ctrl", "ctrl+a+b"):
            with self.subTest(text=text):
                self.assertIsNone(normalize_hotkey(text))
        for text in ("ctrl+a", "alt+f12", "cmd+space", "super+t", "ctrl+shift+t"):
            with self.subTest(text=text):
                self.assertIsNotNone(normalize_hotkey(text))
        self.assertEqual(normalize_hotkey("option+t"), "<alt>+t")


if __name__ == "__main__":
    unittest.main()
