# -*- coding: utf-8 -*-
"""Этап 12: понятная «Схема оформления» (dark/light) + корректное
форматирование перевода (абзацы сохраняются, искусственных переносов
нет, визуальный wrap — задача Text-виджета).

Запуск (реальные модели не скачиваются, сеть не нужна; для GUI-части
нужен дисплей, полный GUI запускается ОДИН раз):

    python tests/test_stage12_ux.py

Структура:
1. Чистая логика (без GUI): persistence темы (dark/light) в
   settings.json; paragraph boundaries в split_units (один/несколько
   абзацев, пустые строки, несколько предложений в абзаце, кейс из
   задания); assemble_output: внутри абзаца — пробел, между абзацами —
   ровно «\\n\\n»; длинный текст внутри абзаца остаётся одной
   логической строкой (переносов по фиксированной длине нет).
2. GUI (одно приложение, FakeTranslator):
   - «Схема оформления»: подпись и пояснение в диалоге; смена темы
     через «Сохранить» меняет UI без перезапуска (окно, поля, теги
     подсветки, appearance mode), выбор сохраняется; подсветка
     остаётся читаемой в обеих темах (фон hl != фон поля);
   - wrap="word": длинная строка переносится ВИЗУАЛЬНО при сужении
     окна, фактический текст и логические \\n не меняются (короткие
     хвосты — нормальное поведение word-wrap, в текст ничего не
     вставляется);
   - стрим: в поле вывода попадают только логические переводы (\\n —
     только разделители абзацев «\\n\\n»), _unit_map совпадает с
     финальным текстом; после resize hover-mapping, подсветка и
     синхронная прокрутка продолжают работать.
"""
import contextlib
import json
import os
import sys
import tempfile
import time
import types

# Корень репозитория — родитель tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:500]))
    PASS.append(name)


# =====================================================================
# Часть 1. Чистая логика (без GUI, без тяжёлых зависимостей)
# =====================================================================
from sentence_pipeline import assemble_output, split_units  # noqa: E402
from settings import Settings, validate_value  # noqa: E402

# --- Тема: валидация и persistence ------------------------------------
check("theme: dark валиден", validate_value("theme", "dark") == (True, "dark"))
check("theme: light валиден", validate_value("theme", "light") == (True, "light"))
check("theme: другое значение отклоняется",
      validate_value("theme", "blue")[0] is False)

CFG_DIR = tempfile.mkdtemp(prefix="ot_stage12_cfg_")
os.environ["OFFLINE_TRANSLATE_CONFIG"] = CFG_DIR
st = Settings()
check("theme: dark сохраняется",
      st.set("theme", "dark") is True and st.get("theme") == "dark")
st.save()
with open(os.path.join(CFG_DIR, "settings.json"), encoding="utf-8") as f:
    check("theme: dark записан в файл", json.load(f).get("theme") == "dark")
st = Settings()
check("theme: dark читается из файла", st.get("theme") == "dark")
check("theme: light сохраняется",
      st.set("theme", "light") is True and st.get("theme") == "light")
st.save()
with open(os.path.join(CFG_DIR, "settings.json"), encoding="utf-8") as f:
    check("theme: light записан в файл", json.load(f).get("theme") == "light")
check("theme: light читается из файла", Settings().get("theme") == "light")
# Дефолт для GUI-части — тёмная тема (как в DEFAULTS).
st = Settings()
st.set("theme", "dark")
st.save()

# --- Paragraph boundaries: split_units ---------------------------------
# Кейс из задания: два предложения в первом абзаце, второй — отдельный.
SPEC = "A sentence. Another sentence.\n\nNew paragraph."
U_SPEC = split_units(SPEC)
check("para: кейс задания — 3 юнита", len(U_SPEC) == 3, U_SPEC)
check("para: кейс задания — текст юнитов точный",
      [u.text for u in U_SPEC]
      == ["A sentence.", "Another sentence.", "New paragraph."],
      [u.text for u in U_SPEC])
check("para: кейс задания — границы (флаг только у первого в абзаце)",
      [u.new_paragraph for u in U_SPEC] == [False, False, True],
      [(u.text, u.new_paragraph) for u in U_SPEC])
check("para: кейс задания — смещения точные",
      [u.start for u in U_SPEC]
      == [0, len("A sentence. "), len("A sentence. Another sentence.\n\n")]
      and [u.end for u in U_SPEC]
      == [len("A sentence."), len("A sentence. Another sentence."), len(SPEC)],
      [(u.start, u.end) for u in U_SPEC])

# Один абзац — один юнит, new_paragraph у первого юнита текста — False.
U1 = split_units("Single.")
check("para: один абзац", len(U1) == 1 and U1[0].text == "Single."
      and U1[0].new_paragraph is False, U1)

# Несколько абзацев — флаг True у ПЕРВОГО юнита каждого нового абзаца.
U2 = split_units("P1a. P1b.\n\nP2.\n\nP3a. P3b.")
check("para: несколько абзацев — флаги",
      len(U2) == 5 and [u.new_paragraph for u in U2]
      == [False, False, True, True, False],
      [(u.text, u.new_paragraph) for u in U2])

# Несколько пустых строк подряд — ОДНА граница абзацев (лишних
# юнитов/абзацев нет).
U3 = split_units("A.\n\n\n\nB.")
check("para: несколько пустых строк — одна граница",
      len(U3) == 2 and [u.new_paragraph for u in U3] == [False, True], U3)

# Несколько предложений в одном абзаце — все new_paragraph False.
U4 = split_units("One. Two! Three?")
check("para: несколько предложений в одном абзаце",
      len(U4) == 3 and all(not u.new_paragraph for u in U4), U4)

# Хвостовые пустые строки не порождают юнитов.
U5 = split_units("End.\n\n")
check("para: хвостовые пустые строки — без лишних юнитов",
      len(U5) == 1 and U5[0].text == "End.", U5)

# --- assemble_output: только логические переводы -----------------------
A_SPEC = assemble_output(U_SPEC, ["t1", "t2", "t3"])
check("assemble: кейс задания — т1 т2, пустая строка, т3",
      A_SPEC == "t1 t2\n\nt3", repr(A_SPEC))


def _only_para_seps(out):
    """В выводе есть ТОЛЬКО разделители абзацев «\\n\\n»: ни одного
    одиночного \\n (перенос в пределах абзаца) и ни одного «\\n\\n\\n»
    (лишняя пустая строка)."""
    return "\n" not in out.replace("\n\n", "") and "\n\n\n" not in out


check("assemble: кейс задания — только разделители абзацев",
      _only_para_seps(A_SPEC), repr(A_SPEC))

# Длинный текст в ОДНОМ абзаце — остаётся одной логической строкой
# (переносов по фиксированной длине нет ни в одном месте пайплайна).
LONG_PARA = " ".join("Word %d." % i for i in range(120))  # 120 предложений
UL = split_units(LONG_PARA)
check("fmt: длинный абзац — все юниты без new_paragraph",
      len(UL) == 120 and all(not u.new_paragraph for u in UL))
AL = assemble_output(UL, ["T%d" % i for i in range(120)])
check("fmt: длинный абзац — ноль физических переносов",
      "\n" not in AL and len(AL.split()) == 120)

# Одно «предложение» без границ .!?… — одна строка на любой длине.
HUGE = "word " * 500
UH = split_units(HUGE)
AH = assemble_output(UH, ["x " * 500])
check("fmt: «предложение» без границ — одна логическая строка",
      len(UH) == 1 and "\n" not in AH)

# Свойство для смешанного случая: переносы — только разделители абзацев.
UM = split_units("M1. M2.\n\nM3. M4. M5.")
AM = assemble_output(UM, ["a", "b", "c", "d", "e"])
check("fmt: смешанный — только разделители абзацев",
      AM == "a b\n\nc d e" and _only_para_seps(AM), repr(AM))

# =====================================================================
# Часть 2. GUI (одно приложение, FakeTranslator)
# =====================================================================
# Стабы torch/transformers — ПЕРЕД импортом main/translator (как в
# других тестах): реальные модели не загружаются.
fake_torch = types.ModuleType("torch")
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake_torch
fake_transformers = types.ModuleType("transformers")
fake_transformers.AutoTokenizer = type("AutoTokenizer", (), {})
fake_transformers.AutoModelForSeq2SeqLM = type("AutoModelForSeq2SeqLM", (), {})
sys.modules["transformers"] = fake_transformers

import tkinter as tk  # noqa: E402

from sentence_pipeline import StreamUnit  # noqa: E402

# Длинная строка перевода (≈300 символов, БЕЗ переносов) — чтобы
# «короткие хвосты» и визуальный wrap воспроизводились детерминированно.
LONG_T = ("слово%d " * 25).strip()


class FakeTranslator:
    """Заместитель OfflineTranslator: детерминированный перевод юнитов,
    инкрементальный интерфейс того же контракта (как в test_stage10_ui)."""

    def __init__(self, model_id=None):
        self.model_id = model_id

    def translate_stream(self, text, direction="en-ru", on_sentence=None):
        units = split_units(text)
        out = []
        for i, u in enumerate(units):
            if on_sentence is not None:
                on_sentence("start", i, len(units),
                            StreamUnit(u.text, u.start, u.end,
                                       u.new_paragraph))
            t = LONG_T
            out.append(t)
            if on_sentence is not None:
                on_sentence("done", i + 1, len(units),
                            StreamUnit(u.text, u.start, u.end,
                                       u.new_paragraph, t))
        return assemble_output(units, out)

    def translate(self, text, direction="en-ru"):
        return self.translate_stream(text, direction, None)


import main  # noqa: E402
main.OfflineTranslator = FakeTranslator  # реальные модели не загружаются


def pump(seconds, app):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)


def wait_for(app, cond, timeout=10.0):
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


def tag_offsets(tb, tag):
    """Диапазоны тега (смещения в символах) — как hl_ranges в stage5/10."""
    text = tb.get("1.0", "end-1c")
    offs = [0]
    for j, ch in enumerate(text):
        if ch == "\n":
            offs.append(j + 1)

    def off(idx):
        ls, cs = str(idx).split(".", 1)
        return offs[int(ls) - 1] + int(cs)

    ranges = tb.tag_ranges(tag) or ()
    return [(off(ranges[i]), off(ranges[i + 1]))
            for i in range(0, len(ranges), 2)]


def yview(tb):
    v = tb.yview()
    return (float(v[0]), float(v[1])) if v else (0.0, 1.0)


def color(value):
    """Нормализация цвета CTk для сравнения (str/tuple -> нижний регистр)."""
    return str(value).strip().lower()


def _labels_of(frame):
    """Все CTkLabel в дереве виджетов (метки настроек живут во внутреннем
    фрейме CTkScrollableFrame)."""
    found = []
    for w in frame.winfo_children():
        if isinstance(w, main.ctk.CTkLabel):
            found.append(w)
        found.extend(_labels_of(w))
    return found


app = main.TranslatorApp()
check("gui: приложение запущено, переводчик загружен",
      wait_for(app, lambda: app.translator is not None))
app.settings.set("autotranslate", False)  # сценарий без автоперевода
app.update_idletasks()
pump(0.2, app)

# =====================================================================
# A. «Схема оформления»: пояснение + смена темы без перезапуска
# =====================================================================
app._open_settings()
check("dlg: диалог открыт",
      wait_for(app, lambda: (app._settings_dialog is not None
                             and app._settings_dialog.winfo_exists())))
dlg = app._settings_dialog
pump(0.3, app)

# Подпись «Схема оформления» присутствует в области настроек.
labels = [w for w in _labels_of(dlg._scroll_frame)
          if w.cget("text") == "Схема оформления"]
check("theme: в диалоге подпись «Схема оформления»", len(labels) == 1)

# Пояснение назначения темы присутствует и содержательно.
note = dlg.theme_note_label.cget("text")
check("theme: пояснение схемы присутствует", bool(note.strip()), repr(note))
check("theme: пояснение объясняет обе схемы и немедленное применение",
      "Тёмная" in note and "Светлая" in note
      and "без перезапуска" in note, note)

# Текущий выбор — из настроек.
check("theme: первичный выбор из настроек",
      dlg.theme_var.get() == main._THEME_LABELS[app.settings.get("theme")],
      dlg.theme_var.get())

# Смена на «Светлую» через «Сохранить» (эмуляция выбора из дропдауна,
# как в stage10: CTk command не зовётся при программном set, а _on_save
# читает переменную).
dlg.theme_var.set("Светлая")
dlg._on_save()
check("theme: диалог закрыт после «Сохранить»",
      wait_for(app, lambda: (app._settings_dialog is None
                             or not app._settings_dialog.winfo_exists())))
check("theme: светлая сохранена в настройки",
      app.settings.get("theme") == "light")
check("theme: CTk appearance mode переключён",
      main.ctk.get_appearance_mode() == "Light",
      main.ctk.get_appearance_mode())
check("theme: палитра приложения — light",
      app._pal == main.PALETTES["light"], sorted(set(app._pal)))
check("theme: главное окно перекрашено",
      color(app.cget("fg_color")) == color(main.PALETTES["light"]["bg"]),
      (app.cget("fg_color"), main.PALETTES["light"]["bg"]))
check("theme: поля перекрашены",
      all(color(box.cget("fg_color")) == color(main.PALETTES["light"]["field"])
          for box in (app.input_text, app.output_text)),
      (app.input_text.cget("fg_color"),
       app.output_text.cget("fg_color")))
# Подсветка: фон тега — из light-палитры и ЧИТАЕМЫЙ (отличается от фона
# поля; при совпадении подсвеченное предложение было бы невидимо).
check("theme: подсветка — из light-палитры и читаемая",
      all(color(tb.tag_cget(tag, "background"))
          == color(main.PALETTES["light"]["hl"])
          for tb, tag in ((app.input_text._textbox, app._hl_src_tag),
                          (app.output_text._textbox, app._hl_dst_tag)))
      and main.PALETTES["light"]["hl"] != main.PALETTES["light"]["field"],
      (app.input_text._textbox.tag_cget(app._hl_src_tag, "background"),
       main.PALETTES["light"]["hl"], main.PALETTES["light"]["field"]))

# Новый диалог сразу получает новую палитру (Settings dialog обновляется).
app._open_settings()
wait_for(app, lambda: (app._settings_dialog is not None
                       and app._settings_dialog.winfo_exists()))
dlg2 = app._settings_dialog
pump(0.2, app)
check("theme: новый диалог — в light-палитре",
      color(dlg2.cget("fg_color")) == color(main.PALETTES["light"]["bg"]),
      dlg2.cget("fg_color"))
check("theme: выбор в диалоге — сохранённый (Светлая)",
      dlg2.theme_var.get() == "Светлая", dlg2.theme_var.get())
check("theme: пояснение есть и в новом диалоге",
      bool(dlg2.theme_note_label.cget("text").strip()))

# Возврат на «Тёмную» тем же путём (цикл переключения работает в обе
# стороны; подсветка в обеих темах читаемая).
dlg2.theme_var.set("Тёмная")
dlg2._on_save()
wait_for(app, lambda: (app._settings_dialog is None
                       or not app._settings_dialog.winfo_exists()))
check("theme: тёмная снова сохранена",
      app.settings.get("theme") == "dark"
      and main.ctk.get_appearance_mode() == "Dark")
check("theme: тёмная — окно и поля в dark-палитре",
      color(app.cget("fg_color")) == color(main.PALETTES["dark"]["bg"])
      and all(color(box.cget("fg_color")) == color(main.PALETTES["dark"]["field"])
              for box in (app.input_text, app.output_text)))
check("theme: тёмная — подсветка читаемая",
      main.PALETTES["dark"]["hl"] != main.PALETTES["dark"]["field"])

# =====================================================================
# B. Визуальный wrap: перенос по ширине — без изменения текста
# =====================================================================
in_tb = app.input_text._textbox
out_tb = app.output_text._textbox
check("wrap: оба поля — word-wrap (перенос по словам, по ширине)",
      in_tb.cget("wrap") == "word" and out_tb.cget("wrap") == "word",
      (in_tb.cget("wrap"), out_tb.cget("wrap")))

# Одна ЛОГИЧЕСКАЯ строка без \n; в узком поле она визуально разбивается
# на несколько строк — «короткий хвост» (последний недолетевший до конца
# строки фрагмент) — нормальное поведение word-wrap; в текст НИЧЕГО не
# вставляется.
LONG_LINE = ("текст " * 250).strip()  # 1250 символов, ноль \n
app.output_text.delete("1.0", "end")
app.output_text.insert("1.0", LONG_LINE)
pump(0.2, app)
text_wide = out_tb.get("1.0", "end-1c")
# «Визуальных» строк считаем через count(-displaylines) (в tkinter 3.12
# результат — кортеж из одного числа; логических строк — по тексту).
disp_wide = out_tb.count("1.0", "end-1c", "displaylines")[0]
check("wrap: фактический текст — одна логическая строка",
      text_wide == LONG_LINE and text_wide.count("\n") == 0,
      (len(text_wide), text_wide.count("\n")))
check("wrap: длинная строка визуально перенесена по ширине",
      disp_wide > 1, disp_wide)

# Сужение окна — перенос пересчитывается ВИДЖЕТОМ, текст не меняется.
app.geometry("700x450")  # минимальный размер: поля заметно сужаются
pump(0.5, app)
text_narrow = out_tb.get("1.0", "end-1c")
disp_narrow = out_tb.count("1.0", "end-1c", "displaylines")[0]
check("wrap: после сужения окна текст НЕ изменился",
      text_narrow == LONG_LINE, (len(text_narrow), len(LONG_LINE)))
check("wrap: после сужения — переносов стало не меньше",
      disp_narrow >= disp_wide and disp_narrow > 1,
      (disp_wide, disp_narrow))

# Возврат размера — reflow обратно (виджет, а не код, управляет переносом).
app.geometry("900x600")
pump(0.5, app)
text_restored = out_tb.get("1.0", "end-1c")
disp_restored = out_tb.count("1.0", "end-1c", "displaylines")[0]
check("wrap: после возврата размера текст снова тот же",
      text_restored == LONG_LINE and disp_restored <= disp_narrow,
      (disp_narrow, disp_restored))

# =====================================================================
# C. Стрим: форматирование вывода + mapping/подсветка/скролл после resize
# =====================================================================
app.clear_fields()
# Длинное «предложение» (одна точка в конце, без границ .!?… внутри) —
# чтобы и поле ввода было ПРОКРУЧИВАЕМО после resize (scroll-sync в
# непрокручиваемом документе yview_moveto нечего выставлять).
_FILL = "formatting details and wrapping behavior are important, " * 8
SRC = ("First long sentence: " + _FILL + " end of first. "
       "Second long sentence: " + _FILL + " end of second.\n\n"
       "Third long sentence: " + _FILL + " end of third.")
U = split_units(SRC)
check("pipe: источник — 3 юнита, 2 абзаца",
      len(U) == 3 and [u.new_paragraph for u in U] == [False, False, True], U)
app.input_text.insert("1.0", SRC)
app._auto_follow = True  # автопоказ снова доступен (как при новом запуске)
app.start_translation()
check("pipe: перевод завершён",
      wait_for(app, lambda: (not app._translation_busy
                             and app.output_text.get("1.0", "end-1c")
                             .strip() != "")))
pump(0.2, app)
out = app.output_text.get("1.0", "end-1c")
EXPECTED = assemble_output(U, [LONG_T, LONG_T, LONG_T])
check("pipe: финальный текст == assemble_output (стрим == финал)",
      out == EXPECTED, out[:120])
check("pipe: в выводе нет переносов кроме разделителей абзацев",
      _only_para_seps(out), repr(out[:200]))
check("pipe: внутри абзацев — ноль физических переносов",
      "\n" not in out.split("\n\n")[0]
      and "\n" not in out.split("\n\n")[-1])
check("pipe: ровно один разделитель абзацев между двумя абзацами",
      out.count("\n\n") == 1 and "\n\n\n" not in out,
      "count=%d" % out.count("\n\n"))

# _unit_map: src — из пайплайна, dst — из фактического финального текста.
check("pipe: в mapping три юнита", len(app._unit_map) == 3, app._unit_map)
check("pipe: dst-смещения совпадают с финальным текстом",
      all(out[s:e] == LONG_T for (_ss, _se, s, e) in app._unit_map), out)
check("pipe: src-смещения — из пайплайна",
      all((ss, se) == (u.start, u.end)
          for u, (ss, se, _s, _e) in zip(U, app._unit_map)),
      app._unit_map)
check("pipe: после translation_done подсвечена последняя пара",
      app._hl_unit == 2, app._hl_unit)

# Отложенный сброс подсветки (grace ~0.7 c) — как в stage10.
pump(1.0, app)
check("pipe: после завершения подсветка снята",
      app._hl_unit is None
      and tag_offsets(in_tb, app._hl_src_tag) == []
      and tag_offsets(out_tb, app._hl_dst_tag) == [])

# Resize: визуальный перенос изменился, ЛОГИЧЕСКИЙ текст нет — mapping
# (смещения, а не визуальные строки) остаётся корректным.
app.geometry("700x450")
pump(0.5, app)
check("pipe: после resize финальный текст не изменился",
      app.output_text.get("1.0", "end-1c") == out)
mid0 = U[0].start + len(U[0].text) // 2
mid2 = U[2].start + len(U[2].text) // 2
check("pipe: после resize hover-mapping (src) работает",
      app._find_unit_at("src", mid0) == 0
      and app._find_unit_at("src", mid2) == 2,
      (app._find_unit_at("src", mid0), app._find_unit_at("src", mid2)))
dst_mid1 = app._unit_map[1][2] + (app._unit_map[1][3]
                                  - app._unit_map[1][2]) // 2
check("pipe: после resize hover-mapping (dst) работает",
      app._find_unit_at("dst", dst_mid1) == 1,
      app._find_unit_at("dst", dst_mid1))

# Синхронная прокрутка после resize: вид одного поля -> вид другого.
in_tb.yview_moveto(0.3)
pump(0.3, app)
check("scroll: после resize синхронная прокрутка работает",
      abs(yview(out_tb)[0] - 0.3) < 0.05, (yview(in_tb), yview(out_tb)))
out_tb.yview_moveto(0.0)
pump(0.3, app)
check("scroll: после resize обратно (dst -> src)",
      yview(in_tb)[0] < 0.03, (yview(in_tb), yview(out_tb)))

# Подсветка после resize: пары ставятся по смещениям _unit_map
# (визуальные строки не используются) — в обеих темах читаемая.
app._highlight_unit(1)
check("pipe: после resize подсветка пары ставится в обоих полях",
      tag_offsets(in_tb, app._hl_src_tag)
      == [(app._unit_map[1][0], app._unit_map[1][1])]
      and tag_offsets(out_tb, app._hl_dst_tag)
      == [(app._unit_map[1][2], app._unit_map[1][3])],
      (tag_offsets(in_tb, app._hl_src_tag),
       tag_offsets(out_tb, app._hl_dst_tag)))
check("pipe: подсветка после resize — в текущей палитре, читаемая",
      color(in_tb.tag_cget(app._hl_src_tag, "background"))
      == color(app._pal["hl"])
      and app._pal["hl"] != app._pal["field"])
app._clear_pair()

app.destroy()

print("Stage 12 OK: %d checks passed" % len(PASS))
