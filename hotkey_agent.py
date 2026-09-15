"""Глобальный агент автоперевода выделенного текста (EN -> RU).

В любой программе выделяете текст (он попадает в буфер) -> нажимаете
настроенный хоткей (по умолчанию Ctrl+Alt+T, меняется в Settings) ->
перевод приходит системным уведомлением. Работает в фоновых потоках,
GUI не блокирует.

Параметры (хоткей, пороги, таймауты) читаются из ``Settings``.

Зависимости: pynput (перехват хоткея), pyperclip (буфер обмена; на Linux
нужен xclip/xsel или wl-clipboard), notify-send или plyer (уведомления;
на Linux используется notify-send, plyer — fallback).

На Linux выделенный мышью текст лежит в PRIMARY-селекции (CLIPBOARD —
только после Ctrl+C), поэтому PRIMARY читается напрямую утилитами, а
CLIPBOARD читается только как fallback.
"""
from __future__ import annotations

import logging
import platform
import re
import shutil
import subprocess
import threading

import pyperclip
from pynput import keyboard

from settings import Settings

logger = logging.getLogger("hotkey_agent")

_IS_LINUX = platform.system() == "Linux"

# --- Константы, не выносимые в config ------------------------------------- #
NOTIFY_TITLE = "Offline Translate"
NOTIFY_MAX_LEN = 200           # сколько символов перевода показывать

# Команды чтения PRIMARY-селекции (X11): берётся первая установленная.
# На Wayland PRIMARY недоступен из CLI — работаем через CLIPBOARD (fallback).
_PRIMARY_CMDS = [
    ["xclip", "-selection", "primary", "-o"],
    ["xsel", "--primary"],
]

# Кириллица в буфере -> вероятно, переводить нечего (уже русский).
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")

# Порог, при котором переключаемся на «длинный» режим перевода.
_LONG_TEXT_LEN = 300


class HotkeyAgent:
    """Слушает глобальный хоткей и переводит текст из буфера.

    Все тяжёлые операции (чтение буфера, перевод, уведомление) выполняются
    в фоновых потоках; главный поток/GUI не блокируются.
    """

    def __init__(self, translator, settings: Settings | None = None):
        self._translator = translator
        self._settings = settings if settings is not None else Settings()
        self._translate_lock = threading.Lock()  # CTranslate2 не потокобезопасен
        self._listener: keyboard.GlobalHotKeys | None = None
        self._running = False
        self._stopping = threading.Event()

    @property
    def settings(self) -> Settings:
        return self._settings

    # ------------------------------------------------------------------ #
    #  Жизненный цикл                                                     #
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        """Запускает слушатель хоткеев (в собственном потоке pynput)."""
        if self._running:
            return
        hotkey = self._settings.hotkey_pynput()
        if hotkey is None:
            logger.warning("Хоткей из настроек невалиден — агент не запущен")
            return
        self._stopping.clear()
        try:
            self._listener = keyboard.GlobalHotKeys({hotkey: self._on_hotkey})
            self._listener.start()  # внутри поднимает свой поток
        except Exception:  # noqa: BLE001 — нет X/прав/двойной запуск
            logger.exception("Не удалось запустить слушатель хоткеев")
            return
        self._running = True
        logger.info("Агент запущен, хоткей: %s", hotkey)

    def restart(self) -> None:
        """Перезапуск со свежими настройками (нужно после смены hotkey)."""
        self.stop()
        self.start()

    def stop(self) -> None:
        """Корректно завершает слушатель (идемпотентно)."""
        if not self._running:
            return
        self._stopping.set()
        try:
            if self._listener is not None:
                self._listener.stop()
        except Exception:  # noqa: BLE001
            logger.exception("Ошибка при остановке слушателя")
        finally:
            self._listener = None
            self._running = False
            logger.info("Агент остановлен")

    # ------------------------------------------------------------------ #
    #  Чтение буфера                                                      #
    # ------------------------------------------------------------------ #
    def _read_primary(self) -> str | None:
        """PRIMARY-селекция на X11 (выделение мышью, без Ctrl+C).

        Возвращает текст или None, если ни одна утилита не установлена
        / PRIMARY пуст."""
        if not _IS_LINUX:
            return None
        for cmd in _PRIMARY_CMDS:
            tool = shutil.which(cmd[0])
            if not tool:
                continue
            try:
                result = subprocess.run(
                    [tool, *cmd[1:]],
                    capture_output=True, text=True, timeout=2,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            if result.returncode != 0:
                continue
            text = result.stdout
            return text  # может быть "" (PRIMARY пуст) — это результат
        return None

    def _read_clipboard(self) -> str:
        """Текст из буфера.

        Linux/X11: PRIMARY (выделение мышью) с fallback на CLIPBOARD
        (Ctrl+C). Не трогает CLIPBOARD — только чтение.
        Windows/macOS/Wayland: CLIPBOARD (pyperclip)."""
        primary = self._read_primary()
        if primary is not None and primary.strip():
            return primary
        return pyperclip.paste() or ""

    # ------------------------------------------------------------------ #
    #  Обработка                                                          #
    # ------------------------------------------------------------------ #
    def _on_hotkey(self) -> None:
        """Срабатывает в потоке pynput. Не делаем здесь тяжёлого —
        передаём работу в отдельный worker, чтобы не тормозить слушателя."""
        if self._stopping.is_set():
            return
        threading.Thread(
            target=self._handle_request, daemon=True, name="hotkey-worker"
        ).start()

    def _handle_request(self) -> None:
        try:
            text = self._read_clipboard()
        except pyperclip.PyperclipException as exc:
            # нет xclip/xsel/wl-clipboard и т.п. — GUI не падаем
            logger.warning("Не удалось прочитать буфер: %s", exc)
            self._notify(
                "Ошибка: не удалось прочитать буфер "
                f"(установите xclip: {exc})"
            )
            return
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось прочитать буфер")
            return
        text = (text or "").strip()
        if not text:
            logger.info("Буфер пуст — пропускаем")
            self._notify("Нечего переводить: буфер обмена пуст")
            return
        max_len = int(self._settings.get("max_text_length", 5000))
        if len(text) > max_len:
            text = text[:max_len].strip()
            logger.info("Текст длиннее %d — усекаю", max_len)
        if self._settings.get("filter_cyrillic") and _CYRILLIC_RE.search(text):
            logger.info("В буфере кириллица — похоже, не английский")
            self._notify("Похоже, это не английский (найдена кириллица)")
            return
        self._translate_and_notify(text)

    def _translate_and_notify(self, text: str) -> None:
        """Перевод в потоке; если дольше ``slow_after_sec`` — сначала
        «Перевод...», затем результат."""
        box: dict = {}
        done = threading.Event()

        def work() -> None:
            try:
                box["text"] = self._do_translate(text).strip()
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(
            target=work, daemon=True, name="hotkey-translate"
        )
        worker.start()

        if not done.wait(self._settings.get("slow_after_sec", 3)):
            self._notify("Перевод...")
        done.wait()

        if "error" in box:
            logger.exception("Перевод завершился ошибкой")
            self._notify(f"Ошибка перевода: {box['error']}")
        else:
            self._notify(box["text"][:NOTIFY_MAX_LEN])

    def _do_translate(self, text: str) -> str:
        """Короткий — translate() (даёт источник), длинный — по абзацам."""
        with self._translate_lock:
            if len(text) <= _LONG_TEXT_LEN and "\n\n" not in text:
                return self._translator.translate(text).text
            return self._translator.translate_text(text)

    def _notify(self, message: str) -> None:
        """Системное уведомление.

        Linux — ``notify-send`` (прямой вызов, без plyer и dbus-warnings);
        остальное — plyer. Ошибки только логируются."""
        timeout = self._settings.get("notify_timeout", 5)
        if _IS_LINUX and shutil.which("notify-send"):
            try:
                subprocess.run(
                    ["notify-send", "-t", str(int(timeout * 1000)),
                     NOTIFY_TITLE, message],
                    check=False, timeout=5,
                    capture_output=True,
                )
                return
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("notify-send не сработал (%s) — plyer", exc)
        self._notify_plyer(message, timeout)

    @staticmethod
    def _notify_plyer(message: str, timeout: int) -> None:
        """Fallback: plyer (Windows/macOS или нет notify-send)."""
        try:
            from plyer import notification  # лениво: не нужен на Linux
            notification.notify(
                title=NOTIFY_TITLE, message=message,
                timeout=timeout,
            )
        except Exception:  # noqa: BLE001 — нет notify-системы и т.п.
            logger.exception("Не удалось отправить уведомление")
