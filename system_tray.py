# -*- coding: utf-8 -*-
"""Необязательная интеграция с системным треем через pystray."""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger("offline_translate.tray")

try:
    import pystray  # type: ignore
    from PIL import Image, ImageDraw  # type: ignore
    PYSTRAY_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    pystray = None
    Image = None
    ImageDraw = None
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
            image = Image.new("RGBA", (64, 64), (30, 30, 46, 255))
            draw = ImageDraw.Draw(image)
            draw.rounded_rectangle((8, 8, 56, 56), radius=10,
                                   fill=(137, 180, 250, 255))
            draw.text((23, 18), "A", fill=(30, 30, 46, 255))
            menu = pystray.Menu(
                pystray.MenuItem("Показать", self._show),
                pystray.MenuItem("Выйти", self._quit),
            )
            self.icon = pystray.Icon(
                "OfflineTranslator", image, "Офлайн Переводчик", menu)
            self.thread = threading.Thread(target=self._run,
                                            name="offline-translator-tray",
                                            daemon=True)
            self.thread.start()
            self.active = True
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось запустить трей: %s", exc)
            self.icon = None
            self.active = False
            return False

    def _run(self):
        try:
            self.icon.run()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Трей завершился недоступностью платформы: %s", exc)
            self.active = False

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
