# -*- coding: utf-8 -*-
"""Хранилище настроек приложения (settings.json).

Механизм перенесён из старой версии (origin/master:settings.py) и переиспользует
пользовательскую директорию проекта (dictionary_manager.user_data_dir):
  Linux/macOS — ~/.cache/OfflineTranslator/settings.json
  Windows     — %LOCALAPPDATA%/OfflineTranslator/settings.json
Путь можно переопределить переменной окружения OFFLINE_TRANSLATE_CONFIG
(абсолютный путь к файлу или каталогу) — удобно для тестов.

Валидация при ``set()``: невалидное значение НЕ применяется (метод возвращает
False, состояние не меняется). Невалидные значения в файле при загрузке
заменяются дефолтами.

Формат хоткея — дружелюбная форма pynput ("ctrl+alt+t"); для передачи в pynput
используется ``normalize_hotkey`` (даёт "<ctrl>+<alt>+t").
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

try:  # pynput опционален: без него проверка хоткея — по паттерну
    from pynput.keyboard import HotKey
except Exception:  # noqa: BLE001 (нет pynput, нет X/Xlib и т.п.)
    HotKey = None

from dictionary_manager import user_data_dir

logger = logging.getLogger("offline_translate.settings")

# --- Дефолтные значения -------------------------------------------------- #
DEFAULTS: dict = {
    "hotkey": "ctrl+alt+t",        # глобальный хоткей перевода (формат pynput)
    "source_lang": "en",            # стартовое направление при запуске
    "target_lang": "ru",
    "theme": "dark",                # "dark" | "light"
    "autotranslate": True,          # автоперевод после остановки набора
    "slow_after_sec": 3,            # задержка статуса «Перевод...» (0 — сразу)
    "debounce_sec": 1.5,            # debounce ввода (сек.)
    "max_text_length": 5000,        # лимит символов (0 = без лимита)
    "filter_cyrillic": True,        # автоопределение направления по написанию
}

# Текущая архитектура поддерживает только EN/RU (модели Helsinki-NLP opus-mt),
# поэтому списки языков уже, чем в старой версии.
_ALLOWED = {
    "source_lang": {"en", "ru"},
    "target_lang": {"en", "ru"},
    "theme": {"dark", "light"},
}

# --- Хоткей -------------------------------------------------------------- #
# Модификаторы, которые pynput понимает без угловых скобок.
_MODIFIERS = {"ctrl", "alt", "shift", "cmd", "command", "win", "super", "option", "alt_gr"}
_HOTKEY_RE = re.compile(r"^[\w<>()+]+$")
_HK_FUNC_RE = re.compile(r"^f([1-9]|1[0-9]|2[0-4])$")
# «Простые» клавиши, допустимые как главная клавиша.
_HK_NAMES = {
    "space", "enter", "backspace", "tab", "esc", "delete", "home", "end",
    "page_up", "page_down", "up", "down", "left", "right", "comma", "period",
    "slash", "semicolon", "apostrophe", "minus", "equal", "grave",
    "bracketleft", "bracketright", "backslash",
}


def _default_config_path() -> Path:
    env = os.environ.get("OFFLINE_TRANSLATE_CONFIG")
    if env:
        p = Path(env).expanduser()
        return p if p.suffix else p / "settings.json"
    return Path(user_data_dir()) / "settings.json"


def normalize_hotkey(hotkey: str) -> str | None:
    """Приводит дружелюбную форму ``ctrl+alt+t`` к парсируемой pynput
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
    has_main = False  # комбинация из одних модификаторов невалидна
    for part in parts:
        name = part[1:-1].lower() if part.startswith("<") and part.endswith(">") else part.lower()
        if name in _MODIFIERS:
            if name in ("command", "win", "super", "option"):
                name = "cmd"
            normalized.append(f"<{name}>")
        elif len(name) == 1 and name.isalnum():
            normalized.append(name)
            has_main = True
        elif _HK_FUNC_RE.match(name) or name in _HK_NAMES:
            normalized.append(name)
            has_main = True
        else:
            return None
    if not has_main or len(normalized) != len(set(normalized)):
        return None
    result = "+".join(normalized)
    # Перепроверка парсером самого pynput — он и есть источник правды.
    if HotKey is not None:
        try:
            HotKey.parse(result)
        except ValueError:
            return None
    return result


def _coerce_number(value):
    """int/float — как есть; числовая строка конвертируется (GUI передаёт
    строки из полей ввода); bool/мусор -> None."""
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


def validate_value(key: str, value) -> tuple[bool, object]:
    """Валидирует значение настройки.

    Возвращает ``(True, нормализованное_значение)`` или ``(False, дефолт)``.
    Отдельно от ``Settings.set`` — чтобы GUI мог проверить все поля ДО
    применения (иначе при ошибке в одном поле остальные уже «утекли» в память).
    """
    if key == "hotkey":
        normalized = normalize_hotkey(value)
        if normalized is None:
            return False, DEFAULTS["hotkey"]
        # Храним дружелюбную форму, нормализованную при использовании.
        return True, normalized.replace("<", "").replace(">", "")
    if key in _ALLOWED:
        if not isinstance(value, str) or value.lower() not in _ALLOWED[key]:
            return False, DEFAULTS[key]
        return True, value.lower()
    if key in ("autotranslate", "filter_cyrillic"):
        if not isinstance(value, bool):
            return False, DEFAULTS[key]
        return True, value
    if key == "debounce_sec":
        # Минимум 0.1 с: «на каждую букву» перевод запускать нельзя.
        num = _coerce_number(value)
        if num is None or not (0.1 <= num <= 60):
            return False, DEFAULTS["debounce_sec"]
        return True, float(num)
    if key == "slow_after_sec":
        num = _coerce_number(value)
        if num is None or not (0 <= num <= 600):
            return False, DEFAULTS["slow_after_sec"]
        return True, float(num)

    if key == "max_text_length":
        num = _coerce_number(value)
        if num is None or not (0 <= num <= 1_000_000):
            return False, DEFAULTS["max_text_length"]
        return True, int(num)
    return False, value


class Settings:
    """Обёртка над ``settings.json`` с валидацией и дефолтами."""

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
                raise ValueError("settings.json должен содержать JSON-объект")
        except (OSError, ValueError) as exc:
            logger.warning("Не удалось прочитать %s (%s) — использую дефолты", self.path, exc)
            self._data = {**DEFAULTS}
            self.save()
            return
        for key, value in raw.items():
            if key in DEFAULTS:
                ok, normalized = validate_value(key, value)
                if ok:
                    self._data[key] = normalized
                else:
                    logger.warning("Недопустимое значение %r для %s в %s — дефолт", value, key, self.path)
            else:
                logger.warning("Неизвестный ключ настроек %r в %s — пропущен", key, self.path)
        for key, default in DEFAULTS.items():
            self._data.setdefault(key, default)
        # Согласованность: исходный и целевой языки должны отличаться.
        if self._data["source_lang"] == self._data["target_lang"]:
            logger.warning("source_lang == target_lang в %s — дефолты", self.path)
            self._data["source_lang"] = DEFAULTS["source_lang"]
            self._data["target_lang"] = DEFAULTS["target_lang"]

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
        return self._data.get(key, default)

    def set(self, key: str, value) -> bool:
        """Применяет валидное значение. False — значение невалидно и НЕ
        применено (состояние не изменилось)."""
        ok, normalized = validate_value(key, value)
        if not ok:
            logger.warning("Недопустимое значение %r для %s — не применено", value, key)
            return False
        self._data[key] = normalized
        return True

    def as_dict(self) -> dict:
        return dict(self._data)
