# -*- coding: utf-8 -*-
"""Regression-тесты Этапа 5: UX — настройки, синхронная подсветка,
синхронная прокрутка и их работа вместе.

Запуск (реальные модели не скачиваются, сеть и pytest не нужны):

    python tests/test_stage5_gui.py

Два уровня (реальный дисплей, torch/transformers застаблены, переводчик —
фейк с инкрементальным translate_stream; очередь/потоки/layout — реальные):

A. Настройки: все поля реально существуют и имеют ненулевой размер;
   layout при маленьком/стандартном/большом окне и при изменении
   ширины/высоты; контролы растягиваются по ширине; кнопки внизу;
   вертикальный scrollbar прокручивает только содержимое; значения
   сохраняются (файл + повторное открытие); невалидное значение
   отклоняется с ошибкой; смена темы применяется.
B. Длинные предложения из нескольких token-чанков: UI получает одно
   логическое предложение, подсветка одна, внутренние чанки не создают
   дополнительных highlight-переходов.
C. Синхронная прокрутка: left -> right и right -> left (середина, верх,
   низ), mouse wheel, отсутствие рекурсии/осцилляций, флаги
   _auto_follow / _syncing_scroll.
D. Подсветка + прокрутка вместе: автопозиционирование текущей пары,
   пользовательский wheel отбирает управление (auto-follow off),
   следующий запуск перевода его возвращает.
"""
import contextlib
import json
import os
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

# Отдельная временная директория настроек (изоляция от настроек пользователя).
CFG_DIR = tempfile.mkdtemp(prefix="ot_stage5_cfg_")
os.environ["OFFLINE_TRANSLATE_CONFIG"] = CFG_DIR

import tkinter as tk  # noqa: E402

from sentence_pipeline import split_units  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:500]))
    PASS.append(name)


class FakeTranslator:
    """Заместитель OfflineTranslator с инкрементальным интерфейсом
    (тот же контракт, что у реального translate_stream): юниты —
    split_units, чанки — по chunk_words слов, done-событие — одно на
    логическое предложение (сколько чанков ни было)."""

    def __init__(self):
        self.chunk_calls = []
        self.done_events = []
        self.lock = threading.Lock()
        self.chunk_delay = 0.0
        self.chunk_words = 2

    def _chunks(self, sentence):
        words = sentence.split()
        if not words:
            return [sentence]
        return [" ".join(words[i:i + self.chunk_words])
                for i in range(0, len(words), self.chunk_words)]

    def translate_stream(self, text, direction="en-ru", on_sentence=None):
        from sentence_pipeline import StreamUnit, assemble_output
        units = split_units(text)
        total = len(units)
        out = []
        for i, u in enumerate(units):
            if on_sentence is not None:
                on_sentence("start", i, total,
                            StreamUnit(u.text, u.start, u.end,
                                       u.new_paragraph))
            parts = []
            for c in self._chunks(u.text):
                with self.lock:
                    self.chunk_calls.append((direction, c))
                time.sleep(self.chunk_delay)
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
        return self.translate_stream(text, direction, None)


import main  # noqa: E402
main.OfflineTranslator = FakeTranslator  # реальные модели не загружаются


def pump(seconds, app):
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)


def wait_for(app, cond, timeout=8.0):
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
    tb = getattr(app, field)._textbox
    text = tb.get("1.0", "end-1c")
    offs = [0]
    for j, ch in enumerate(text):
        if ch == "\n":
            offs.append(j + 1)

    def off(idx):
        ls, cs = idx.split(".", 1)
        return offs[int(ls) - 1] + int(cs)

    ranges = tb.tag_ranges(tag) or ()
    return [(off(tb.index(ranges[i])), off(tb.index(ranges[i + 1])))
            for i in range(0, len(ranges), 2)]


def out_text(app):
    return app.output_text.get("1.0", "end-1c")


def in_text(app):
    return app.input_text.get("1.0", "end-1c")


def reset_fields(app, text=""):
    app.clear_fields()
    if text:
        app.input_text.insert("1.0", text)
    pump(0.05, app)


app = main.TranslatorApp()
check("приложение запущено, переводчик загружен",
      wait_for(app, lambda: app.translator is not None, 10.0))
ft = app.translator
# =====================================================================
# A. Настройки: layout, размеры, прокрутка, сохранение
# =====================================================================
app._open_settings()
check("настройки: диалог открыт",
      wait_for(app, lambda: (app._settings_dialog is not None
                             and app._settings_dialog.winfo_exists()), 5.0))
dlg = app._settings_dialog
pump(0.3, app)


def widgets_of(kind):
    return [w for w in dlg._scroll_frame.winfo_children()
            if type(w).__name__ == kind]


entries = widgets_of("CTkEntry")
options = widgets_of("CTkOptionMenu")
switches = widgets_of("CTkSwitch")
labels = widgets_of("CTkLabel")
check("настройки: поля debounce/timeout/maxlength существуют",
      len(entries) == 3, [type(w).__name__
                          for w in dlg._scroll_frame.winfo_children()])
check("настройки: поле глобального хоткея существует",
      dlg.hotkey_entry.winfo_exists() and dlg.hotkey_entry.winfo_width() >= 100,
      dlg.hotkey_entry.winfo_width())
check("настройки: меню существуют (тема + 2 языка)", len(options) == 3, options)
check("настройки: переключатели существуют (автоперевод + направление)",
      len(switches) == 2, switches)
check("настройки: подписи существуют (>= 9 строк)", len(labels) >= 9, labels)
check("настройки: кнопки Отмена/Сохранить существуют",
      dlg.cancel_btn.winfo_exists() and dlg.save_btn.winfo_exists())


def mapped_size(w, min_w=20, min_h=15):
    return (w.winfo_ismapped() and w.winfo_width() >= min_w
            and w.winfo_height() >= min_h)


check("настройки: все контролы отображаются с ненулевым размером",
      all(mapped_size(w) for w in entries + options + switches + labels)
      and mapped_size(dlg.save_btn) and mapped_size(dlg.cancel_btn),
      [(type(w).__name__, w.winfo_width(), w.winfo_height())
       for w in entries + options + switches])
check("настройки: у полей ввода нормальная минимальная ширина (>= 140)",
      all(w.winfo_width() >= 140 for w in entries),
      [w.winfo_width() for w in entries])
check("настройки: у меню нормальная минимальная ширина (>= 170)",
      all(w.winfo_width() >= 170 for w in options),
      [w.winfo_width() for w in options])

# Кнопки — в фиксированной нижней панели (внизу окна).
dlg.update_idletasks()
dlg_bottom = dlg.winfo_rooty() + dlg.winfo_height()
btn_bottom = dlg.save_btn.winfo_rooty() + dlg.save_btn.winfo_height()
check("настройки: кнопки находятся внизу окна",
      dlg_bottom - btn_bottom < 45, (dlg_bottom, btn_bottom))
check("настройки: label и control выровнены (control правее подписи)",
      dlg.save_btn.winfo_rootx() > labels[0].winfo_rootx())
# Растяжение по ширине: большое окно -> контролы шире.
std_entry_w = entries[0].winfo_width()
std_option_w = options[0].winfo_width()
dlg.geometry("900x700")
pump(0.4, app)
check("настройки: при увеличении ширины поля ввода растягиваются",
      entries[0].winfo_width() > std_entry_w + 50,
      (std_entry_w, entries[0].winfo_width()))
check("настройки: при увеличении ширины меню растягиваются",
      options[0].winfo_width() > std_option_w + 50,
      (std_option_w, options[0].winfo_width()))

# Только изменение ширины (высота та же).
dlg.geometry("700x700")
pump(0.3, app)
check("настройки: изменение ширины — контролы не исчезают",
      all(mapped_size(w) for w in entries + options + switches))

# Маленькое окно (minsize): ничего не исчезает, есть вертикальный scrollbar.
dlg.geometry("500x480")
pump(0.4, app)
check("настройки: маленькое окно — все контролы на месте",
      all(mapped_size(w) for w in entries + options + switches + labels))
check("настройки: маленькое окно — у полей ввода минимум 135 px",
      all(w.winfo_width() >= 135 for w in entries),
      [w.winfo_width() for w in entries])
canvas = dlg._scroll_frame._parent_canvas
sb = dlg._scroll_frame._scrollbar
check("настройки: вертикальный scrollbar отображается", sb.winfo_ismapped())
before_scroll = float(canvas.yview()[0])
canvas.yview_moveto(0.99)
pump(0.2, app)
check("настройки: scrollbar прокручивает содержимое (книзу)",
      float(canvas.yview()[0]) > before_scroll + 0.01, canvas.yview())
check("настройки: нижняя панель НЕ в области прокрутки (кнопки на месте)",
      mapped_size(dlg.save_btn) and mapped_size(dlg.cancel_btn))
canvas.yview_moveto(0.0)
pump(0.2, app)
check("настройки: прокрутка обратно вверх", canvas.yview()[0] <= 0.01)

# Изменение только высоты.
dlg.geometry("500x640")
pump(0.3, app)
check("настройки: изменение высоты — layout не сломан",
      all(mapped_size(w) for w in entries + options + switches)
      and mapped_size(dlg.save_btn))
dlg.geometry("560x640")
pump(0.3, app)

# Сохранение: debounce 2.5 -> файл + повторное открытие.
dlg.debounce_entry.delete(0, "end")
dlg.debounce_entry.insert(0, "2.5")
dlg.save_btn.invoke()
check("настройки: сохранение закрыло диалог",
      wait_for(app, lambda: not dlg.winfo_exists(), 3.0))
check("настройки: значение debounce сохранено в памяти",
      app.settings.get("debounce_sec") == 2.5,
      app.settings.get("debounce_sec"))
with open(os.path.join(CFG_DIR, "settings.json"), encoding="utf-8") as f:
    saved = json.load(f)
check("настройки: значение debounce записано в файл",
      saved["debounce_sec"] == 2.5)
app._open_settings()
wait_for(app, lambda: (app._settings_dialog is not None
                       and app._settings_dialog.winfo_exists()), 5.0)
pump(0.3, app)
check("настройки: повторное открытие — значение подставлено",
      app._settings_dialog.debounce_entry.get() == "2.5",
      app._settings_dialog.debounce_entry.get())

# Невалидное значение: диалог остаётся открытым, ошибка видна.
bad_dlg = app._settings_dialog
bad_dlg.debounce_entry.delete(0, "end")
bad_dlg.debounce_entry.insert(0, "abc")
bad_dlg.save_btn.invoke()
pump(0.3, app)
check("настройки: невалидное значение отклонено (диалог открыт, ошибка видна)",
      bad_dlg.winfo_exists() and bad_dlg.error_label.cget("text") != "",
      bad_dlg.error_label.cget("text"))
check("настройки: сохранение не применилось",
      app.settings.get("debounce_sec") == 2.5)
bad_dlg.cancel_btn.invoke()
wait_for(app, lambda: not bad_dlg.winfo_exists(), 3.0)

# Смена темы применяется без перезапуска.
app._open_settings()
wait_for(app, lambda: (app._settings_dialog is not None
                       and app._settings_dialog.winfo_exists()), 5.0)
pump(0.2, app)
app._settings_dialog.theme_var.set("Светлая")
app._settings_dialog.save_btn.invoke()
wait_for(app, lambda: app._settings_dialog is None
         or not app._settings_dialog.winfo_exists(), 3.0)
check("настройки: смена темы сохранена и применена",
      app.settings.get("theme") == "light"
      and app._pal["bg"] == main.PALETTES["light"]["bg"], app._pal["bg"])
# =====================================================================
# B. Одно длинное предложение из N чанков = ОДИН переход подсветки
# =====================================================================
LONGB = "Alpha beta gamma delta epsilon zeta eta theta."  # 8 слов -> 4 чанка
TLONG = "«Alpha beta» «gamma delta» «epsilon zeta» «eta theta.»"
ft.chunk_delay = 0.08
U_L = split_units(LONGB)
check("B: длинный текст — одно логическое предложение", len(U_L) == 1, U_L)
reset_fields(app, LONGB)
n0_done = len(ft.done_events)
n0_calls = len(ft.chunk_calls)
app.start_translation()
states = set()
t0 = time.time()
while out_text(app) != TLONG and time.time() - t0 < 10:
    pump(0.02, app)
    s_r = hl_ranges(app, "input_text", app._hl_src_tag)
    d_r = hl_ranges(app, "output_text", app._hl_dst_tag)
    if s_r or d_r:
        states.add((tuple(map(tuple, s_r)), tuple(map(tuple, d_r))))
check("B: полный перевод готов", out_text(app) == TLONG, out_text(app))
pump(0.15, app)
# Единственная (последняя) пара удерживается после завершения
# (grace ~0.7 c) — т.е. реально отрисовывается, а не гаснет «впритык».
s_r = hl_ranges(app, "input_text", app._hl_src_tag)
d_r = hl_ranges(app, "output_text", app._hl_dst_tag)
if s_r or d_r:
    states.add((tuple(map(tuple, s_r)), tuple(map(tuple, d_r))))
check("B: один логический done-результат",
      len(ft.done_events) - n0_done == 1, ft.done_events[n0_done:])
check("B: внутри было несколько технических чанков",
      len(ft.chunk_calls) - n0_calls == 4, ft.chunk_calls[n0_calls:])
check("B: чанки не создавали дополнительные highlight-переходы (состояний 1)",
      len(states) == 1, states)
the_state = next(iter(states))
check("B: подсветка — ОДНА пара: предложение целиком + перевод целиком",
      the_state[0] == ((U_L[0].start, U_L[0].end),)
      and the_state[1] == ((0, len(TLONG)),), the_state)
pump(0.9, app)  # > grace-периода 0.7 c
check("B: после завершения подсветка снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])

# =====================================================================
# C. Синхронная прокрутка
# =====================================================================
in_tb = app.input_text._textbox
out_tb = app.output_text._textbox
app.clear_fields()
# Больше строк, чем помещается в окно, — обе «середине» реально
# прокручиваются (see() не двигает вид, если строка уже видна).
for i in range(1, 121):
    in_tb.insert("end", "left line %d\n" % i)
for i in range(1, 201):
    out_tb.insert("end", "right line %d\n" % i)
pump(0.3, app)


def yv(tb):
    return tuple(float(x) for x in tb.yview())


def at_bottom(tb):
    f, l = yv(tb)
    return f + l >= 0.999


def at_top(tb):
    return yv(tb)[0] <= 0.001


check("C: оба поля прокручиваются",
      yv(in_tb)[1] < 0.99 and yv(out_tb)[1] < 0.99,
      (yv(in_tb), yv(out_tb)))


in_tb.see("1.0")
out_tb.see("1.0")
pump(0.2, app)
check("C: исходно оба поля вверху", at_top(in_tb) and at_top(out_tb),
      (tuple(yv(in_tb)), tuple(yv(out_tb))))

# left -> right, середина
in_tb.see("40.0")
pump(0.2, app)
li, ro = yv(in_tb)[0], yv(out_tb)[0]
check("C: left -> right (середина, относительная позиция)",
      li > 0.1 and abs(li - ro) <= 0.05, (li, ro))

# right -> left, низ
out_tb.see("end-1c")
pump(0.2, app)
check("C: right (низ) -> left (низ)", at_bottom(in_tb), tuple(yv(in_tb)))

# right -> left, верх
out_tb.see("1.0")
pump(0.2, app)
check("C: right (верх) -> left (верх)", at_top(in_tb), tuple(yv(in_tb)))

# left -> right, другая середина (не у края: в край срабатывает
# правило «низ -> низ», и доли у полей с разной высотой допустимо
# различаются)
in_tb.see("60.0")
pump(0.2, app)
li, ro = yv(in_tb)[0], yv(out_tb)[0]
check("C: left -> right (другая середина)", abs(li - ro) <= 0.05, (li, ro))

# mouse wheel: левое поле прокручивается, правое следует
before = yv(in_tb)[0]
in_tb.event_generate("<Button-4>")
pump(0.2, app)
li, ro = yv(in_tb)[0], yv(out_tb)[0]
check("C: mouse wheel в левом поле", li != before, (before, li))
check("C: правое поле синхронизировано за wheel", abs(li - ro) <= 0.05,
      (li, ro))
check("C: ручной wheel отключил автопоказ пары", app._auto_follow is False)

# Отсутствие рекурсии/осцилляций: позиции стабильны, флаг защиты сброшен.
snap_in, snap_out = yv(in_tb)[0], yv(out_tb)[0]
pump(0.6, app)
check("C: без бесконечного callback loop (позиции стабильны)",
      abs(yv(in_tb)[0] - snap_in) < 0.001
      and abs(yv(out_tb)[0] - snap_out) < 0.001,
      ((snap_in, snap_out), (yv(in_tb)[0], yv(out_tb)[0])))
check("C: флаг _syncing_scroll сброшен", app._syncing_scroll is False)
# =====================================================================
# D. Подсветка + прокрутка вместе
# =====================================================================
# 30 предложений, каждое — отдельный абзац (одна логическая строка):
# see() двигает вид по ЛОГИЧЕСКИМ строкам, поэтому и исходное, и выводное
# поля реально прокручиваются (одно абзацное предложение — одна строка,
# её «видна» всегда).
LONGD = "\n\n".join(
    "Sentence number %d %s." % (i, "word" * 20) for i in range(1, 31))
UD = split_units(LONGD)
check("D: 30 логических предложений (по одному в абзаце)", len(UD) == 30,
      len(UD))


def _ft_trans(sentence):
    """Ожидаемый перевод предложения (логика FakeTranslator)."""
    words = sentence.split()
    return " ".join("«%s»" % " ".join(words[i:i + 2])
                    for i in range(0, len(words), 2))


FULLD = "\n\n".join(_ft_trans(u.text) for u in UD)
ft.chunk_delay = 0.02
reset_fields(app, LONGD)

# Запуск 1: автопозиционирование — текущая пара видна, поля подведены вниз.
app.start_translation()
check("D: перевод 30 предложений завершён (1-й запуск)",
      wait_for(app, lambda: out_text(app) == FULLD, 30.0),
      out_text(app)[:120])
li, lo = yv(in_tb)[0], yv(out_tb)[0]
check("D: автопоказ: оба поля подведены к текущей паре (книзу)",
      li > 0.5 and lo > 0.5, (li, lo))
check("D: поля остались примерно синхронизированы", abs(li - lo) <= 0.35,
      (li, lo))
pump(0.9, app)  # > grace-периода 0.7 c
check("D: после завершения подсветка снята",
      hl_ranges(app, "input_text", app._hl_src_tag) == []
      and hl_ranges(app, "output_text", app._hl_dst_tag) == [])

# Запуск 2: пользователь «забирает» управление — автопоказ не возвращает.
app.start_translation()
check("D: новый запуск снова включил автопоказ", app._auto_follow is True)
check("D: первое предложение переведено",
      wait_for(app, lambda: "«" in out_text(app), 10.0))
in_tb.event_generate("<Button-4>")  # ручной wheel
pump(0.1, app)
check("D: ручной wheel отключил автопоказ", app._auto_follow is False)
in_tb.see("1.0")
out_tb.see("1.0")
pump(0.2, app)
check("D: перевод 30 предложений завершён (2-й запуск)",
      wait_for(app, lambda: out_text(app) == FULLD, 30.0),
      out_text(app)[:120])
pump(0.2, app)
li, lo = yv(in_tb)[0], yv(out_tb)[0]
check("D: пользователь не «подхвачен» автопрокруткой (поля вверху)",
      li < 0.3 and lo < 0.3, (li, lo))

# Запуск 3: явный запуск снова включает автопозиционирование.
app.start_translation()
check("D: третий запуск включил автопоказ", app._auto_follow is True)
check("D: третий запуск завершён",
      wait_for(app, lambda: out_text(app) == FULLD, 30.0))

# ---------------------------------------------------------------------
app._on_window_close()
check("приложение закрылось без ошибок", True)

print("OK: %d checks passed" % len(PASS))