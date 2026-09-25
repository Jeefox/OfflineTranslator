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


def split_units(text: str) -> list:
    """Разбивает текст на логические предложения (абзацы → предложения).

    Правила совпадают с translator.OfflineTranslator._split_text:
    абзацы — блоки между пустыми строками; предложение заканчивается по
    . ! ? … и последующим пробелам. Порядок сохраняется; разделители
    (пробелы) в юниты не входят — восстанавливаются при сборке.
    """
    units = []
    # Разделитель абзацев НЕ входит ни в предыдущий, ни в следующий
    # (ровно как в re.split): абзац заканчивается в m.start(), следующий
    # начинается в m.end().
    para_seps = [(m.start(), m.end()) for m in _PARA_RE.finditer(text)]
    para_starts = [0] + [e for _s, e in para_seps]
    para_ends = [s for s, _e in para_seps] + [len(text)]
    for ps, pe in zip(para_starts, para_ends):
        para = text[ps:pe]
        # Границы предложений внутри абзаца: разделитель (пробел(ы) после
        # .!?…) НЕ входит ни в предыдущее, ни в следующее предложение —
        # ровно как в re.split (правило translator._split_text).
        seps = [(m.start(), m.end()) for m in _SENT_RE.finditer(para)]
        bounds = []
        prev = 0
        for s, e in seps:
            bounds.append((prev, s))
            prev = e
        bounds.append((prev, len(para)))
        first_in_para = True
        for s, e in bounds:
            seg = para[s:e]
            if seg.strip():
                units.append(Unit(seg, ps + s, ps + e,
                                  new_paragraph=first_in_para and bool(units)))
                first_in_para = False
    return units


def assemble_output(units: list, translations: list) -> str:
    """Собирает финальный перевод из переводов логических предложений.

    Правила склейки совпадают с OfflineTranslator.translate: предложения
    внутри абзаца соединяются пробелом, абзацы — пустой строкой.
    """
    paras = []
    cur = []
    for u, t in zip(units, translations):
        if u.new_paragraph and cur:
            paras.append(" ".join(cur))
            cur = []
        cur.append(t)
    if cur:
        paras.append(" ".join(cur))
    return "\n\n".join(paras)


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
