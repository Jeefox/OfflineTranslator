# -*- coding: utf-8 -*-
"""Минимальный кроссплатформенный слой системных уведомлений."""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger("offline_translate.notifications")


def _send_command(command):
    result = subprocess.run(command, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=3)
    if result.returncode != 0:
        logger.warning("Команда уведомления завершилась с кодом %s", result.returncode)
        return False
    return True


def show_notification(title: str, message: str, duration_sec: float = 5) -> bool:
    """Показывает уведомление ОС и возвращает признак успешной отправки."""
    title = str(title or "Офлайн Переводчик")
    message = str(message or "")
    if not message:
        return False
    timeout_ms = max(1000, int(float(duration_sec) * 1000))

    try:
        system = platform.system()
        if system == "Linux" and shutil.which("notify-send"):
            return _send_command(["notify-send", "-t", str(timeout_ms), title, message])
        if system == "Darwin":
            # osascript не позволяет надёжно задать timeout, но системное
            # уведомление всё равно отображается по политике macOS.
            def apple_string(value):
                return '"' + value.replace("\\", "\\\\").replace(
                    '"', '\\"').replace("\n", " ") + '"'
            script = "display notification %s with title %s" % (
                apple_string(message), apple_string(title))
            return _send_command(["osascript", "-e", script])
        if system == "Windows":
            # plyer остаётся необязательным fallback для Windows-сборок.
            from plyer import notification  # type: ignore
            notification.notify(
                title=title, message=message, timeout=max(1, int(duration_sec))
            )
            return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Системное уведомление недоступно: %s", exc)
    return False
