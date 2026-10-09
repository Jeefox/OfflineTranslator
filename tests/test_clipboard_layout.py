"""Clipboard readers preserve leading/trailing whitespace and newline style."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hotkey_agent as agent


class ClipboardTests(unittest.TestCase):
    def test_linux_preserves_bytes_layout(self):
        source = "  привет\t\r\n  "
        with patch.object(agent.shutil, "which", return_value="/bin/xclip"), \
                patch.object(agent.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout=source.encode("utf-8"))) as run:
            self.assertEqual(agent._read_clipboard_with(["xclip", "-o"]), source)
            self.assertNotIn("text", run.call_args.kwargs)

    def test_windows_preserves_unicode_layout(self):
        source = "  привет\t\r\n  "
        user32, kernel32 = MagicMock(), MagicMock()
        with patch.object(agent.ctypes, "windll", SimpleNamespace(user32=user32,
                          kernel32=kernel32), create=True), \
                patch.object(agent.ctypes, "wstring_at", return_value=source):
            self.assertEqual(agent._read_windows_clipboard(), source)
        kernel32.GlobalUnlock.assert_called_once()
        user32.CloseClipboard.assert_called_once()


if __name__ == "__main__":
    unittest.main()
