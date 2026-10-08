# -*- coding: utf-8 -*-
"""Сентенс-уровневый пайплайн: чистая логика, независимая от GUI и моделей.

Два уровня (не путать):

Уровень A — логические предложения (юниты):
    текст → предложение 1 → предложение 2 → ...
    (split_units: абзацы → предложения, смещения в исходном тексте).

Уровень B — технические inference chunks:
    длинное предложение → chunk 1 → chunk 2 → ...
    (реализуется в translator.OfflineTranslator._split_sentence_to_chunks —
    существующий token-aware лимит остаётся единственным источником истины
    для ограничения входа модели).

Одно логическое предложение может состоять из нескольких технических
chunks; пользователю показывается только завершённый перевод предложения
(chunks склеиваются внутри translator.translate_stream).

Модуль содержит только операции с текстом (без tkinter и без torch):
- split_units / Unit       — разбивка на логические предложения;
- StreamUnit               — юнит в событиях инкрементального перевода;
- assemble_output          — сборка финального перевода (те же правила
                             склейки, что у OfflineTranslator.translate);
- line_offsets / off_to_tk / tk_to_off — перевод смещений в Tk-индексы
                             (для подсветки предложений, перенос из
                             старой версии gui_ctk.py).
"""
import bisect
import re
from dataclasses import dataclass

# Граница предложения: после . ! ? … пробел/перевод строки — то же правило,
# что в translator.OfflineTranslator._split_text (история: gui_ctk.py старой
# версии). Граница абзаца: пустая строка (одна или несколько).
_SENT_RE = re.compile(r'(?<=[.!?…])\s+')
_PARA_RE = re.compile(r'\n\s*\n+')


@dataclass(frozen=True)
class Unit:
    """Логическое предложение с позицией в исходном тексте.

    text — точный фрагмент исходного текста (без изменений);
    start/end — символьные смещения [start, end) в исходном тексте;
    new_paragraph — True, если юнит начинает новый абзац относительно
    предыдущего юнита (False у первого юнита текста).
    """
    text: str
    start: int
    end: int
    new_paragraph: bool
    separator_before: str | None = None
    separator_after: str = ""


@dataclass(frozen=True)
class StreamUnit:
    """Юнит в событиях инкрементального перевода (идут в GUI-очередь).

    translation заполняется на фазе "done" (на фазе "start" — "").
    src_* — смещения в исходном тексте; положение перевода в поле вывода
    GUI вычисляет сам (позиция последнего добавления).
    """
    src: str
    src_start: int
    src_end: int
    new_paragraph: bool
    translation: str = ""
    separator_before: str | None = None
    separator_after: str = ""


class SentenceSegmenter:
    """Sentence boundaries with line breaks and common abbreviations protected."""
    abbreviations = {"dr", "mr", "mrs", "ms", "prof", "sr", "jr", "e.g", "i.e",
                     "etc", "vs", "т.д", "т.п", "т.е", "г", "ул", "рис", "им"}

    def spans(self, text):
        start = 0
        previous_end = 0
        for match in re.finditer(r"\s+", text):
            before = text[previous_end:match.start()]
            newline = "\n" in match.group() or "\r" in match.group()
            boundary = before.endswith((".", "!", "?", "…"))
            if boundary and not newline and before.endswith("."):
                token = re.search(r"([\w.]+)\.$", before)
                if token:
                    word = token.group(1)
                    if word.lower() in self.abbreviations:
                        boundary = False
            if newline or boundary:
                yield start, match.start()
                start = match.end()
            previous_end = match.end()
        yield start, len(text)


def split_units(text: str) -> list:
    """Store exact whitespace between units independently of inference text."""
    units = []
    previous_end = 0
    for start, end in SentenceSegmenter().spans(text):
        segment = text[start:end]
        if not segment.strip():
            continue
        left = len(segment) - len(segment.lstrip())
        right = len(segment.rstrip())
        start, end = start + left, start + right
        separator = text[previous_end:start]
        units.append(Unit(text[start:end], start, end,
                          bool(units) and bool(_PARA_RE.search(separator)),
                          separator_before=separator))
        previous_end = end
    if units:
        from dataclasses import replace
        units[-1] = replace(units[-1], separator_after=text[previous_end:])
    return units


def assemble_output(units: list, translations: list) -> str:
    parts = []
    for index, (unit, translation) in enumerate(zip(units, translations)):
        separator = unit.separator_before
        if separator is None:
            separator = ("\n\n" if unit.new_paragraph else " ") if index else ""
        parts.append(separator + translation + unit.separator_after)
    return "".join(parts)


def line_offsets(text: str) -> list:
    """Абсолютные позиции начал строк текста (первая — 0)."""
    offs = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            offs.append(i + 1)
    return offs


def off_to_tk(text: str, off: int) -> str:
    """Символьное смещение в тексте -> Tk-индекс 'line.col'.

    Линия — с единицы, столбец — с нуля (формат индексов tkinter.Text).
    Смещение за пределами текста зажимается в допустимый диапазон.
    """
    n = len(text)
    off = max(0, min(off, n))
    offs = line_offsets(text)
    line = bisect.bisect_right(offs, off) - 1
    line = max(0, min(line, len(offs) - 1))
    return f"{line + 1}.{off - offs[line]}"


def tk_to_off(text: str, idx: str) -> int:
    """Tk-индекс 'line.col' -> символьное смещение в тексте (обратное
    преобразование к off_to_tk; используется для чтения ranges тегов)."""
    try:
        line_s, col_s = idx.split(".", 1)
        return line_offsets(text)[int(line_s) - 1] + int(col_s)
    except (ValueError, IndexError):
        return 0
