# -*- coding: utf-8 -*-
"""Необязательная интеграция с системным треем через pystray."""
from __future__ import annotations

import logging
import threading
from enum import Enum
from app_icons import application_icon

logger = logging.getLogger("offline_translate.tray")

try:
    import pystray  # type: ignore
    PYSTRAY_AVAILABLE = True
except Exception as exc:  # noqa: BLE001
    pystray = None
    PYSTRAY_AVAILABLE = False
    PYSTRAY_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"


class TrayState(str, Enum):
    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPED = "stopped"


class SystemTray:
    """Native readiness controls whether closing the window may hide it."""

    def __init__(self, root, on_show, on_quit, *, startup_timeout=5):
        self.root = root
        self.on_show = on_show
        self.on_quit = on_quit
        self.icon = None
        self._lock = threading.RLock()
        self._state = TrayState.CREATED
        self._generation = 0
        self._timer = None
        self._startup_timeout = startup_timeout

    @property
    def state(self):
        with self._lock:
            return self._state

    @property
    def active(self):
        return self.state == TrayState.READY

    @property
    def available(self):
        return PYSTRAY_AVAILABLE and bool(pystray.Icon.HAS_MENU)

    def start(self) -> bool:
        with self._lock:
            if self._state in (TrayState.STARTING, TrayState.READY):
                return True
            self._generation += 1
            generation = self._generation
            self._state = TrayState.STARTING
        if not self.available:
            self._fail(generation, "Tray backend or recoverable menu unavailable")
            return False
        try:
            image = application_icon(64)
            menu = pystray.Menu(
                pystray.MenuItem("Показать", self._show, default=True),
                pystray.MenuItem("Выйти", self._quit),
            )
            icon = pystray.Icon("OfflineTranslator", image, "Офлайн Переводчик", menu)
            with self._lock:
                if generation != self._generation:
                    return False
                self.icon = icon
                self._timer = threading.Timer(self._startup_timeout, self._fail,
                                              args=(generation, "Tray startup timed out"))
                self._timer.daemon = True
                self._timer.start()
            icon.run_detached(setup=lambda ready_icon: self._ready(ready_icon, generation))
            return self.state in (TrayState.STARTING, TrayState.READY)
        except Exception as exc:
            self._fail(generation, str(exc))
            return False

    def _ready(self, icon, generation):
        try:
            with self._lock:
                stale = (generation != self._generation or self.icon is not icon
                         or self._state != TrayState.STARTING)
                if not stale:
                    icon.visible = True
                    self._state = TrayState.READY
                    if self._timer is not None:
                        self._timer.cancel()
                        self._timer = None
            if stale:
                self._stop_icon(icon)
        except Exception as exc:
            self._fail(generation, str(exc))

    def _fail(self, generation, reason):
        with self._lock:
            if generation != self._generation or self._state != TrayState.STARTING:
                return
            self._state = TrayState.FAILED
            icon, self.icon = self.icon, None
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        logger.warning("Не удалось запустить трей: %s", reason)
        self._stop_icon(icon)

    @staticmethod
    def _stop_icon(icon):
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                logger.warning("Не удалось остановить трей", exc_info=True)

    def _show(self, _icon=None, _item=None):
        self.root.after(0, self.on_show)

    def _quit(self, _icon=None, _item=None):
        self.root.after(0, self.on_quit)

    def stop(self):
        with self._lock:
            self._generation += 1
            icon, self.icon = self.icon, None
            self._state = TrayState.STOPPED
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
        self._stop_icon(icon)
