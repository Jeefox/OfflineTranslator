# -*- coding: utf-8 -*-
"""Необязательная интеграция с системным треем через pystray."""
from __future__ import annotations

import logging
from app_icons import application_icon

logger = logging.getLogger("offline_translate.tray")

try:
    import pystray  # type: ignore
    PYSTRAY_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    pystray = None
    PYSTRAY_AVAILABLE = False
    PYSTRAY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class SystemTray:
    """Жизненный цикл иконки трея не обращается к Tk из tray-потока."""

    def __init__(self, root, on_show, on_quit):
        self.root = root
        self.on_show = on_show
        self.on_quit = on_quit
        self.icon = None
        self.thread = None
        self.active = False

    @property
    def available(self):
        return PYSTRAY_AVAILABLE

    def start(self) -> bool:
        if not PYSTRAY_AVAILABLE:
            return False
        if self.active:
            return True
        try:
            image = application_icon(64)
            menu = pystray.Menu(
                pystray.MenuItem("Показать", self._show),
                pystray.MenuItem("Выйти", self._quit),
            )
            self.icon = pystray.Icon(
                "OfflineTranslator", image, "Офлайн Переводчик", menu)
            # pystray.run() должен вызываться из главного потока. Вызов
            # run() вручную из нашего daemon-потока иногда работает на Xorg,
            # но ломается на GTK/AppIndicator, из-за чего active сбрасывался
            # и крестик закрывал приложение вместо сворачивания.
            # run_detached() сам выбирает корректный способ интеграции с
            # текущим оконным циклом.
            self.icon.run_detached()
            self.active = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось запустить трей: %s", exc)
            self.icon = None
            self.active = False
            return False

    def _show(self, _icon=None, _item=None):
        self.root.after(0, self.on_show)

    def _quit(self, _icon=None, _item=None):
        self.root.after(0, self.on_quit)

    def stop(self):
        icon, self.icon = self.icon, None
        self.active = False
        if icon is not None:
            try:
                icon.stop()
            except Exception:  # noqa: BLE001
                pass
