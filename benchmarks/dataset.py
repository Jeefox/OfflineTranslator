# -*- coding: utf-8 -*-
"""Benchmark dataset OfflineTranslator (EN↔RU).

Dataset — фиксированный файл benchmarks/data/dataset.jsonl (JSON Lines).
Он зафиксирован ДО запуска benchmark и не меняется по результатам
моделей: цель — измерение, а не демонстрация лучшего результата.
Сложные/неоднозначные примеры НЕ удаляются — они помечаются
category="ambiguity".

Формат примера (один JSON-объект на строку):
    {"id": "en001", "direction": "en-ru",
     "source": "Hello, world!", "category": "basic",
     "reference": "Привет, мир!", "exact": true}

Поля:
    id         — уникален в dataset (str, формат enNNN/ruNNN);
    direction  — "en-ru" | "ru-en" (явное направление, как в приложении);
    source     — непустой текст;
    category   — одно из CATEGORIES;
    reference  — опционально: эталонный перевод. Используется для
                 chrF. Для естественных предложений reference — не
                 «единственно правильный» вариант, а один корректный;
    exact      — опционально bool (по умолчанию false): reference
                 ожидается строгим (детерминированный пример) — только
                 такие примеры входят в метрику exact match.
"""
import json
import os

#: Категории dataset (12). См. README: распределение приблизительное,
#: равночисленность не требуется.
CATEGORIES = (
    "basic", "conversational", "technical", "terminology", "names",
    "numbers", "punctuation", "urls", "long_sentence", "multi_sentence",
    "ambiguity", "formatting",
)

#: Направления, поддерживаемые приложением.
DIRECTIONS = ("en-ru", "ru-en")

REQUIRED_FIELDS = ("id", "direction", "source", "category")


def dataset_path(path=None):
    """Путь к dataset (по умолчанию benchmarks/data/dataset.jsonl)."""
    if path:
        return path
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "dataset.jsonl")


def _validate(example, lineno):
    if not isinstance(example, dict):
        raise ValueError("line %d: пример — не JSON-объект" % lineno)
    for field in REQUIRED_FIELDS:
        if field not in example or example[field] is None:
            raise ValueError("line %d: нет поля %r" % (lineno, field))
    if not isinstance(example["id"], str) or not example["id"].strip():
        raise ValueError("line %d: id — непустая строка" % lineno)
    if example["direction"] not in DIRECTIONS:
        raise ValueError(
            "line %d: direction %r (допустимо: %s)"
            % (lineno, example["direction"], ", ".join(DIRECTIONS)))
    if not isinstance(example["source"], str) or not example["source"].strip():
        raise ValueError("line %d: source — непустая строка" % lineno)
    if example["category"] not in CATEGORIES:
        raise ValueError(
            "line %d: category %r (допустимо: %s)"
            % (lineno, example["category"], ", ".join(CATEGORIES)))
    ref = example.get("reference")
    if ref is not None and (not isinstance(ref, str) or not ref.strip()):
        raise ValueError("line %d: reference — непустая строка или null" % lineno)
    exact = example.get("exact")
    if exact is not None and not isinstance(exact, bool):
        raise ValueError("line %d: exact — bool или null" % lineno)


def load_dataset(path=None):
    """Загружает и валидирует dataset.

    Возвращает список примеров (слова dict) в порядке файла.
    Ошибки: FileNotFoundError (нет файла), ValueError (плохая строка,
    невалидный пример, дубликат id, пустой dataset).
    """
    path = dataset_path(path)
    if not os.path.isfile(path):
        raise FileNotFoundError("dataset не найден: %r" % path)
    examples = []
    seen = set()
    with open(path, "r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                example = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError("line %d: некорректный JSON: %s" % (lineno, e)) from e
            _validate(example, lineno)
            if example["id"] in seen:
                raise ValueError("line %d: дубликат id %r" % (lineno, example["id"]))
            seen.add(example["id"])
            examples.append(example)
    if not examples:
        raise ValueError("пустой dataset: %r" % path)
    return examples


def validate_example(example):
    """Валидирует один пример (для тестов). ValueError при ошибке."""
    _validate(example, 0)


def summary(examples):
    """Сводка по dataset: total, по направлениям, по категориям,
    количество с reference / с exact-reference."""
    per_dir, per_cat, n_ref, n_exact = {}, {}, 0, 0
    for ex in examples:
        per_dir[ex["direction"]] = per_dir.get(ex["direction"], 0) + 1
        per_cat[ex["category"]] = per_cat.get(ex["category"], 0) + 1
        if ex.get("reference"):
            n_ref += 1
        if ex.get("reference") and ex.get("exact"):
            n_exact += 1
    return {
        "total": len(examples),
        "per_direction": per_dir,
        "per_category": per_cat,
        "with_reference": n_ref,
        "exact_reference": n_exact,
    }
