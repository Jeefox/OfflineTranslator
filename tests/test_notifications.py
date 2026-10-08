"""Starting a notifier process is not confirmation that it accepted delivery."""
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifications


class NotificationTests(unittest.TestCase):
    def test_completion_status_on_linux_and_macos(self):
        for system in ("Linux", "Darwin"):
            for code in (0, 1):
                with self.subTest(system=system, code=code), \
                        patch.object(notifications.platform, "system", return_value=system), \
                        patch.object(notifications.shutil, "which", return_value="/bin/notify-send"), \
                        patch.object(notifications.subprocess, "run", return_value=SimpleNamespace(
                            returncode=code)) as run:
                    self.assertEqual(notifications.show_notification("title", "message"), code == 0)
                    self.assertEqual(run.call_args.kwargs["timeout"], 3)
                    self.assertNotIn("shell", run.call_args.kwargs)

    def test_timeout_and_exec_failure(self):
        for error in (subprocess.TimeoutExpired("notify-send", 3), OSError("DBus failed")):
            with patch.object(notifications.platform, "system", return_value="Linux"), \
                    patch.object(notifications.shutil, "which", return_value="/bin/notify-send"), \
                    patch.object(notifications.subprocess, "run", side_effect=error), \
                    self.assertLogs("offline_translate.notifications", level="WARNING"):
                self.assertFalse(notifications.show_notification("title", "message"))


if __name__ == "__main__":
    unittest.main()
