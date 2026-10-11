"""Clipboard routing and pointer-width WinAPI regression without native OS APIs."""
import ctypes
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import hotkey_agent


class ClipboardTests(unittest.TestCase):
    def test_windows_preserves_handles_and_unicode_pointer(self):
        handle = 0x123456789ABC if ctypes.sizeof(ctypes.c_void_p) == 8 else 0x123456
        buffer = ctypes.create_unicode_buffer("Привет\nclipboard")
        address = ctypes.addressof(buffer)
        calls = []
        # Actual C-callable functions deliberately start with ctypes' int
        # restype; application code must set their correct WinAPI signatures.
        def native(result, arguments, function):
            callback = ctypes.CFUNCTYPE(result, *arguments)(function)
            callback.restype = ctypes.c_int
            return callback
        user32 = SimpleNamespace(
            OpenClipboard=native(ctypes.c_int, [ctypes.c_void_p], lambda _: 1),
            GetClipboardData=native(ctypes.c_void_p, [ctypes.c_uint], lambda _: handle),
            CloseClipboard=native(ctypes.c_int, [], lambda: calls.append("close") or 1))
        kernel32 = SimpleNamespace(
            GlobalLock=native(ctypes.c_void_p, [ctypes.c_void_p],
                              lambda value: calls.append(("lock", value)) or address),
            GlobalUnlock=native(ctypes.c_int, [ctypes.c_void_p],
                                lambda value: calls.append(("unlock", value)) or 1))
        original_read = ctypes.wstring_at
        def guarded_read(pointer):
            # A failing regression must not dereference a truncated address.
            self.assertEqual(pointer, address)
            return original_read(pointer)
        with patch.object(ctypes, "windll", SimpleNamespace(user32=user32, kernel32=kernel32), create=True), \
                patch.object(ctypes, "wstring_at", side_effect=guarded_read):
            self.assertEqual(hotkey_agent._read_windows_clipboard(), buffer.value)
            self.assertEqual(calls, [("lock", handle), ("unlock", handle), "close"])
            calls.clear()
            with patch.object(ctypes, "wstring_at", side_effect=RuntimeError("read failed")):
                with self.assertRaises(RuntimeError):
                    hotkey_agent._read_windows_clipboard()
            self.assertEqual(calls[-2:], [("unlock", handle), "close"])

    def test_macos_reads_pbpaste_without_x11(self):
        with patch.object(hotkey_agent, "_IS_WINDOWS", False), \
                patch.object(hotkey_agent.platform, "system", return_value="Darwin"), \
                patch.object(hotkey_agent.shutil, "which", return_value="/usr/bin/pbpaste"), \
                patch.object(hotkey_agent.subprocess, "run", return_value=SimpleNamespace(
                    returncode=0, stdout="  Привет\n".encode())) as run:
            self.assertEqual(hotkey_agent.read_clipboard_text(), "  Привет\n")
            self.assertEqual(run.call_args.args[0], ["pbpaste"])
            self.assertEqual(run.call_args.kwargs["timeout"], 2)
            self.assertNotIn("shell", run.call_args.kwargs)
            for failure in (OSError("missing"), subprocess.TimeoutExpired("pbpaste", 2)):
                run.side_effect = failure
                self.assertEqual(hotkey_agent.read_clipboard_text(), "")

    def test_linux_keeps_primary_selection_priority(self):
        with patch.object(hotkey_agent, "_IS_WINDOWS", False), \
                patch.object(hotkey_agent.platform, "system", return_value="Linux"), \
                patch.object(hotkey_agent, "_read_clipboard_with", side_effect=[None, "primary"]) as read:
            self.assertEqual(hotkey_agent.read_clipboard_text(), "primary")
            self.assertEqual(read.call_args_list[-1].args[0], ["xsel", "--primary"])


if __name__ == "__main__":
    unittest.main()
