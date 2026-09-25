# -*- coding: utf-8 -*-
"""Regression-тесты Этапа 4: попредложенический перевод + синхронизация
и подсветка предложений.

Запуск (реальные модели не скачиваются, сеть и pytest не нужны):

    python tests/test_sentence_translation.py

Два уровня проверки:

1. Чистая логика (без GUI и моделей): split_units/assemble_output
   (sentence_pipeline), token-aware лимит чанков
   (OfflineTranslator._split_sentence_to_chunks с фейковым токенизатором),
   off_to_tk/tk_to_off для подсветки.

2. Приложение (реальный дисплей, torch/transformers застаблены,
   OfflineTranslator подменён на FakeTranslator с инкрементальным
   translate_stream — очередь/потоки/debounce/подсветка реальные):
   N предложений -> N логических результатов в правильном порядке;
   длинное предложение из нескольких чанков — ОДИН логический результат;
   каждый чанк <= лимита; постепенное появление перевода (не одним
   финалом); новый generation инвалидирует старый результат; старый
   worker не перезаписывает новый текст; очистка инвалидирует; swap —
   ровно один цикл; пустой текст ничего не запускает; ручной и
   автоматический перевод — один pipeline; подсветка соответствует
   текущему индексу предложения и снимается после завершения;
   resize и Ctrl+A/Ctrl+C во время перевода не ломают состояние.
"""
import contextlib
import os
import re as _re
import sys
import tempfile
import threading
import time
import types

# Корень репозитория — родитель tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Стабы torch/transformers — ПЕРЕД импортом translator (как в других тестах).
fake_torch = types.ModuleType("torch")
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake_torch
fake_transformers = types.ModuleType("transformers")
fake_transformers.AutoTokenizer = type("AutoTokenizer", (), {})
fake_transformers.AutoModelForSeq2SeqLM = type("AutoModelForSeq2SeqLM", (), {})
sys.modules["transformers"] = fake_transformers

import tkinter as tk  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:500]))
    PASS.append(name)


# ---------------------------------------------------------------------
# Часть 1. Чистая логика (без GUI и без реальных моделей)
# ---------------------------------------------------------------------
from sentence_pipeline import (  # noqa: E402
    StreamUnit, assemble_output, line_offsets, off_to_tk, split_units,
    tk_to_off,
)
from translator import OfflineTranslator  # noqa: E402

# --- split_units: уровень A (логические предложения) ---------------------
S3 = "First sentence. Second sentence. Third sentence."
U3 = split_units(S3)
check("3 предложения -> 3 логических юнита", len(U3) == 3, U3)
check("юниты в порядке документа",
      [u.text for u in U3] == ["First sentence.", "Second sentence.",
                               "Third sentence."], U3)
check("смещения юнитов точные (text.find)",
      all(S3.find(u.text, u.start) == u.start and S3[u.start:u.end] == u.text
          for u in U3), [(u.text, u.start, u.end) for u in U3])
check("юниты идут без пересечений, по порядку",
      all(U3[i].end <= U3[i + 1].start for i in range(len(U3) - 1)))
check("один абзац: new_paragraph у всех False",
      all(not u.new_paragraph for u in U3))

UPARA = split_units("Para one. Para two.\n\nPara three. End.")
# new_paragraph — True только у ПЕРВОГО юнита каждого нового абзаца.
check("абзацы: new_paragraph-флаги",
      [u.new_paragraph for u in UPARA] == [False, False, True, False],
      [(u.text, u.new_paragraph) for u in UPARA])
check("абзацы: смещения во втором абзаце корректны",
      all("Para three. End.".find(u.text, u.start - (UPARA[2].start)) >= 0
          for u in UPARA[2:]), [(u.text, u.start, u.end) for u in UPARA[2:]])

UCOMMA = split_units("Long, comma sentence. End.")
check("запятая — не граница предложения",
      [u.text for u in UCOMMA] == ["Long, comma sentence.", "End."], UCOMMA)

UNOEND = split_units("No end punctuation here")
check("без завершающих знаков — одно предложение",
      len(UNOEND) == 1 and UNOEND[0].text == "No end punctuation here", UNOEND)

# --- эквивалентность _split_text до/после рефакторинга -------------------
# Референсная реализация (исходный код translator.py до Этапа 4).
def _ref_split_text(text):
    paragraphs = []
    for paragraph in _re.split(r'\n\s*\n+', text):
        sentences = [s for s in _re.split(r'(?<=[.!?…])\s+', paragraph)
                     if s.strip()]
        if sentences:
            paragraphs.append(sentences)
    return paragraphs


ot = OfflineTranslator.__new__(OfflineTranslator)  # без загрузки моделей
_SPLIT_CASES = ["A. B.\n\nC.", "X", "", "A..\n\n\nB?", "  \n\n  ", "End. ",
                "One. Two! Three?", "Para.\n\n\n\nPara2. Para3.",
                "… Ellipsis test. More.", S3, "A.\nB.\nC.", "\n\n\n"]
for _t in _SPLIT_CASES:
    check("сходство _split_text (до/после) %r" % _t[:18],
          ot._split_text(_t) == _ref_split_text(_t),
          (ot._split_text(_t), _ref_split_text(_t)))

# --- assemble_output: та же сборка, что у translate() ---------------------
_UA = split_units("P1a. P1b.\n\nP2a.")
check("assemble: абзацы и предложения склеиваются как в translate()",
      assemble_output(_UA, ["t1a", "t1b", "t2a"]) == "t1a t1b\n\nt2a")
check("assemble: один юнит", assemble_output(U3[:1], ["only"]) == "only")

# --- уровень B: token-aware лимит чанков (существующий механизм) ---------
class FakeTokenizer:
    """Один «токен» на слово (по пробелам) — под _split_sentence_to_chunks."""

    def __init__(self):
        self.calls = 0

    def __call__(self, text, add_special_tokens=True, **kw):
        self.calls += 1
        return {"input_ids": text.split()}

    def decode(self, ids, skip_special_tokens=True):
        return " ".join(ids)


class CharTokenizer:
    """Один «токен» на символ — для проверки разбивки длинного слова."""

    def __call__(self, text, add_special_tokens=True, **kw):
        return {"input_ids": list(text)}

    def decode(self, ids, skip_special_tokens=True):
        return "".join(ids)


ot.max_source_tokens = 4
_tok = FakeTokenizer()


def _check_chunks(sentence, limit=4, tokenizer=None, preserve="words"):
    chunks = ot._split_sentence_to_chunks(sentence, tokenizer or _tok)
    for c in chunks:
        check("чанк <= max_source_tokens (%r)" % c[:20],
              len(c.split()) <= limit, (c, len(c.split())))
    if preserve == "words":
        # Слова не теряются и порядок сохраняется (пробелы —
        # восстанавливаемые разделители, как и в основном переводе).
        check("слова сохранены: %r" % sentence[:24],
              " ".join(chunks).split() == sentence.split(),
              (sentence, chunks))
    else:  # preserve == "chars": символьная разбивка длинного слова
        check("символы сохранены: %r" % sentence[:24],
              "".join(_re.split(r"\s+", " ".join(chunks)))
              == "".join(sentence.split()),
              (sentence, chunks))
    return chunks


_check_chunks("one two three four five six seven")       # 7 слов > лимит 4
_check_chunks("a b c d e f g h i j k l m n o p q r s t")  # 20 слов
_check_chunks("part one, part two; part three: four five")  # ест. границы
_short = _check_chunks("short one")
check("короткое предложение — один чанк, как есть", _short == ["short one"])

ot.max_source_tokens = 10
_long_word = _check_chunks("a" * 25 + " " + "b" * 25,
                           tokenizer=CharTokenizer(), preserve="chars")
check("длинное «слово» разбито без потери символов",
      "".join(_re.split(r"\s+", " ".join(_long_word)))
      == "a" * 25 + "b" * 25, _long_word)

# --- off_to_tk / tk_to_off (перевод смещений в Tk-индексы) ----------------
_T = "line1\nline2\nline3"
check("line_offsets", line_offsets(_T) == [0, 6, 12])
check("off_to_tk: начало", off_to_tk(_T, 0) == "1.0")
check("off_to_tk: начало второй строки", off_to_tk(_T, 6) == "2.0")
check("off_to_tk: начало третьей строки", off_to_tk(_T, 12) == "3.0")
check("off_to_tk: конец зажимается", off_to_tk(_T, 999) == "3.5")
check("tk_to_off: обратный перевод", tk_to_off(_T, "3.0") == 12)
check("round-trip off->tk->off", all(
    tk_to_off(_T, off_to_tk(_T, i)) == i for i in range(len(_T))))

# --- StreamUnit: dataclasses.replace не меняет источник --------------------
import dataclasses  # noqa: E402

_su = StreamUnit(src="s", src_start=0, src_end=1, new_paragraph=False)
_su2 = dataclasses.replace(_su, translation="t")
check("StreamUnit.replace", _su.translation == "" and _su2.translation == "t"
      and _su2.src == "s" and _su2.src_start == 0)


# ---------------------------------------------------------------------
# Часть 2. Приложение (реальный GUI + фейковый попредложенический перевод)
# ---------------------------------------------------------------------
# Отдельная временная директория настроек (изоляция от настроек пользователя).
CFG_DIR = tempfile.mkdtemp(prefix="ot_stage4_cfg_")
os.environ["OFFLINE_TRANSLATE_CONFIG"] = CFG_DIR


class FakeTranslator:
    """Заместитель OfflineTranslator с инкрементальным интерфейсом.

    translate_stream(text, direction, on_sentence) повторяет контракт
    реального translator.OfflineTranslator.translate_stream:
      - юниты (уровень A) — split_units;
      - чанки (уровень B) — по chunk_words «токенов» (слов), все чанки
        <= лимита;
      - после каждого юнита on_sentence("done", ...) — один логический
        результат на юнит (сколько чанков ни было);
      - возвращает полный перевод (assemble_output).
    translate() — старый цельный путь (не должен использоваться при
    наличии translate_stream: один общий pipeline).
    """

    def __init__(self):
        self.chunk_calls = []   # (direction, chunk) — все технические чанки
        self.done_events = []   # (direction, src_sentence) — логические юниты
        self.stream_calls = 0
        self.translate_calls = 0
        self.lock = threading.Lock()
        self.chunk_delay = 0.0
        self.chunk_words = 2
        self.fail = False

    def _chunks(self, sentence):
        words = sentence.split()
        if not words:
            return [sentence]
        return [" ".join(words[i:i + self.chunk_words])
                for i in range(0, len(words), self.chunk_words)]

    def translate_stream(self, text, direction="en-ru", on_sentence=None):
        with self.lock:
            self.stream_calls += 1
        units = split_units(text)
        total = len(units)
        out = []
        for i, u in enumerate(units):
            if on_sentence is not None:
                on_sentence("start", i, total,
                            StreamUnit(u.text, u.start, u.end, u.new_paragraph))
            parts = []
            for c in self._chunks(u.text):
                with self.lock:
                    self.chunk_calls.append((direction, c))
                time.sleep(self.chunk_delay)
                if self.fail:
                    self.fail = False
                    raise RuntimeError("fake error")
                parts.append("«%s»" % c)
            t = " ".join(parts)
            out.append(t)
            with self.lock:
                self.done_events.append((direction, u.text))
            if on_sentence is not None:
                on_sentence("done", i + 1, total,
                            StreamUnit(u.text, u.start, u.end,
                                       u.new_paragraph, t))
        return assemble_output(units, out)

    def translate(self, text, direction="en-ru"):
        """Цельный путь (не должен использоваться, если есть stream)."""
        with self.lock:
            self.translate_calls += 1
        return self.translate_stream(text, direction, None)


import main  # noqa: E402
main.OfflineTranslator = FakeTranslator  # реальные модели не загружаются


def pump(seconds, app):
    """Помпа событий Tk на заданное (реальное) время."""
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)


def wait_for(app, cond, timeout=6.0):
    """Ждёт условие, обрабатывая события (worker кладёт в очередь)."""
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        try:
            if cond():
                return True
        except tk.TclError:
            return False
        time.sleep(0.015)
    return False


def hl_ranges(app, field, tag):
    """Диапазоны тега в поле как [(start, end)] символьных смещений."""
    tb = getattr(app, field)._textbox
    text = tb.get("1.0", "end-1c")
    ranges = tb.tag_ranges(tag) or ()
    res = []
    for i in range(0, len(ranges), 2):
        res.append((tk_to_off(text, tb.index(ranges[i])),
                    tk_to_off(text, tb.index(ranges[i + 1]))))
    return res


def out_text(app):
    return app.output_text.get("1.0", "end-1c")


def in_text(app):
    return app.input_text.get("1.0", "end-1c")


app = main.TranslatorApp()
check("приложение запущено, переводчик загружен",
      wait_for(app, lambda: app.translator is not None, 10.0))
ft = app.translator

S3 = "First sentence. Second sentence. Third sentence."
U3 = split_units(S3)
# Ожидаемые переводы фейка (каждое предложение S3 = 2 слова = 1 чанк).
T1 = "«First sentence.»"
T2 = "«Second sentence.»"
T3 = "«Third sentence.»"
FULL3 = f"{T1} {T2} {T3}"


def _reset_fields(text=""):
    app.clear_fields()
    if text:
        app.input_text.insert("1.0", text)
    pump(0.05, app)


# --- 1) 3 предложения -> 3 логических результата, порядок, без перестановки
ft.chunk_delay = 0.05
_reset_fields(S3)
n0_calls, n0_done = len(ft.chunk_calls), len(ft.done_events)
app.start_translation()
check("3 предложения: полный перевод появился",
      wait_for(app, lambda: out_text(app) == FULL3))
check("3 логических результата (по одному done на предложение)",
      len(ft.done_events) - n0_done == 3, ft.done_events[n0_done:])
check("логические результаты в правильном порядке",
      [u[1] for u in ft.done_events[n0_done:]] == [u.text for u in U3])
check("чанки в порядке предложений, без перестановки",
      [c[1] for c in ft.chunk_calls[n0_calls:]] ==
      [u.text for u in U3], ft.chunk_calls[n0_calls:])
check("статус «Готово» после завершения",
      wait_for(app, lambda: "Готов" in app.status_label.cget("text")))

# --- 2) длинный текст переводится ПОСТЕПЕННО, не одним финалом -------------
ft.chunk_delay = 0.15
_reset_fields(S3)
app.start_translation()
samples = []
full_seen = False
deadline = time.time() + 8
while time.time() < deadline:
    samples.append(out_text(app))
    if out_text(app) == FULL3:
        full_seen = True
        break
    pump(0.03, app)
check("постепенный перевод: финал достигнут", full_seen, samples[-3:])
check("постепенный перевод: были промежуточные состояния",
      any(0 < len(s) < len(FULL3) for s in samples), samples)
check("постепенный перевод: вывод только прирастал (нет перестановки)",
      all(b.startswith(a) for a, b in zip(samples, samples[1:])),
      samples)

# --- 3) длинное предложение из нескольких чанков — ОДНО логическое ---------
LONGB = "Alpha beta gamma delta epsilon zeta eta theta."  # 8 слов
TLONG = "«Alpha beta» «gamma delta» «epsilon zeta» «eta theta.»"
ft.chunk_delay = 0.05
_reset_fields(LONGB)
n0_done = len(ft.done_events)
n0_calls = len(ft.chunk_calls)
app.start_translation()
check("длинное предложение: полный перевод",
      wait_for(app, lambda: out_text(app) == TLONG))
check("длинное предложение = ОДИН логический результат",
      len(ft.done_events) - n0_done == 1, ft.done_events[n0_done:])
new_chunks = [c for c in ft.chunk_calls[n0_calls:]]
check("длинное предложение дало несколько технических чанков",
      len(new_chunks) == 4, new_chunks)
check("каждый технический чанк <= лимита (chunk_words)",
      all(len(c[1].split()) <= ft.chunk_words for c in new_chunks), new_chunks)
check("чанки одного предложения собраны в один перевод предложения",
      out_text(app) == TLONG and out_text(app).count("«") == 4)

# --- 4) абзацы: перевод по предложениям, структура абзацев сохраняется ----
PARA = "Para one. Para two.\n\nSecond para. More."
TPARA = "«Para one.» «Para two.»\n\n«Second para.» «More.»"
_reset_fields(PARA)
app.start_translation()
check("абзацы: финальный перевод с сохранением абзаца",
      wait_for(app, lambda: out_text(app) == TPARA), out_text(app))

# --- 5) подсветка: ОДНО предложение = ОДНА единица синхронизации (Этап 5) --
# В любой момент подсвечена ровно одна ЦЕЛАЯ пара «предложение k <->
# перевод k» (последняя завершённая единица). «Крестовая» пара
# (предложение N, перевод N-1) не показывается: пока обрабатывается N-е
# предложение, пара (N-1, N-1) остаётся согласованной; на done(N) пара
# атомарно продвигается на (N, N) ровно один раз (внутренние chunks
# предложения дополнительных переходов не создают).
ft.chunk_delay = 0.3
_reset_fields(S3)
app.start_translation()

def _state_start2():
    return (out_text(app) == T1
            and hl_ranges(app, "input_text", app._hl_src_tag)
            == [(U3[0].start, U3[0].end)])
check("подсветка: во время 2-го предложения пара (1-е, 1-й перевод)",
      wait_for(app, _state_start2, 6.0),
      (out_text(app), hl_ranges(app, "input_text", app._hl_src_tag)))
check("подсветка: src и dst — ОДНО и то же предложение (1 <-> 1)",
      hl_ranges(app, "input_text", app._hl_src_tag)
      == [(U3[0].start, U3[0].end)]
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [(0, len(T1))],
      (hl_ranges(app, "input_text", app._hl_src_tag),
       hl_ranges(app, "output_text", app._hl_dst_tag)))

# done(2) и start(3) worker отдаёт вместе (микросекунды), GUI применяет в
# одном цикле: устойчивое состояние «перевод 2-го готов» — пара продвигается
# как единое целое на (2-е предложение, 2-й перевод).
def _state_after2():
    return (out_text(app) == f"{T1} {T2}"
            and hl_ranges(app, "input_text", app._hl_src_tag)
            == [(U3[1].start, U3[1].end)]
            and hl_ranges(app, "output_text", app._hl_dst_tag)
            == [(len(T1) + 1, len(T1) + 1 + len(T2))])
check("подсветка: после done(2) пара (2-е, 2-й перевод) целиком",
      wait_for(app, _state_after2, 6.0),
      (out_text(app), hl_ranges(app, "input_text", app._hl_src_tag),
       hl_ranges(app, "output_text", app._hl_dst_tag)))
check("статус: идёт перевод (2 из 3 завершено)",
      "2/3" in app.status_label.cget("text"),
      app.status_label.cget("text"))
check("подсветка: финал", wait_for(app, lambda: out_text(app) == FULL3, 6.0))
check("подсветка: в финальном состоянии пара (3-е, 3-й перевод) видна",
      hl_ranges(app, "input_text", app._hl_src_tag)
      == [(U3[2].start, U3[2].end)]
      and hl_ranges(app, "output_text", app._hl_dst_tag)
      == [(len(T1) + 1 + len(T2) + 1, len(T1) + 1 + len(T2) + 1 + len(T3))],
      (hl_ranges(app, "input_text", app._hl_src_tag),
       hl_ranges(app, "output_text", app._hl_dst_tag)))
# Последняя пара показывается короткое время (0.7 c), затем снимается.
pump(0.9, app)
check("подсветка: после завершения в исходном поле снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == [])
check("подсветка: после завершения в поле вывода снята",
      hl_ranges(app, "output_text", app._hl_dst_tag) == [])

# 5b) инвариант на всём проходе: в ЛЮБОЙ момент либо подсветки нет вовсе,
# либо ровно одна ЦЕЛАЯ пара того же юнита k (src-диапазон == k-е
# предложение, dst-диапазон == k-й перевод). «Крестовая» пара
# (предложение N, перевод N-1) не наблюдается никогда.
ft.chunk_delay = 0.25
_reset_fields(S3)
app.start_translation()


def _dst_range(k):
    """Диапазон k-го перевода (k=1..3) в поле вывода (простой абзац)."""
    trans = [T1, T2, T3]
    start = sum(len(t) for t in trans[:k - 1]) + (k - 1)
    return (start, start + len(trans[k - 1]))


observed = []
while out_text(app) != FULL3:
    pump(0.02, app)
    observed.append((hl_ranges(app, "input_text", app._hl_src_tag),
                     hl_ranges(app, "output_text", app._hl_dst_tag)))
check("подсветка: перевод завершён", out_text(app) == FULL3, out_text(app))
pump(0.9, app)  # > grace-периода 0.7 c: последняя пара уже снята
check("подсветка: инвариант — снята после завершения",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])
bad = []
for s_r, d_r in observed:
    if not s_r and not d_r:
        continue  # до первого done — пара ещё не существует
    if len(s_r) == 1 and len(d_r) == 1:
        (ss, se), (ds, de) = s_r[0], d_r[0]
        k = next((i + 1 for i, u in enumerate(U3)
                  if (u.start, u.end) == (ss, se)), None)
        if k is None or d_r[0] != _dst_range(k):
            bad.append((s_r, d_r))
    else:
        bad.append((s_r, d_r))
check("подсветка: в любой момент — пусто или ровно одна пара того же юнита",
      not bad, bad[:3])

# --- 6) новый generation инвалидирует старый результат ----------------------
ft.chunk_delay = 0.25
_reset_fields(S3)
app.start_translation()
check("stale: первое предложение переведено",
      wait_for(app, lambda: T1 in out_text(app), 6.0))
# Пользователь изменил исходный текст (редактирование во время перевода).
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "Brand new text.")
app._on_input_modified()
pump(0.1, app)
n0_calls = len(ft.chunk_calls)
check("stale: новый текст переведён (коалесинг/авто)",
      wait_for(app, lambda: out_text(app) == "«Brand new» «text.»", 8.0),
      out_text(app))
check("stale: старый результат НЕ появился (старый worker не перезаписал)",
      "First sentence" not in out_text(app), out_text(app))
new_calls = [c for c in ft.chunk_calls[n0_calls:]]
check("stale: новый текст переведён ровно одним циклом",
      [c[1] for c in new_calls] == ["Brand new", "text."], new_calls)
check("stale: статус «Готово»",
      wait_for(app, lambda: "Готов" in app.status_label.cget("text")))
pump(0.9, app)  # > grace-периода 0.7 c: подсветка новой пары снята
check("stale: подсветка снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])

# --- 7) очистка инвалидирует старый перевод ---------------------------------
ft.chunk_delay = 0.25
_reset_fields(S3)
app.start_translation()
check("clear: старое предложение появилось в полёте",
      wait_for(app, lambda: T1 in out_text(app), 6.0))
app.clear_fields()
pump(0.1, app)
check("clear: поля пусты, статус «Готово»",
      in_text(app) == "" and out_text(app) == ""
      and "Готов" in app.status_label.cget("text"))
check("clear: подсветка снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])
n1 = len(ft.chunk_calls)
pump(0.4, app)
check("clear: старый worker остановлен (новых чанков нет)",
      len(ft.chunk_calls) == n1, ft.chunk_calls[max(0, n1 - 2):])
check("clear: старый перевод не появился", out_text(app) == "")

# --- 8) пустой текст ничего не запускает ------------------------------------
_reset_fields(S3)
n0_calls = len(ft.chunk_calls)
app.input_text.delete("1.0", "end")
app._on_input_modified()   # пользователь удалил всё
pump(0.4, app)
check("пустой текст (в покое): перевод не запущен",
      len(ft.chunk_calls) == n0_calls)
check("пустой текст: вывод пуст, статус «Готово»",
      out_text(app) == "" and "Готов" in app.status_label.cget("text"))

ft.chunk_delay = 0.25
_reset_fields(S3)
app.start_translation()
check("пустой текст: старое предложение появилось в полёте",
      wait_for(app, lambda: T1 in out_text(app), 6.0))
app.input_text.delete("1.0", "end")
app._on_input_modified()   # пользователь удалил всё во время перевода
pump(1.0, app)
check("пустой текст (в полёте): старый перевод остановлен, вывод пуст",
      out_text(app) == "", out_text(app))
check("пустой текст (в полёте): статус «Готово», подсветка снята",
      "Готов" in app.status_label.cget("text")
      and hl_ranges(app, "input_text", app._hl_src_tag) == [])
n1 = len(ft.chunk_calls)
pump(0.4, app)
check("пустой текст (в полёте): новых чанков нет",
      len(ft.chunk_calls) == n1, ft.chunk_calls[max(0, n1 - 2):])

# --- 9) swap: ровно один новый цикл, старые tag/tasks отменены -------------
# "Swap source one." = 3 слова -> 2 чанка; "Two." = 1 чанк.
SWAP_OUT = "«Swap source» «one.» «Two.»"
ft.chunk_delay = 0.05
_reset_fields("Swap source one. Two.")
app.start_translation()
check("swap: исходный перевод готов",
      wait_for(app, lambda: out_text(app) == SWAP_OUT), out_text(app))
n0_calls = len(ft.chunk_calls)
app.swap_fields()
# Новый ввод = SWAP_OUT (4 слова) -> 2 чанка -> ровно 2 новых вызова.
check("swap: ровно один новый translation cycle",
      wait_for(app, lambda: len(ft.chunk_calls) >= n0_calls + 2, 6.0)
      and len(ft.chunk_calls) == n0_calls + 2, ft.chunk_calls[n0_calls:])
check("swap: направление переставлено, в инпуте старый перевод",
      app.direction == "ru-en" and in_text(app) == SWAP_OUT)
check("swap: новый перевод по новому направлению (ru-en)",
      wait_for(app, lambda: "[ru-en]" in out_text(app)
               or "«" in out_text(app)), out_text(app))
pump(0.9, app)  # > grace-периода 0.7 c: пары (старой и новой) сняты
check("swap: старые tags/highlights удалены",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])
app.change_direction("EN → RU")
pump(0.1, app)

# --- 10) swap ВО ВРЕМЯ перевода: старый стрим не применяется ---------------
ft.chunk_delay = 0.25
_reset_fields(S3)
app.start_translation()
check("swap в полёте: первое предложение появилось",
      wait_for(app, lambda: T1 in out_text(app), 6.0))
n0_calls = len(ft.chunk_calls)
app.swap_fields()
# Поле ввода после swap = частичный перевод (T1) -> один автоперевод (ru-en).
check("swap в полёте: новый цикл по новому направлению запущен",
      wait_for(app, lambda: out_text(app) == "««First sentence.»»", 8.0),
      out_text(app))
after = [c for c in ft.chunk_calls[n0_calls:]]
check("swap в полёте: старый стрим остановлен (en-ru чанков <= 1 в полёте)",
      sum(1 for c in after if c[0] == "en-ru") <= 1, after)
check("swap в полёте: старый полный результат не появился",
      "Second sentence" not in out_text(app), out_text(app))
pump(0.9, app)  # > grace-периода 0.7 c
check("swap в полёте: подсветка снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == [])
app.change_direction("EN → RU")
pump(0.1, app)

# --- 11) ручной и автоматический перевод — ОДИН pipeline -------------------
ft.chunk_delay = 0.05
n0_stream = ft.stream_calls
n0_whole = ft.translate_calls
# Ручной (кнопка «Перевести» / Ctrl+Enter — один запускатель).
_reset_fields(S3)
app.start_translation()
check("manual: полный перевод", wait_for(app, lambda: out_text(app) == FULL3))
# Автоматический (debounce) — тот же механизм.
_reset_fields("Auto sentence. Auto two.")
app._on_input_modified()
check("auto: полный перевод",
      wait_for(app, lambda: out_text(app) == "«Auto sentence.» «Auto two.»"))
check("ручной и авто: использован попредложенический pipeline",
      ft.stream_calls >= n0_stream + 2, (n0_stream, ft.stream_calls))
check("ручной и авто: старый whole-text путь не использовался",
      ft.translate_calls == n0_whole == 0, ft.translate_calls)

# --- 12) resize во время перевода не ломает поля/tags ----------------------
ft.chunk_delay = 0.2
_reset_fields(S3)
app.start_translation()
check("resize: первое предложение в полёте",
      wait_for(app, lambda: T1 in out_text(app), 6.0))
app.geometry("1200x800")
pump(0.3, app)
check("resize: во время перевода ranges читаются без ошибок",
      isinstance(hl_ranges(app, "input_text", app._hl_src_tag), list)
      and isinstance(hl_ranges(app, "output_text", app._hl_dst_tag), list))
app.geometry("900x600")
pump(0.3, app)
check("resize: перевод завершён после изменения размера",
      wait_for(app, lambda: out_text(app) == FULL3, 8.0), out_text(app))
pump(0.9, app)  # > grace-периода 0.7 c
check("resize: подсветка снята после завершения",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])

# --- 13) редактирование/копирование во время перевода не ломают стрим ------
ft.chunk_delay = 0.2
_reset_fields(S3)
app.start_translation()
check("copy: первое предложение в полёте",
      wait_for(app, lambda: T1 in out_text(app), 6.0))
out_tb = app.output_text._textbox
out_tb.event_generate("<Control-a>")   # выделить всё (binding на поле)
pump(0.05, app)
out_tb.event_generate("<Control-c>")   # копирование (binding на поле)
pump(0.05, app)
check("Ctrl+A/Ctrl+C: стрим продолжается, перевод завершён",
      wait_for(app, lambda: out_text(app) == FULL3, 8.0), out_text(app))
pump(0.9, app)  # > grace-периода 0.7 c
check("Ctrl+A/Ctrl+C: подсветка снята, статус «Готово»",
      hl_ranges(app, "output_text", app._hl_dst_tag) == []
      and "Готов" in app.status_label.cget("text"))

# --- 14) короткий текст (одно предложение) — без «1/1» в статусе -----------
_reset_fields("Hello world")
app.start_translation()
check("короткий текст: перевод",
      wait_for(app, lambda: out_text(app) == "«Hello world»"))
check("короткий текст: статус «Готово» (без «1/1»)",
      "Готов" in app.status_label.cget("text")
      and "1/1" not in app.status_label.cget("text"),
      app.status_label.cget("text"))

# --- 15) ошибка перевода: подсветка снята, статус — ошибка -----------------
ft.fail = True
_reset_fields(S3)
app.start_translation()
check("ошибка: показана в статусе",
      wait_for(app, lambda: "Ошибка перевода" in app.status_label.cget("text")))
check("ошибка: подсветка снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])
_reset_fields("Back to work")
app.start_translation()
check("ошибка сбрасывается успешным переводом",
      wait_for(app, lambda: "Готов" in app.status_label.cget("text")))

# --- закрытие ----------------------------------------------------------------
app._on_window_close()
check("приложение закрылось без ошибок", True)

print("OK: %d checks passed" % len(PASS))
