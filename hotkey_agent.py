# -*- coding: utf-8 -*-
"""Глобальный агент хоткея — перенос hotkey_agent.py из старой версии.

Поведение: в любой программе выделяете текст (выделение мышью / Ctrl+C),
нажимаете глобальный хоткей (по умолчанию Ctrl+Alt+T, настраивается в
«Настройках») — агент передаёт текст главному окну: он ставится в исходное
поле, направление определяется автоматически (если включён фильтр кириллицы),
перевод запускается, окно поднимается на передний план.

Отличия от старой версии (адаптация к текущей архитектуре):
- результат уходит в окно приложения, а не системным уведомлением
  (подсистемы уведомлений в текущей версии нет; перевод виден в интерфейсе);
- pyperclip/plyer не используются: буфер читается через xclip/xsel на Linux
  и через ctypes (stdlib) на Windows;
- pynput опционален: без него (или когда backend недоступен — Wayland, нет
  X, нет python3-xlib) агент просто не запускается, а само приложение
  работает как обычно (импорт модуля никогда не падает).
"""
from __future__ import annotations

import ctypes
import logging
import platform
import shutil
import subprocess

logger = logging.getLogger("hotkey_agent")

try:  # pynput опционален: приложение обязано запускаться без него
    from pynput import keyboard as _pynput_keyboard
    PYNPUT_AVAILABLE = True
    PYNPUT_IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001 (нет pynput, нет X/Xlib и т.п.)
    _pynput_keyboard = None
    PYNPUT_AVAILABLE = False
    PYNPUT_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

_IS_WINDOWS = platform.system() == "Windows"

# На X11 выделенный мышью текст лежит в PRIMARY-селекции (CLIPBOARD
# заполняется только после Ctrl+C). Берём первую установленную утилиту.
# На Wayland PRIMARY из CLI недоступен — работает CLIPBOARD (fallback).
_PRIMARY_CMDS = (
    ["xclip", "-selection", "primary", "-o"],
    ["xsel", "--primary"],
)
_CLIPBOARD_CMDS = (
    ["xclip", "-o"],
    ["xsel", "--clipboard"],
)


def _read_clipboard_with(cmd: list[str]) -> str | None:
    """Читает буфер одной утилитой; None, если утилита не установлена/пусто."""
    if not shutil.which(cmd[0]):
        return None
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return None


def _read_windows_clipboard() -> str:
    """Системный буфер Windows через ctypes (без сторонних библиотек)."""
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
    except AttributeError:  # не Windows
        return ""
    CF_UNICODE_TEXT = 13
    if not user32.OpenClipboard(None):
        return ""
    try:
        handle = user32.GetClipboardData(CF_UNICODE_TEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr).strip()
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


def read_clipboard_text() -> str:
    """Текст из буфера/селекции. При любой проблеме — '' (не исключение)."""
    if _IS_WINDOWS:
        try:
            return _read_windows_clipboard()
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось прочитать буфер Windows")
            return ""


    for cmds in (_PRIMARY_CMDS, _CLIPBOARD_CMDS):
        for cmd in cmds:
            text = _read_clipboard_with(cmd)
            if text:
                return text
    logger.info("Буфер прочитать не удалось (нет xclip/xsel или пусто)")
    return ""


class HotkeyAgent:
    """Слушает глобальный хоткей и передаёт перехваченный текст приложению.

    Агент НЕ обращается к Tkinter: ``on_trigger`` вызывается из потока pynput,
    и это приложение должно безопасно передать текст в главный поток
    (например, через queue.Queue — см. main.py).
    """

    def __init__(self, on_trigger):
        self._on_trigger = on_trigger
        self._listener = None
        self.active = False
        self.error: str | None = None

    # ------------------------------------------------------------------ #
    #  Жизненный цикл                                                     #
    # ------------------------------------------------------------------ #
    def start(self, hotkey: str) -> bool:
        """Запускает слушатель хоткея. False — не запущен (причина в self.error)."""
        self.stop()
        if not PYNPUT_AVAILABLE:
            self.error = f"pynput недоступен ({PYNPUT_IMPORT_ERROR or 'не установлен'})"
            logger.warning("Агент хоткея не запущен: %s", self.error)
            return False
        from settings import normalize_hotkey
        normalized = normalize_hotkey(hotkey)
        if normalized is None:
            self.error = f"невалидный хоткей: {hotkey!r}"
            logger.warning("Агент хоткея не запущен: %s", self.error)
            return False
        try:
            key = _pynput_keyboard.HotKey.parse(normalized)
            self._listener = _pynput_keyboard.GlobalHotKeys({key: self._trigger})
            self._listener.start()  # внутри поднимает свой поток
        except Exception as exc:  # noqa: BLE001 (нет X/прав, двойной запуск и т.п.)
            self._listener = None
            self.error = str(exc)
            logger.warning("Не удалось запустить слушатель хоткеев: %s", exc)
            return False
        self.active = True
        self.error = None
        logger.info("Агент хоткея запущен: %s", hotkey)
        return True

    def stop(self) -> None:
        """Останавливает слушатель (безопасно при повторном/пустом вызове)."""
        listener, self._listener = self._listener, None
        self.active = False
        if listener is not None:
            try:
                listener.stop()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    def _trigger(self) -> None:
        """Глобальный хоткей нажат: читаем буфер и передаём текст приложению."""
        try:
            text = read_clipboard_text()
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось прочитать буфер по хоткею")
            return
        if not text:
            logger.info("Буфер пуст — переводить нечего")
            return
        try:
            self._on_trigger(text)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось передать текст приложению")