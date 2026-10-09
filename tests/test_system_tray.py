"""Async native startup is not readiness; stale callbacks cannot revive a tray."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import system_tray as tray


class Icon:
    HAS_MENU = True
    def __init__(self, *args):
        self.visible = False
        self.stopped = False
    def run_detached(self, setup): self.setup = setup
    def stop(self): self.stopped = True


class TrayTests(unittest.TestCase):
    def setUp(self):
        api = SimpleNamespace(Icon=Icon, Menu=MagicMock(), MenuItem=MagicMock())
        for patcher in (patch.object(tray, "pystray", api),
                        patch.object(tray, "PYSTRAY_AVAILABLE", True)):
            patcher.start(); self.addCleanup(patcher.stop)
        self.tray = tray.SystemTray(MagicMock(), lambda: None, lambda: None)
        self.addCleanup(self.tray.stop)

    def test_ready_only_after_successful_native_setup(self):
        self.assertEqual(self.tray.state, tray.TrayState.CREATED)
        self.assertTrue(self.tray.start())
        self.assertEqual(self.tray.state, tray.TrayState.STARTING)
        self.assertFalse(self.tray.active)
        icon = self.tray.icon
        self.assertTrue(self.tray.start())
        self.assertIs(self.tray.icon, icon)
        icon.setup(icon)
        self.assertTrue(icon.visible)
        self.assertEqual(self.tray.state, tray.TrayState.READY)
        self.assertTrue(self.tray.active)
        self.tray.stop()
        self.assertTrue(icon.stopped)
        self.assertFalse(self.tray.active)

    def test_timeout_and_late_callback_cannot_hide_window(self):
        self.tray.start()
        icon = self.tray.icon
        with self.assertLogs("offline_translate.tray", level="WARNING"):
            self.tray._fail(self.tray._generation, "timeout")
        icon.setup(icon)
        self.assertEqual(self.tray.state, tray.TrayState.FAILED)
        self.assertFalse(self.tray.active)
        self.assertTrue(icon.stopped)
        self.assertFalse(icon.visible)
        self.assertTrue(self.tray.start())
        new_icon = self.tray.icon
        icon.setup(icon)
        self.assertFalse(self.tray.active)
        new_icon.setup(new_icon)
        self.assertTrue(self.tray.active)

    def test_stop_during_startup_and_missing_menu(self):
        self.tray.start()
        icon = self.tray.icon
        self.tray.stop()
        icon.setup(icon)
        self.assertEqual(self.tray.state, tray.TrayState.STOPPED)
        self.assertFalse(self.tray.active)
        with patch.object(Icon, "HAS_MENU", False), self.assertLogs("offline_translate.tray"):
            self.assertFalse(self.tray.start())
        self.assertEqual(self.tray.state, tray.TrayState.FAILED)

    def test_native_visible_failure(self):
        class BrokenIcon(Icon):
            @property
            def visible(self): return False
            @visible.setter
            def visible(self, value):
                if value: raise RuntimeError("native backend failed")
        with patch.object(tray.pystray, "Icon", BrokenIcon):
            self.tray.start()
        icon = self.tray.icon
        with self.assertLogs("offline_translate.tray", level="WARNING"):
            icon.setup(icon)
        self.assertEqual(self.tray.state, tray.TrayState.FAILED)
        self.assertFalse(self.tray.active)
        self.assertTrue(icon.stopped)


if __name__ == "__main__":
    unittest.main()
