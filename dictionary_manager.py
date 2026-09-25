# -*- coding: utf-8 -*-
"""Словарь: общий core (snapshot) + DictionaryManager — единственный
писатель dictionary.json.

Логическая модель:
- словарь — набор логических пар (EN, RU). Пара двунаправленная: точный
  lookup работает и как en -> ru, и как ru -> en; направление перевода
  ('en-ru'/'ru-en') определяет только выбор нейросетевой модели;
- физическое представление — flat JSON "ключ -> значение" (формат
  совместим с существующим dictionary.json); при сохранении каждая пара
  пишется в обоих направлениях (зеркальные записи), как в текущем файле;
- нормализация в одном месте (этот файл): ключи — strip()+lower(),
  значения — strip() с сохранением регистра; lookup выполняется по lower();
- политика 1:1 (O3): у термина ровно один партнёр. Запись пары в конфликте
  с существующей запрещена (см. DictionaryManager.find_conflict); зеркало
  той же пары (a -> b и b -> a) конфликтом НЕ является;
- legacy-файлы (зеркала, конфликты) загружаются с детерминированным
  last-wins: более поздняя в файле запись побеждает, с предупреждением.

OfflineTranslator не хранит словарь в памяти: в начале каждого translate()
он читает свежий snapshot (load_snapshot), поэтому изменения словаря через
DictionaryManager видны без перезапуска приложения.
"""

import json
import os
import shutil
import sys
import tempfile
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Нормализация (единая реализация для всего проекта)
# ---------------------------------------------------------------------------

def _norm_key(text: str) -> str:
    """Ключ: strip + lower. Lookup по словарю регистронезависим."""
    return text.strip().lower()


def _norm_value(text: str) -> str:
    """Значение: strip, регистр сохраняется."""
    return text.strip()


def _is_cyrillic(text: str) -> bool:
    """Содержит ли текст кириллицу (эвристика языка для пары (en, ru))."""
    return any("\u0400" <= ch <= "\u04FF" for ch in text)


def orient_pair(key: str, value: str) -> Tuple[str, str]:
    """Неориентированная запись (key, value) -> логическая пара (en, ru).

    Язык определяется письменностью: сторона с кириллицей — RU. Если
    неоднозначно (кириллица с обеих сторон или ни с одной) — сохраняется
    порядок (key, value). Результат нормализован: (en — lower, ru — с
    сохранением регистра).
    """
    if _is_cyrillic(value) and not _is_cyrillic(key):
        en, ru = key, value
    elif _is_cyrillic(key) and not _is_cyrillic(value):
        en, ru = value, key
    else:
        en, ru = key, value
    return _norm_key(en), _norm_value(ru)


# ---------------------------------------------------------------------------
# Путь к dictionary.json
# ---------------------------------------------------------------------------

def user_data_dir() -> str:
    """Постоянная user-директория приложения (та же логика, что у
    CacheManager.default_cache_dir):
    - Windows: %LOCALAPPDATA%\\OfflineTranslator;
    - Linux/macOS: $XDG_CACHE_HOME/OfflineTranslator
      (или ~/.cache/OfflineTranslator, если переменная не задана).
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_CACHE_HOME")
                or os.path.join(os.path.expanduser("~"), ".cache"))
    return os.path.join(base, "OfflineTranslator")


def default_dictionary_path() -> str:
    """Путь к dictionary.json: абсолютный, не зависит от CWD.

    - development (python main.py): dictionary.json в корне проекта;
    - frozen EXE: %LOCALAPPDATA%\\OfflineTranslator\\dictionary.json
      (на Unix — user-директория из user_data_dir()).

    sys._MEIPASS как writable-директория НЕ используется: bundled-копия
    (_MEIPASS/dictionary.json) — только read-only источник для первичного
    копирования при первом запуске; существующий пользовательский словарь
    не перезаписывается.
    """
    if not getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "dictionary.json")
    path = os.path.join(user_data_dir(), "dictionary.json")
    if not os.path.exists(path):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            bundled = os.path.join(meipass, "dictionary.json")
            if os.path.exists(bundled):
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    shutil.copy2(bundled, path)
                except Exception as e:
                    print(f"⚠ Не удалось скопировать словарь из бандла EXE: {e}")
    return path

# ---------------------------------------------------------------------------
# Core: snapshot (единая загрузка словаря для DictionaryManager и
# OfflineTranslator)
# ---------------------------------------------------------------------------

def _read_json(path: str) -> Optional[dict]:
    """Читает JSON-файл с fallback по кодировкам (utf-8-sig, utf-8, cp1251).
    При любой ошибке (файл отсутствует, повреждён, нераспознаваемая
    кодировка) возвращает None."""
    if not os.path.exists(path):
        return None
    try:
        for encoding in ("utf-8-sig", "utf-8", "cp1251"):
            try:
                with open(path, "r", encoding=encoding) as f:
                    return json.load(f)
            except UnicodeDecodeError:
                continue
    except Exception as e:
        print(f"⚠ Ошибка чтения файла словаря ({path}): {e}")
    return None


class DictionarySnapshot:
    """Неизменяемый снимок словаря: логические пары + двунаправленная
    таблица точного совпадения.

    lookup — direction-agnostic: термин (lower) -> партнёр. Таблица
    содержит оба направления каждой пары, поэтому направление
    'en-ru'/'ru-en' не влияет на словарный lookup.
    """

    def __init__(self, pairs: List[Tuple[str, str]], lookup: Dict[str, str]):
        self.pairs = pairs    # логические пары (en, ru)
        self._lookup = lookup

    def lookup(self, text: str) -> Optional[str]:
        """Точное совпадение: text.strip().lower() -> партнёр, или None."""
        return self._lookup.get(_norm_key(text))

    def get_pairs(self) -> List[Tuple[str, str]]:
        """Копия списка логических пар (en, ru) без зеркальных дубликатов."""
        return list(self.pairs)

    def __len__(self) -> int:
        return len(self.pairs)


def _pairs_from_lookup(lookup: Dict[str, str]) -> List[Tuple[str, str]]:
    """Выводит логические пары из итоговой lookup-таблицы (для GUI).
    Само-запись (x -> x) парой не считается. Порядок — алфавитный."""
    edges = set()
    for a, b_disp in lookup.items():
        b_id = _norm_key(b_disp)
        if a != b_id:
            edges.add(frozenset((a, b_id)))
    pairs = []
    for edge in sorted(edges, key=lambda e: sorted(e)):
        a, b = sorted(edge)
        # Отображаемая форма стороны хранится в записи её партнёра:
        # lookup[b] — как отображается a; lookup[a] — как отображается b.
        disp_a = lookup.get(b, a)
        disp_b = lookup.get(a, b)
        if _is_cyrillic(a) and not _is_cyrillic(b):
            en, ru = disp_b, disp_a   # a — RU-сторона
        elif _is_cyrillic(b) and not _is_cyrillic(a):
            en, ru = disp_a, disp_b   # b — RU-сторона
        else:
            en, ru = disp_a, disp_b   # письменность неоднозначна: детерм. порядок
        pairs.append((_norm_key(en), _norm_value(ru)))
    pairs.sort(key=lambda p: (p[0], p[1]))
    return pairs


def build_snapshot(data) -> Optional[DictionarySnapshot]:
    """Собирает snapshot из разобранных JSON-данных.

    Возвращает None, если top-level — не dict.

    Записи обрабатываются в порядке файла, last-wins (legacy-поведение):
    для одного термина более поздняя запись побеждает более раннюю (с
    предупреждением). Зеркальные записи (a -> b и b -> a) — одна
    логическая пара, конфликтом не являются.
    """
    if not isinstance(data, dict):
        print("⚠ dictionary.json: top-level не JSON-объект — словарь не загружен")
        return None
    lookup: Dict[str, str] = {}

    def _put(term_id: str, partner_disp: str):
        prev = lookup.get(term_id)
        if prev is not None and _norm_key(prev) != _norm_key(partner_disp):
            print(f"⚠ Словарь: термин «{term_id}» уже связан с «{prev}» — "
                  f"действует более поздняя связь «{partner_disp}»")
        lookup[term_id] = partner_disp

    for key, value in data.items():
        if not isinstance(key, str) or not isinstance(value, str):
            print(f"⚠ Словарь: пропущена запись со нестроковыми "
                  f"ключом/значением: {key!r} -> {value!r}")
            continue
        k = _norm_key(key)
        v = _norm_value(value)
        if not k or not v:
            continue
        _put(k, v)             # прямое направление
        _put(_norm_key(v), k)  # обратное (форма ключа = lower-форма)
    return DictionarySnapshot(_pairs_from_lookup(lookup), lookup)


def load_snapshot(path: str) -> Optional[DictionarySnapshot]:
    """Единственная реализация загрузки словаря: flat JSON -> нормализованный
    snapshot. Если файл отсутствует/повреждён/невалиден — None; вызывающий
    продолжает перевод без словаря (graceful fallback)."""
    data = _read_json(path)
    if data is None:
        return None
    return build_snapshot(data)


def serialize_pairs(pairs: List[Tuple[str, str]]) -> Dict[str, str]:
    """Логические пары -> flat JSON-объект (формат совместим с
    dictionary.json): каждая пара пишется в обоих направлениях
    (зеркальные записи)."""
    out: Dict[str, str] = {}
    for en, ru in pairs:
        out[_norm_key(en)] = ru
        out[_norm_key(ru)] = en
    return out

# ---------------------------------------------------------------------------
# DictionaryManager: единственный изменяющий словарь компонент
# ---------------------------------------------------------------------------

class DictionaryManager:
    """Единственный писатель dictionary.json + in-memory состояние для GUI.

    Каждое изменение: нормализация -> валидация -> проверка конфликта 1:1 ->
    сборка нового состояния -> атомарное сохранение (tmp-файл + os.replace)
    -> и только после успешного сохранения в памяти обновляется состояние.
    Поэтому рассинхронизация «память изменена, файл не сохранён» невозможна.
    """

    def __init__(self, dictionary_path: Optional[str] = None):
        """Args:
            dictionary_path: путь к dictionary.json. Если не указан —
                default_dictionary_path() (проект в dev, user-директория
                в EXE).
        """
        self.dictionary_path = dictionary_path or default_dictionary_path()
        self._pairs: List[Tuple[str, str]] = []
        self.last_error: Optional[str] = None
        self.load()

    # -- загрузка -----------------------------------------------------------

    def load(self) -> bool:
        """Читает словарь из файла (только память, без сохранения)."""
        snapshot = load_snapshot(self.dictionary_path)
        if snapshot is None:
            self.last_error = f"Словарь не загружен: {self.dictionary_path}"
            return False
        self._pairs = snapshot.get_pairs()
        self.last_error = None
        return True

    def reload(self) -> bool:
        """Перечитывает словарь из файла (после внешнего изменения, например
        ручной правки dictionary.json). При ошибке сохраняет текущее
        состояние."""
        return self.load()

    # -- сохранение ------------------------------------------------------------

    def _save_pairs(self, pairs: List[Tuple[str, str]]) -> bool:
        """Атомарное сохранение: временный файл в той же директории +
        os.replace(). При ошибке исходный файл не трогается, временный
        файл удаляется."""
        data = serialize_pairs(pairs)
        directory = os.path.dirname(self.dictionary_path) or "."
        try:
            fd, tmp_path = tempfile.mkstemp(prefix=".dictionary-",
                                            suffix=".tmp", dir=directory)
        except Exception as e:
            print(f"✗ Не удалось создать временный файл для словаря: {e}")
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, self.dictionary_path)
            return True
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            print(f"✗ Ошибка сохранения словаря: {e}")
            return False

    def _apply(self, new_pairs: List[Tuple[str, str]]) -> bool:
        """Сохраняет новое состояние атомарно; в памяти обновляется только
        после успешного сохранения."""
        if self._save_pairs(new_pairs):
            self._pairs = new_pairs
            return True
        return False

    # -- политика 1:1 (O3) -------------------------------------------------------

    @staticmethod
    def _same_pair(e_id: str, r_id: str, en_id: str, ru_id: str) -> bool:
        """Пары совпадают (в любой ориентации — зеркало той же пары)."""
        return ((e_id == en_id and r_id == ru_id)
                or (e_id == ru_id and r_id == en_id))

    @staticmethod
    def _conflict_in(pairs: List[Tuple[str, str]],
                     en_id: str, ru_id: str) -> Optional[Tuple[str, str]]:
        """Первая пара, конфликтующая с (en_id, ru_id), или None.

        Конфликт: пара делит ровно ОДИН термин с запрашиваемой (EN- или
        RU-термин уже связан с другим партнёром) — в любой ориентации
        записи. Та же пара (в т.ч. в зеркальном виде) — не конфликт.
        """
        for e, r in pairs:
            e_id, r_id = _norm_key(e), _norm_key(r)
            if DictionaryManager._same_pair(e_id, r_id, en_id, ru_id):
                continue
            if e_id in (en_id, ru_id) or r_id in (en_id, ru_id):
                return (e, r)
        return None

    @staticmethod
    def _has_pair(pairs: List[Tuple[str, str]], en_id: str, ru_id: str) -> bool:
        """Пара уже есть (в любой ориентации)."""
        return any(DictionaryManager._same_pair(_norm_key(e), _norm_key(r),
                                                en_id, ru_id)
                   for e, r in pairs)

    def find_conflict(self, en: str, ru: str) -> Optional[Tuple[str, str]]:
        """Пара, конфликтующая с (en, ru) по политике 1:1, или None.

        Для GUI: позволяет показать пользователю, с какой парой конфликт.
        Зеркало той же пары (ru -> en) конфликтом не является.
        """
        return self._conflict_in(self._pairs, _norm_key(en), _norm_key(ru))

    # -- изменения -----------------------------------------------------------------

    def add(self, en: str, ru: str) -> bool:
        """Добавляет логическую пару (EN, RU) и атомарно сохраняет словарь.

        False: пустые данные или конфликт 1:1 (см. find_conflict).
        Добавление уже существующей пары (в т.ч. в зеркальном виде) —
        не ошибка: True, ничего не меняется.
        """
        en_id = _norm_key(en)
        ru_disp = _norm_value(ru)
        ru_id = _norm_key(ru_disp)
        if not en_id or not ru_id:
            return False
        if self._has_pair(self._pairs, en_id, ru_id):
            return True
        conflict = self._conflict_in(self._pairs, en_id, ru_id)
        if conflict is not None:
            print(f"✗ Конфликт 1:1: «{en_id}»/«{ru_id}» уже занята парой "
                  f"«{conflict[0]}» ↔ «{conflict[1]}»")
            return False
        return self._apply(self._pairs + [(en_id, ru_disp)])

    def edit(self, old_en: str, new_en: str, new_ru: str) -> bool:
        """Редактирует пару: находит старую по old_en (по любому из её
        терминов — при 1:1 это однозначно), проверяет новые данные на
        конфликт 1:1 (без учёта самой редактируемой пары) и атомарно
        сохраняет. Зеркальная запись обновляется автоматически."""
        old_id = _norm_key(old_en)
        new_id = _norm_key(new_en)
        new_ru_disp = _norm_value(new_ru)
        new_ru_id = _norm_key(new_ru_disp)
        if not old_id or not new_id or not new_ru_id:
            return False
        old_pair = None
        for p in self._pairs:
            if _norm_key(p[0]) == old_id or _norm_key(p[1]) == old_id:
                old_pair = p
                break
        if old_pair is None:
            return False
        rest = [p for p in self._pairs if p is not old_pair]
        conflict = self._conflict_in(rest, new_id, new_ru_id)
        if conflict is not None:
            print(f"✗ Конфликт 1:1: «{new_id}»/«{new_ru_id}» уже занята парой "
                  f"«{conflict[0]}» ↔ «{conflict[1]}»")
            return False
        if not self._has_pair(rest, new_id, new_ru_id):
            rest = rest + [(new_id, new_ru_disp)]
        return self._apply(rest)

    def delete(self, en: str, ru: Optional[str] = None) -> bool:
        """Удаляет логическую пару (зеркальную запись — вместе с ней).

        Если ru указан — удаляется ровно пара (en, ru) в любой ориентации;
        иначе — пара, содержащая термин en (при 1:1 это однозначно).
        """
        en_id = _norm_key(en)
        ru_id = _norm_key(ru) if ru is not None else None
        if not en_id or (ru_id is not None and not ru_id):
            return False
        target = None
        for p in self._pairs:
            e_id, r_id = _norm_key(p[0]), _norm_key(p[1])
            if ru_id is not None:
                if ((e_id == en_id and r_id == ru_id)
                        or (e_id == ru_id and r_id == en_id)):
                    target = p
                    break
            elif e_id == en_id or r_id == en_id:
                target = p
                break
        if target is None:
            return False
        return self._apply([p for p in self._pairs if p is not target])

    # -- чтение (для GUI) -------------------------------------------------------------

    def get_all(self) -> List[Tuple[str, str]]:
        """Все логические пары (en, ru) по алфавиту, без зеркальных
        дубликатов."""
        return sorted(self._pairs,
                      key=lambda p: (_norm_key(p[0]), _norm_key(p[1])))

    def search(self, query: str) -> List[Tuple[str, str]]:
        """Подстрока (без учёта регистра) в любом из двух терминов пары."""
        q = _norm_key(query)
        if not q:
            return []
        return [p for p in self.get_all()
                if q in _norm_key(p[0]) or q in _norm_key(p[1])]

    def get_count(self) -> int:
        """Количество логических пар."""
        return len(self._pairs)

    # -- импорт/экспорт -----------------------------------------------------------------

    def import_from_json(self, filepath: str) -> Tuple[int, int]:
        """Импорт пар из flat JSON-файла. Возвращает (added, errors).

        Файл полностью читается, валидируется и подготавливается ДО
        применения: память меняется и файл сохраняется (ОДИН раз,
        атомарно) только после успешной подготовки. Некорректная запись
        (не строки, пустая) — ошибки +1, импорт продолжается; конфликт 1:1
        — запись не импортируется, ошибки +1; top-level не dict — импорт не
        применяется.
        """
        data = _read_json(filepath)
        if data is None or not isinstance(data, dict):
            print(f"✗ Импорт: {filepath} — не валидный JSON-словарь")
            return (0, 1)
        new_pairs = list(self._pairs)
        added = 0
        errors = 0
        for key, value in data.items():
            if not isinstance(key, str) or not isinstance(value, str):
                errors += 1
                continue
            k = _norm_key(key)
            v = _norm_value(value)
            if not k or not v:
                errors += 1
                continue
            en, ru = orient_pair(k, v)
            if self._has_pair(new_pairs, en, _norm_key(ru)):
                continue  # уже есть (в т.ч. зеркало) — ни добавление, ни ошибка
            if self._conflict_in(new_pairs, en, _norm_key(ru)) is not None:
                errors += 1  # конфликт 1:1 — не импортируется
                continue
            new_pairs.append((en, ru))
            added += 1
        if added and not self._apply(new_pairs):
            # Ошибка сохранения: память не изменилась (рассинхронизации нет)
            return (0, errors + 1)
        return (added, errors)

    def export_to_json(self, filepath: str) -> bool:
        """Экспорт текущего словаря в отдельный файл (тот же flat-формат,
        атомарно)."""
        data = serialize_pairs(self._pairs)
        try:
            directory = os.path.dirname(os.path.abspath(filepath)) or "."
            fd, tmp_path = tempfile.mkstemp(prefix=".dictionary-",
                                            suffix=".tmp", dir=directory)
        except Exception as e:
            print(f"✗ Ошибка экспорта словаря: {e}")
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            os.replace(tmp_path, filepath)
            return True
        except Exception as e:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            print(f"✗ Ошибка экспорта словаря: {e}")
            return False