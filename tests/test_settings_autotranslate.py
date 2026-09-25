# -*- coding: utf-8 -*-
"""Regression-тесты Этапа 3: настройки, горячие клавиши, автоперевод.

Запуск (реальные модели не скачиваются, сеть и pytest не нужны):

    python tests/test_settings_autotranslate.py

Проверяют:
- настройки (settings.py): дефолты, save/load, валидация значений,
  нормализация хоткеев, битый/неизвестный/противоречивый файл;
- приложение (реальный дисплей, torch/transformers застаблены,
  переводчик подменён на фейк — код очереди/потоков/debounce реальный):
  запуск; кнопка «Настройки» и Ctrl+, открывают диалог; Escape закрывает;
  сохранение настроек применяется без перезапуска и переживает «перезапуск»;
  невалидное сохранение отклоняется; смена направления (подписи, без
  «лишнего» перевода); ручной перевод; Ctrl+Enter; автоперевод только
  после debounce; быстрый набор -> ровно один перевод; «Очистить» отменяет
  ожидающий автоперевод; swap -> ровно один перевод без рекурсии;
  устаревший результат не перетирает новый; двух одновременных переводов
  одного текста нет; ошибка перевода видна в статусе; окно растягивается;
  отсутствие pynput-backend не роняет приложение (в-процессе и в subprocess).
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types

# Корень репозитория — родитель tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Стабы torch/transformers — ПЕРЕД импортом translator (как в test_dictionary.py).
# Настоящий OfflineTranslator не конструируется: main.OfflineTranslator
# подменяется на FakeTranslator, так что модели не загружаются.
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
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:400]))
    PASS.append(name)


# ---------------------------------------------------------------------
# Часть 1. Настройки (без GUI)
# ---------------------------------------------------------------------
# Отдельная временная директория настроек через OFFLINE_TRANSLATE_CONFIG.
CFG_DIR = tempfile.mkdtemp(prefix="ot_stage3_cfg_")
os.environ["OFFLINE_TRANSLATE_CONFIG"] = CFG_DIR
CFG_PATH = os.path.join(CFG_DIR, "settings.json")

from settings import Settings, normalize_hotkey, validate_value  # noqa: E402

# --- дефолты ---------------------------------------------------------------
s = Settings()
check("дефолты в памяти",
      s.get("hotkey") == "ctrl+alt+t" and s.get("source_lang") == "en"
      and s.get("target_lang") == "ru" and s.get("theme") == "dark"
      and s.get("autotranslate") is True and s.get("slow_after_sec") == 3
      and s.get("debounce_sec") == 1.5 and s.get("max_text_length") == 5000
      and s.get("filter_cyrillic") is True)
check("settings.json создан при первом запуске", os.path.exists(CFG_PATH))

# --- save/load round-trip ----------------------------------------------------
s.set("hotkey", "ctrl+shift+f12")
s.set("source_lang", "ru")
s.set("target_lang", "en")
s.set("theme", "light")
s.set("autotranslate", False)
s.set("slow_after_sec", 0)
s.set("debounce_sec", 0.2)
s.set("max_text_length", 100)
s.set("filter_cyrillic", False)
s.save()
d = Settings().as_dict()
check("save/load round-trip",
      d["hotkey"] == "ctrl+shift+f12" and d["source_lang"] == "ru"
      and d["target_lang"] == "en" and d["theme"] == "light"
      and d["autotranslate"] is False and d["slow_after_sec"] == 0.0
      and d["debounce_sec"] == 0.2 and d["max_text_length"] == 100
      and d["filter_cyrillic"] is False)

# --- валидация ----------------------------------------------------------------
check("невалидный хоткей отклонён",
      s.set("hotkey", "not-a-hotkey") is False and s.get("hotkey") == "ctrl+shift+f12")
check("невалидный язык отклонён", s.set("source_lang", "fr") is False)
check("невалидная тема отклонена", s.set("theme", "neon") is False)
check("debounce < 0.1 отклонён", s.set("debounce_sec", 0.01) is False)
check("debounce > 60 отклонён", s.set("debounce_sec", 999) is False)
check("debounce: bool отклонён", s.set("debounce_sec", True) is False)
check("maxlen < 0 отклонён", s.set("max_text_length", -5) is False)
check("maxlen: числовая строка конвертирована",
      s.set("max_text_length", "250") is True and s.get("max_text_length") == 250)
check("состояние не изменилось после отклонений",
      s.get("debounce_sec") == 0.2 and s.get("max_text_length") == 250
      and s.get("source_lang") == "ru" and s.get("theme") == "light")


# --- нормализация хоткеев --------------------------------------------------------
check("hotkey: дружелюбный -> pynput", normalize_hotkey("Ctrl+Alt+T") == "<ctrl>+<alt>+t")
check("hotkey: простая клавиша", normalize_hotkey("f12") == "f12")
check("hotkey: только модификаторы — невалидно", normalize_hotkey("ctrl+alt") is None)
check("hotkey: пустой — невалидно", normalize_hotkey("") is None)
check("hotkey: дублирующий мод — невалидно", normalize_hotkey("ctrl+ctrl+t") is None)
check("hotkey: мусор — невалидно", normalize_hotkey("ctrl+qwe") is None)
check("validate_value: hotkey хранится в дружелюбной форме",
      validate_value("hotkey", "Ctrl+Alt+T") == (True, "ctrl+alt+t"))

# --- битый файл -> дефолты ---------------------------------------------------------
with open(CFG_PATH, "w", encoding="utf-8") as f:
    f.write("{broken json")
s_broken = Settings()
check("битый JSON -> дефолты",
      s_broken.get("theme") == "dark" and s_broken.get("debounce_sec") == 1.5)

# --- неизвестные ключи и невалидные значения в файле --------------------------------
with open(CFG_PATH, "w", encoding="utf-8") as f:
    json.dump({"theme": "dark", "unknown_key": 42,
               "debounce_sec": "junk", "source_lang": "fr", "target_lang": "ru"}, f)
s_mixed = Settings()
check("неизвестные ключи игнорируются, невалидные -> дефолты",
      s_mixed.get("theme") == "dark" and s_mixed.get("debounce_sec") == 1.5
      and s_mixed.get("source_lang") == "en"
      and "unknown_key" not in s_mixed.as_dict())

# --- source == target в файле -> дефолты ----------------------------------------------
with open(CFG_PATH, "w", encoding="utf-8") as f:
    json.dump({"source_lang": "en", "target_lang": "en"}, f)
s_same = Settings()
check("source==target в файле -> дефолты",
      s_same.get("source_lang") == "en" and s_same.get("target_lang") == "ru")

# ---------------------------------------------------------------------
# Часть 2. Приложение (реальный дисплей + фейковый переводчик)
# ---------------------------------------------------------------------
class FakeTranslator:
    """Заместитель OfflineTranslator: синхронный translate(), фиксирует
    (текст, направление) каждого вызова. Поток/очередь/debounce — реальные."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.delay = 0.0
        self.fail = False

    def translate(self, text, direction="en-ru"):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            self.calls.append((text, direction))
        if self.fail:
            self.fail = False
            raise RuntimeError("fake error")
        return "[%s] %s" % (direction, text)


import main  # noqa: E402
main.OfflineTranslator = FakeTranslator  # реальные модели не загружаются


def pump(seconds, app):
    """Помпа событий Tk на заданное (реальное) время."""
    end = time.time() + seconds
    while time.time() < end:
        app.update()
        time.sleep(0.005)


def wait_for(app, cond, timeout=6.0):
    """Ждёт условие, обрабатывая события (потоки кладут результат в очередь)."""
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        try:
            if cond():
                return True
        except tk.TclError:
            return False
        time.sleep(0.02)
    return False


app = main.TranslatorApp()
check("приложение запущено, переводчик загружен",
      wait_for(app, lambda: app.translator is not None, 10.0))
check("нет ошибок инициализации",
      "Ошибка" not in app.status_label.cget("text"))
ft = app.translator


# --- кнопка «Настройки» открывает диалог, Escape закрывает --------------------------
app.settings_btn.invoke()
check("кнопка «Настройки» открывает диалог",
      wait_for(app, lambda: app._settings_dialog is not None
               and app._settings_dialog.winfo_exists()))
dlg = app._settings_dialog
dlg.event_generate("<Escape>")
check("Escape закрывает диалог",
      wait_for(app, lambda: not dlg.winfo_exists()))
check("ссылка на закрытый диалог сброшена", app._settings_dialog is None)

# --- сохранение: применяется без перезапуска + сохраняется в файл -------------------
app._open_settings()
wait_for(app, lambda: app._settings_dialog is not None
         and app._settings_dialog.winfo_exists())
dlg = app._settings_dialog
dlg.theme_var.set("Светлая")


def _set_entry(entry, value):
    entry.delete(0, "end")
    entry.insert(0, value)


_set_entry(dlg.debounce_entry, "0.2")
_set_entry(dlg.slow_entry, "0")
_set_entry(dlg.max_len_entry, "100")
dlg._on_save()
check("диалог закрыт после «Сохранить»",
      wait_for(app, lambda: app._settings_dialog is None
               or not app._settings_dialog.winfo_exists()))
check("тема применена без перезапуска",
      app.settings.get("theme") == "light"
      and main.ctk.get_appearance_mode() == "Light")
check("параметры автоперевода применены",
      app.settings.get("debounce_sec") == 0.2
      and app.settings.get("slow_after_sec") == 0.0
      and app.settings.get("max_text_length") == 100)
check("настройки переживают «перезапуск»",
      Settings().get("theme") == "light"
      and Settings().get("debounce_sec") == 0.2
      and Settings().get("max_text_length") == 100)
# тёмную тему возвращаем для остального теста
app._set_theme("dark")
app.settings.set("theme", "dark")
app.settings.save()
check("тема возвращена", app.settings.get("theme") == "dark")

# --- невалидное сохранение: source == target ----------------------------------------
app._open_settings()
wait_for(app, lambda: app._settings_dialog is not None
         and app._settings_dialog.winfo_exists())
dlg = app._settings_dialog
src_lang = app.settings.get("source_lang")
tgt_lang = app.settings.get("target_lang")
dlg.source_lang_var.set("Английский (EN)")
dlg.target_lang_var.set("Английский (EN)")
dlg._on_save()
check("невалидное сохранение отклонено (одинаковые языки)",
      dlg.winfo_exists() and dlg.error_label.cget("text") != ""
      and app.settings.get("source_lang") == src_lang
      and app.settings.get("target_lang") == tgt_lang)
dlg._on_close()
wait_for(app, lambda: not dlg.winfo_exists())

# --- смена направления -----------------------------------------------------------------
app.change_direction("RU → EN")
pump(0.1, app)
check("направление RU->EN: состояние и подписи",
      app.direction == "ru-en"
      and app.input_label.cget("text").endswith("(RU)")
      and app.output_label.cget("text").endswith("(EN)"))
app.change_direction("EN → RU")
pump(0.1, app)
check("направление EN->RU: состояние и подписи",
      app.direction == "en-ru"
      and app.input_label.cget("text").endswith("(EN)"))

# смена направления отменяет ожидающий автоперевод и не запускает свой
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "dir only")
app._on_input_modified()          # симуляция <<Modified>>
pump(0.05, app)
n0 = len(ft.calls)
app.change_direction("RU → EN")   # должен отменить ожидающий debounce-таймер
pump(0.5, app)
check("смена направления: «лишнего» перевода нет", len(ft.calls) == n0)
app.change_direction("EN → RU")

# --- ручной перевод ---------------------------------------------------------------------
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "hello world")
app.start_translation()
check("ручной перевод",
      wait_for(app, lambda: "[en-ru] hello world" in app.output_text.get("1.0", "end-1c")))
check("статус «Готово» после перевода",
      "Готов" in app.status_label.cget("text"))

# --- Ctrl+Enter ------------------------------------------------------------------------
n0 = len(ft.calls)
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "ctrl enter")
app.input_text._textbox.event_generate("<Control-Return>")
check("Ctrl+Enter переводит",
      wait_for(app, lambda: len(ft.calls) > n0) and ft.calls[-1][0] == "ctrl enter")

# --- Ctrl+, открывает настройки, Escape закрывает ----------------------------------------
app.event_generate("<Control-comma>")
check("Ctrl+, открывает настройки",
      wait_for(app, lambda: app._settings_dialog is not None
               and app._settings_dialog.winfo_exists()))
dlg = app._settings_dialog
dlg.event_generate("<Escape>")
check("Escape закрывает настройки",
      wait_for(app, lambda: not dlg.winfo_exists()))

# --- автоперевод: только после debounce (debounce=0.2) ------------------------------------
n0 = len(ft.calls)
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "auto test")
app._on_input_modified()
check("статус «Ожидание перевода…» до debounce",
      app.status_label.cget("text") == "Ожидание перевода…")
pump(0.1, app)
check("до debounce перевода нет", len(ft.calls) == n0)
check("автоперевод сработал после debounce",
      wait_for(app, lambda: len(ft.calls) > n0) and ft.calls[-1][0] == "auto test")
check("статус «Готово» после автоперевода",
      wait_for(app, lambda: "Готов" in app.status_label.cget("text")))

# --- быстрый набор: ровно один перевод -----------------------------------------------------
app.settings.set("debounce_sec", 0.8)
n0 = len(ft.calls)
app.input_text.delete("1.0", "end")
for ch in "fast typing test":
    app.input_text.insert("end", ch)
    app._on_input_modified()
    pump(0.02, app)
check("быстрый набор: промежуточных переводов нет", len(ft.calls) == n0)
check("быстрый набор: ровно один перевод с последним текстом",
      wait_for(app, lambda: len(ft.calls) > n0) and len(ft.calls) == n0 + 1
      and ft.calls[-1][0] == "fast typing test")
app.settings.set("debounce_sec", 0.2)

# --- «Очистить» отменяет ожидающий автоперевод -----------------------------------------------
n0 = len(ft.calls)
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "will be cleared")
app._on_input_modified()
pump(0.05, app)
app.clear_fields()
pump(0.5, app)
check("«Очистить» отменил ожидающий автоперевод", len(ft.calls) == n0)
check("поля пусты после очистки",
      app.input_text.get("1.0", "end-1c") == ""
      and app.output_text.get("1.0", "end-1c") == "")

# --- swap: ровно один перевод, без рекурсии ---------------------------------------------------
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "swap src")
app.start_translation()
wait_for(app, lambda: "[en-ru] swap src" in app.output_text.get("1.0", "end-1c"))
n0 = len(ft.calls)
app.swap_fields()
pump(0.05, app)
check("swap: ровно один автоперевод",
      wait_for(app, lambda: len(ft.calls) > n0) and len(ft.calls) == n0 + 1)
check("swap: в инпуте старый перевод, направление переставлено",
      ft.calls[-1][0] == "[en-ru] swap src" and ft.calls[-1][1] == "ru-en"
      and app.direction == "ru-en")
app.change_direction("EN → RU")
pump(0.1, app)


# --- устаревший результат не перетирает новое состояние ------------------------------------
ft.delay = 0.5
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "stale test")
app.start_translation()
pump(0.1, app)
app.change_direction("RU → EN")    # перевод в полёте: поколение меняется
pump(1.0, app)
check("устаревший результат не применён к полям",
      "[en-ru] stale test" not in app.output_text.get("1.0", "end-1c"))
ft.delay = 0.0
app.change_direction("EN → RU")
pump(0.1, app)

# --- «Перевести» при том же тексте в полёте: дубля нет -------------------------------------
ft.delay = 0.4
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "manual wins")
app._on_input_modified()
pump(0.05, app)
n0 = len(ft.calls)
app.start_translation()             # отменяет ожидающий автоперевод, стартует поток
pump(0.15, app)
app.start_translation()             # тот же текст уже переводится — игнор
pump(1.0, app)
check("ручное нажатие: ожидающий автоперевод отменён, дубля нет",
      len(ft.calls) == n0 + 1 and ft.calls[-1][0] == "manual wins")

# --- другой текст в полёте: коалесированный следующий перевод -------------------------------
ft.delay = 0.4
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "first text")
app.start_translation()
pump(0.1, app)
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "second text")
app.start_translation()             # другой текст -> уходит в pending
n0 = len(ft.calls)
pump(1.3, app)
check("коалесинг: после текущего переводится последний запрошенный текст",
      len(ft.calls) == n0 + 2 and ft.calls[-1][0] == "second text")
ft.delay = 0.0

# --- ошибка перевода видна в статусе, успешный перевод её сбрасывает --------------------------
ft.fail = True
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "fail me")
app.start_translation()
check("ошибка перевода показана в статусе",
      wait_for(app, lambda: "Ошибка перевода" in app.status_label.cget("text")))
app.input_text.delete("1.0", "end")
app.input_text.insert("1.0", "back to work")
app.start_translation()
check("успешный перевод сбрасывает ошибку",
      wait_for(app, lambda: "Готов" in app.status_label.cget("text")))

# --- окно растягивается (responsive-сетка) -----------------------------------------------------
check("окно ресайзабельно", tuple(app.resizable()) == (True, True))
app.geometry("1200x800")
pump(0.3, app)
check("окно растянулось",
      app.winfo_width() >= 1100 and app.winfo_height() >= 700)
app.geometry("900x600")
pump(0.3, app)
check("окно вернулось к прежнему размеру", app.winfo_width() < 1100)

# --- pynput-backend отсутствует: приложение не падает -------------------------------------------
import hotkey_agent as ha  # noqa: E402
saved_pynput = ha.PYNPUT_AVAILABLE
try:
    ha.PYNPUT_AVAILABLE = False
    ok = app._start_hotkey_agent()
    check("агент корректно неактивен без pynput",
          ok is False and bool(app._agent_error) and not app._hotkey_agent.active)
    # приложение продолжает работать: ручной перевод
    app.input_text.delete("1.0", "end")
    app.input_text.insert("1.0", "no pynput still works")
    app.start_translation()
    check("приложение работает без бэкенда хоткеев",
          wait_for(app, lambda: "no pynput still works"
                   in app.output_text.get("1.0", "end-1c")))
finally:
    ha.PYNPUT_AVAILABLE = saved_pynput
    app._start_hotkey_agent()

# --- subprocess: импорт main без pynput (симуляция import-ошибки pynput) ----------------------
LESS_PYNPUT_CODE = r"""
import contextlib, sys, types
fake = types.ModuleType("torch")
fake.cuda = types.SimpleNamespace(is_available=lambda: False)
fake.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake
ft = types.ModuleType("transformers")
ft.AutoTokenizer = type("AutoTokenizer", (), {})
ft.AutoModelForSeq2SeqLM = type("AutoModelForSeq2SeqLM", (), {})
sys.modules["transformers"] = ft
sys.modules["pynput"] = None          # import pynput -> ImportError
sys.modules["pynput.keyboard"] = None
import main  # импорт не должен падать
from hotkey_agent import HotkeyAgent, PYNPUT_AVAILABLE
assert PYNPUT_AVAILABLE is False
agent = HotkeyAgent(on_trigger=lambda t: None)
ok = agent.start("ctrl+alt+t")
assert ok is False and agent.error, (ok, agent.error)
agent.stop()
print("PYNPUT_LESS_OK")
"""
proc = subprocess.run([sys.executable, "-c", LESS_PYNPUT_CODE],
                      capture_output=True, text=True, cwd=ROOT, timeout=120)
check("импорт и старт без pynput: без падений (subprocess)",
      proc.returncode == 0 and "PYNPUT_LESS_OK" in proc.stdout,
      "rc=%s stderr=%s" % (proc.returncode, proc.stderr[-300:]))

# --- закрытие приложения --------------------------------------------------------------------------
app._on_window_close()
check("приложение закрылось без ошибок", True)

print("OK: %d checks passed" % len(PASS))

