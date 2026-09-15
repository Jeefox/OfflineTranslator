"""Хранилище настроек приложения (config.json).

Путь к файлу:
  Linux/macOS — ``~/.config/offline_translate/config.json``
  Windows      — ``%APPDATA%/offline_translate/config.json``

Можно переопределить переменной окружения ``OFFLINE_TRANSLATE_CONFIG``
(абсолютный путь). Если файла нет, он создаётся с дефолтными значениями.

Ключи валидируются при ``set()``: hotkey пробрасывается через парсер
pynput (иначе используется дефолт + warning), числа должны быть > 0,
буквенные поля — из разрешённого списка, model_path — мягко (warning,
но значение сохраняется).

``model_path``:
  - ``None`` / пустая строка — модель ищется автоматически:
    в PyInstaller (``sys._MEIPASS``) либо рядом с проектом (dev).
  - строка — абсолютный/относительный путь к ``.argosmodel``.
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import sys
from pathlib import Path

try:  # pynput обязателен для агента, но settings не должен падать без него
    from pynput.keyboard import HotKey
except Exception:  # noqa: BLE001
    HotKey = None

logger = logging.getLogger("offline_translate.settings")

# --- Дефолтные значения -------------------------------------------------- #
# model_path = None означает «автоопределение» (см. get_model_path).
DEFAULTS: dict = {
    "hotkey": "ctrl+alt+t",
    "source_lang": "en",
    "target_lang": "ru",
    "theme": "dark",
    "slow_after_sec": 3,
    "debounce_sec": 1.5,
    "notify_timeout": 5,
    "max_text_length": 5000,
    "filter_cyrillic": True,
    "model_path": None,
}

# Имя файла модели, используемое при автоопределении.
MODEL_FILENAME = "translate-en_ru-1_9.argosmodel"

# Допустимые значения для буквенных полей.
_ALLOWED = {
    "source_lang": {"en", "ru", "de", "fr", "es", "it", "pl", "cs", "ca", "el", "uk", "tr"},
    "target_lang": {"en", "ru", "de", "fr", "es", "it", "pl", "cs", "ca", "el", "uk", "tr"},
    "theme": {"dark", "light"},
}

# Модификаторы, которые pynput понимает без угловых скобок в парсере.
_MODIFIERS = {"ctrl", "alt", "shift", "cmd", "command", "win", "super", "option", "alt_gr"}

# pynput требует <...> вокруг модификаторов; нормализуем дружелюбную форму.
_HOTKEY_RE = re.compile(r"^[\w<>()+]+$")


def _default_config_path() -> Path:
    """Путь к config.json: переопределяется OFFLINE_TRANSLATE_CONFIG
    (абсолютным путём к файлу или каталогу)."""
    env = os.environ.get("OFFLINE_TRANSLATE_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.suffix else p / "config.json"
    if platform.system() == "Windows":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return base / "offline_translate" / "config.json"
    return Path.home() / ".config" / "offline_translate" / "config.json"


def _default_model_path() -> Path:
    """Автоопределение пути к модели.

    Порядок:
      1. PyInstaller (``sys._MEIPASS``) — модель, встроенная в бинарник;
      2. dev-режим — рядом с этим файлом (корень проекта).
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate = Path(meipass) / MODEL_FILENAME
        if candidate.exists():
            return candidate
        return candidate
    return Path(__file__).resolve().parent / MODEL_FILENAME


def _check_model_file(path: Path) -> tuple[bool, str]:
    """Мягкая проверка файла модели (не блокирует сохранение).

    Возвращает ``(True, "OK")``, если файл существует и имеет расширение
    ``.argosmodel``; ``(False, "<warning>")`` — при отсутствии файла или
    ином расширении (сохранить значение всё равно можно).
    """
    if path.suffix.lower() != ".argosmodel":
        msg = f"Ожидалось расширение .argosmodel, а не {path.suffix!r}: {path}"
        return False, msg
    if not path.exists():
        msg = f"Модель не найдена по пути: {path}"
        return False, msg
    return True, "OK"


def normalize_hotkey(hotkey: str) -> str | None:
    """Приводит дружельную форму ``ctrl+alt+t`` к парсируемой pynput
    ``<ctrl>+<alt>+t``. Возвращает ``None``, если комбинация невалидна."""
    if not isinstance(hotkey, str):
        return None
    raw = hotkey.strip().lower()
    if not raw or not _HOTKEY_RE.match(raw):
        return None
    parts = [p.strip() for p in raw.split("+") if p.strip()]
    if not parts:
        return None
    normalized = []
    for part in parts:
        if part.startswith("<") and part.endswith(">"):
            name = part[1:-1].lower()
        else:
            name = part.lower()
        if name in _MODIFIERS:
            # command/win/super/option -> pynput 'cmd'
            if name in ("command", "win", "super", "option"):
                name = "cmd"
            normalized.append(f"<{name}>")
        elif len(name) == 1 and (name.isalnum()):
            normalized.append(name)
        else:
            return None
    if len(normalized) != len(set(normalized)):
        return None
    result = "+".join(normalized)
    # Перепроверка парсером самого pynput — он и есть источник правды.
    if HotKey is not None:
        try:
            HotKey.parse(result)
        except ValueError:
            return None
    return result


def _coerce_number(value) -> int | float | None:
    """Приводит значение к числу для валидации.

    int/float принимаются как есть, числовые строки конвертируются
    (``'3'`` -> ``3``, ``'1.5'`` -> ``1.5``) — GUI передаёт значения
    из полей ввода именно строками. bool, пустые/нечисловые строки
    и прочий мусор -> ``None``.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            num = float(value.strip())
        except ValueError:
            return None
        return int(num) if num.is_integer() else num
    return None


def _validate(key: str, value) -> object:
    """Возвращает скорректированное значение (или дефолт + warning)."""
    if key == "hotkey":
        normalized = normalize_hotkey(value)
        if normalized is None:
            logger.warning(
                "Невалидный hotkey %r — используется дефолт %r",
                value, DEFAULTS["hotkey"],
            )
            return DEFAULTS["hotkey"]
        # Храним дружельную форму, нормализованную при использовании.
        return normalized.replace("<", "").replace(">", "")
    if key in _ALLOWED:
        if not isinstance(value, str) or value.lower() not in _ALLOWED[key]:
            logger.warning(
                "Недопустимое значение %r для %s — используется %r",
                value, key, DEFAULTS[key],
            )
            return DEFAULTS[key]
        return value.lower()
    if key in ("slow_after_sec", "notify_timeout", "max_text_length"):
        num = _coerce_number(value)
        if num is None or num <= 0:
            logger.warning(
                "Недопустимое число %r для %s — используется %r",
                value, key, DEFAULTS[key],
            )
            return DEFAULTS[key]
        return num
    if key == "debounce_sec":
        num = _coerce_number(value)
        if num is None or num <= 0:
            logger.warning(
                "Недопустимое число %r для %s — используется %r",
                value, key, DEFAULTS[key],
            )
            return DEFAULTS[key]
        return float(num)
    if key == "filter_cyrillic":
        if not isinstance(value, bool):
            logger.warning(
                "Недопустимое значение %r для %s — используется %r",
                value, key, DEFAULTS[key],
            )
            return DEFAULTS[key]
        return value
    if key == "model_path":
        # Мягкая валидация: пусто -> автоопределение (None); строка ->
        # путь (warning при проблемах, но значение сохраняется).
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        if not isinstance(value, str):
            logger.warning(
                "Недопустимое значение %r для model_path — использую автоопределение",
                value,
            )
            return None
        path = Path(value).expanduser()
        ok, msg = _check_model_file(path)
        if not ok:
            logger.warning("%s", msg)
        return str(path)
    logger.warning("Неизвестный ключ настроек %r — значение сохранено как есть", key)
    return value


class Settings:
    """Обёртка над ``config.json`` с валидацией и дефолтами."""

    def __init__(self, path: str | Path | None = None):
        self.path: Path = Path(path) if path else _default_config_path()
        self._data: dict = {**DEFAULTS}
        self._load()

    # ------------------------------------------------------------------ #
    #  Загрузка / сохранение                                              #
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self.path.exists():
            self.save()  # создать с дефолтами
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("config.json должен содержать JSON-объект")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(
                "Не удалось прочитать %s (%s) — использую дефолты", self.path, exc
            )
            self._data = {**DEFAULTS}
            self.save()
            return
        for key, value in raw.items():
            self._data[key] = _validate(key, value)
        # Гарантируем наличие всех ключей.
        for key, default in DEFAULTS.items():
            self._data.setdefault(key, default)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.exception("Не удалось сохранить %s: %s", self.path, exc)

    # ------------------------------------------------------------------ #
    #  Доступ                                                             #
    # ------------------------------------------------------------------ #
    def get(self, key: str, default=None):
        if key not in self._data:
            return default
        return self._data[key]

    def set(self, key: str, value) -> None:
        self._data[key] = _validate(key, value)

    def as_dict(self) -> dict:
        return dict(self._data)

    def hotkey_pynput(self) -> str | None:
        """Хоткей в формате, который понимает pynput (или None)."""
        return normalize_hotkey(self._data.get("hotkey"))

    # ------------------------------------------------------------------ #
    #  Путь к модели                                                      #
    # ------------------------------------------------------------------ #
    def get_model_path(self) -> Path:
        """Финальный путь к ``.argosmodel``.

        Если в конфиге ``model_path`` пуст/None — автоопределение
        (PyInstaller ``_MEIPASS`` либо рядом с проектом в dev)."""
        raw = self._data.get("model_path")
        if raw:
            return Path(raw).expanduser()
        return _default_model_path()

    def validate_model_path(self) -> tuple[bool, str]:
        """Мягкая проверка актуального пути к модели.

        Возвращает ``(True, "OK")`` при нормальном файле;
        ``(False, "<warning>")`` — если файл не найден или расширение
        иное (сохранение/работу это не блокирует, GUI может показать hint).
        """
        return _check_model_file(self.get_model_path())
