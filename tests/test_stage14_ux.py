# -*- coding: utf-8 -*-
"""Этап 14: «полировка» состояния UI перевода (минимальные правки по
UX-аудиту Этапа 13):
- P0: swap с directional-моделями: при смене направления swap'ом
  автоматически выбирается совместимая модель (как у меню направления),
  перевод работает;
- user-facing статус: без developer-деталей (model_registry,
  ModuleNotFoundError, No module named, model_id, сырые исключения);
- кнопка «Перевerti» отключена во время (пере)загрузки и остаётся
  отключённой после init_error, включается только после init_done;
- ready-статус показывает фактическую модель (старт, смена модели,
  фолбэк, смена направления); предупреждение про хоткей не вытесняет
  ready/статус модели;
- заметка о фолбэке видна после загрузки, а не только в момент
  сохранения настроек;
- сохранение настроек: при старте (пере)загрузки — «Настройки
  сохранены. Загрузка модели...», без неё — обычное сообщение;
- label моделей в «Настройках»: однозначный «не подходит для ...»
  для несовместимых с направлением (без противоречивого «доступна
  (направление не поддерживается)»);
- стрим-прогресс: «Перевод… (1/3)» на первом юните (не «0/3»),
  «(N/N)» на последнем, total=1 — без прогресс-статуса.

Запуск (реальные модели не скачиваются, сеть не нужна; для GUI-части
нужен дисплей, полный GUI запускается ОДИН раз):

    python tests/test_stage14_ux.py
"""
import contextlib
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
# Стабы torch/transformers — ПЕРЕД импортом translator (как в других
# тестах): реальные модели не загружаются.
fake_torch = types.ModuleType("torch")
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake_torch
fake_transformers = types.ModuleType("transformers")
fake_transformers.AutoTokenizer = type("AutoTokenizer", (), {})
fake_transformers.AutoModelForSeq2SeqLM = type("AutoModelForSeq2SeqLM", (), {})
sys.modules["transformers"] = fake_transformers

from hotkey_agent import HotkeyAgent, PYNPUT_AVAILABLE  # noqa: E402
from translator import OfflineTranslator  # noqa: E402

# --- Ошибка направления: user-facing текст (без идентификаторов) ----
probe = types.SimpleNamespace(model_id="marian-ru-en",
                              _model_directions=("ru-en",))
try:
    OfflineTranslator._check_direction(probe, "en-ru")
    check("dir: неподдерживаемое направление даёт ValueError", False)
except ValueError as exc:
    dir_msg = str(exc)
    check("dir: сообщение понятное",
          "Модель не поддерживает выбранное направление" in dir_msg
          and "«Настройках»" in dir_msg, dir_msg)
    for dev in ("model_registry", "реестр моделей", "marian-ru-en",
                "en-ru", "ValueError"):
        check("dir: в сообщении нет developer-детали %r" % dev,
              dev not in dir_msg, dir_msg)
try:
    OfflineTranslator._check_direction(probe, "ru-en")
    check("dir: совместимое направление — без ошибки", True)
except ValueError as exc:
    check("dir: совместимое направление — без ошибки", False, exc)

# --- Агент хоткея: понятные ошибки (без сырых исключений) ----------
agent = HotkeyAgent(on_trigger=lambda text: None)
if not PYNPUT_AVAILABLE:
    ok = agent.start("ctrl+shift+c")
    check("hotkey: без pynput агент не стартует", ok is False)
    check("hotkey: self.error — понятная причина",
          agent.error == "pynput не установлен", agent.error)
    for dev in ("ModuleNotFoundError", "No module named", "Traceback"):
        check("hotkey: в self.error нет %r" % dev,
              dev not in agent.error, agent.error)
else:
    # pynput есть: глобальный слушатель в тестовом процессе НЕ
    # запускаем (перехватывал бы клавиатуру пользователя).
    print("note: pynput доступен — проверка ошибок агента пропущена")

# =====================================================================
# Часть 2. GUI (одно приложение, FakeTranslator)
# =====================================================================
import tkinter as tk  # noqa: E402

from model_registry import GGUF_ENV_VAR  # noqa: E402
from sentence_pipeline import (  # noqa: E402
    StreamUnit, assemble_output, split_units)

CFG_DIR = tempfile.mkdtemp(prefix="ot_stage14_cfg_")
os.environ["OFFLINE_TRANSLATE_CONFIG"] = CFG_DIR
# hy-mt2 по умолчанию «недоступна» (GGUF не указан) — сценарий фолбэка.
_orig_gguf = os.environ.get(GGUF_ENV_VAR)
os.environ.pop(GGUF_ENV_VAR, None)


class FakeTranslator:
    """Заместитель OfflineTranslator: детерминированный перевод
    «[...]» по юнитам (тот же контракт, что в stage10)."""

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
            t = "[%s]" % u.text
            out.append(t)
            if on_sentence is not None:
                on_sentence("done", i + 1, len(units),
                            StreamUnit(u.text, u.start, u.end,
                                       u.new_paragraph, t))
        return assemble_output(units, out)

    def translate(self, text, direction="en-ru"):
        return self.translate_stream(text, direction, None)


class FailingTranslator:
    """Переводчик, падающий при загрузке: сырое исключение с
    developer-деталями — в пользовательский статус не попадёт."""

    def __init__(self, model_id=None):
        raise RuntimeError(
            "simulated: ModuleNotFoundError: No module named 'torch'")


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


# =====================================================================
# A. Старт: модель в статусе, кнопка, предупреждение про хоткей
# =====================================================================
app = main.TranslatorApp()
check("gui: приложение запущено, переводчик загружен",
      wait_for(app, lambda: app.translator is not None
               and app._status[0] == "ready"))
app.settings.set("autotranslate", False)  # сценарий без автоперевода
st = app._status[1]
check("start: статус ready", app._status[0] == "ready", app._status)
check("start: статус показывает РЕАЛЬНО работающую модель",
      "Marian EN → RU" in st, st)
check("start: кнопка активна после init_done",
      app.translate_btn.cget("state") == "normal")
sub = app.header_subtitle.cget("text")
check("start: сабтайтл — направление + модель",
      "EN → RU" in sub and "Marian EN → RU" in sub, sub)
if not PYNPUT_AVAILABLE:
    check("start: в статусе предупреждение про хоткей",
          "Глобальный хоткей недоступен" in st, st)
    for dev in ("ModuleNotFoundError", "No module named", "Traceback"):
        check("start: в статусе нет developer-детали %r" % dev,
              dev not in st, st)
    check("start: _agent_error — чистая причина",
          app._agent_error == "pynput не установлен", app._agent_error)

# =====================================================================
# B. Кнопка: отключена во время (пере)загрузки (смена из меню)
# =====================================================================
app.change_direction("RU → EN")
check("btn: отключена при (пере)загрузке модели",
      app.translate_btn.cget("state") == "disabled")
# Состояние «ожидание» проверяем по синхронному статусу: сам переводчик
# worker может успеть подставить ДО следующего pump (быстрый FakeTranslator).
check("btn: статус ожидания загрузки",
      app._status == ("busy", "Инициализация модели..."), app._status)
check("btn: готова после загрузки",
      wait_for(app, lambda: app.translator is not None
               and app._translator_model_id == "marian-ru-en"
               and app._status[0] == "ready"))
check("btn: активна после init_done",
      app.translate_btn.cget("state") == "normal")
check("btn: статус показывает новую модель",
      "Marian RU → EN" in app._status[1], app._status)

# =====================================================================
# C. init_error: понятная ошибка, кнопка остаётся отключённой
# =====================================================================
_orig_cls = main.OfflineTranslator
main.OfflineTranslator = FailingTranslator
app._start_model_load()
check("err: кнопка отключена в начале загрузки",
      app.translate_btn.cget("state") == "disabled")
check("err: статус стал error",
      wait_for(app, lambda: app._status[0] == "error"))
main.OfflineTranslator = _orig_cls  # worker уже отработал (ошибка в очереди)
check("err: статус — понятный, без сырого исключения",
      app._status[1]
      == "Не удалось загрузить модель. Проверьте настройки модели "
         "и доступность необходимых файлов.", app._status)
for dev in ("ModuleNotFoundError", "No module named", "simulated",
            "RuntimeError", "torch"):
    check("err: в статусе нет developer-детали %r" % dev,
          dev not in app._status[1], app._status)
check("err: после init_error кнопка ОСТАЁТСЯ отключённой",
      app.translate_btn.cget("state") == "disabled")
# Восстановление: смена направления запускает новую загрузку.
app.change_direction("EN → RU")
check("err: recovery — модель перезагружена",
      wait_for(app, lambda: app.translator is not None
               and app._translator_model_id == "marian-en-ru"
               and app._status[0] == "ready"))
check("err: recovery — кнопка активна",
      app.translate_btn.cget("state") == "normal")

# =====================================================================
# D. P0: swap с directional-моделями
# =====================================================================
# Текущее состояние: EN→RU + Marian EN → RU (загружена). Смена
# направления ИЗ МЕНЮ RU→EN — автоматический выбор совместимой модели.
app.change_direction("RU → EN")
check("swap: меню сменило направление на RU→EN",
      app.direction == "ru-en", app.direction)
check("swap: меню — модель автоматически Marian RU → EN",
      app.model_id == "marian-ru-en"
      and wait_for(app, lambda: app.translator is not None
                   and app._translator_model_id == "marian-ru-en"
                   and app._status[0] == "ready"),
      (app.model_id, app._translator_model_id))

# Swap: «старый перевод» уезжает в исходное поле, текст — в вывод.
app.clear_fields()
app.input_text.insert("1.0", "Hello world. How are you?")
app.output_text.insert("1.0", "Старый результат перевода.")
pump(0.1, app)

app.swap_fields()
check("swap: направление стало EN→RU",
      app.direction == "en-ru", app.direction)
check("swap: содержимое полей поменялось",
      app.input_text.get("1.0", "end-1c") == "Старый результат перевода."
      and app.output_text.get("1.0", "end-1c")
      == "Hello world. How are you?")
check("swap: модель — совместимая с EN→RU (P0: не RU→EN-модель)",
      app.model_id == "marian-en-ru", app.model_id)
check("swap: во время перезагрузки модели — кнопка отключена",
      app.translate_btn.cget("state") == "disabled")
check("swap: перевод работает — модель загружена для EN→RU",
      wait_for(app, lambda: app.translator is not None
               and app._translator_model_id == "marian-en-ru"
               and app._status[0] == "ready"))
check("swap: кнопка активна после загрузки",
      app.translate_btn.cget("state") == "normal")
st = app._status[1]
check("swap: в статусе заметка об авто-смене модели",
      "не поддерживает направление" in st and "Marian EN → RU" in st, st)

# Ручной перевод в новом направлении: без ошибки, результат пришёл.
app.start_translation()
check("swap: перевод завершён",
      wait_for(app, lambda: not app._translation_busy
               and app.output_text.get("1.0", "end-1c").strip() != ""))
check("swap: результат в поле вывода",
      "[Старый результат перевода.]"
      in app.output_text.get("1.0", "end-1c"),
      app.output_text.get("1.0", "end-1c"))
check("swap: в статусе нет ошибки после перевода",
      app._status[0] != "error", app._status)

# =====================================================================
# E. «Настройки»: labels, заметка о фолбэке, статус сохранения
# =====================================================================
app._open_settings()
check("dlg: диалог открыт",
      wait_for(app, lambda: (app._settings_dialog is not None
                             and app._settings_dialog.winfo_exists())))
dlg = app._settings_dialog
pump(0.3, app)
dlg._update_agent_state()

labels = dict((mid, lab) for lab, mid in dlg._model_labels("en-ru"))
lab_enru = labels["marian-en-ru"]
lab_ruen = labels["marian-ru-en"]
lab_hy = labels["hy-mt2-1.8b"]
check("label: совместимая — доступность, без «не подходит»",
      "не подходит" not in lab_enru
      and ("доступна" in lab_enru or "недоступна локально" in lab_enru),
      lab_enru)
check("label: несовместимая — однозначная, без «доступна»",
      lab_ruen.endswith(" — не подходит для EN → RU")
      and "доступна" not in lab_ruen, lab_ruen)
if not PYNPUT_AVAILABLE:
    ag_txt = dlg.agent_state_label.cget("text")
    check("dlg: статус хоткея — без developer-деталей",
          ag_txt == "Недоступен: pynput не установлен", ag_txt)
hy_avail = app.model_manager.is_model_available("hy-mt2-1.8b")
exp_hy = " — доступна" if hy_avail else " — недоступна локально"
check("label: двунаправленная — маркер доступности",
      lab_hy.endswith(exp_hy) and "не подходит" not in lab_hy,
      (lab_hy, hy_avail))

if not hy_avail:
    # Выбор недоступной модели + сохранение: фолбэк на дефолт
    # направления БЕЗ перезагрузки (дефолт и так работает) — заметка
    # не теряется, а попадает в статус сохранения.
    dlg.model_var.set(lab_hy)
    dlg._on_model_selected(lab_hy)
    pump(0.1, app)
    dlg.save_btn.invoke()
    check("dlg: сохранение закрыло диалог",
          wait_for(app, lambda: not dlg.winfo_exists()))
    check("save: выбор пользователя (hy-mt2) сохранён",
          app.settings.get("model_id") == "hy-mt2-1.8b",
          app.settings.get("model_id"))
    check("save: запускается дефолт направления",
          app._active_model_id == "marian-en-ru",
          app._active_model_id)
    st = app._status[1]
    check("save: в статусе — сохранение + заметка о фолбэке",
          st.startswith("Настройки сохранены")
          and "сейчас недоступна — использую" in st
          and "Marian EN → RU" in st, st)
else:
    print("note: hy-mt2-1.8b доступна в окружении — сценарий фолбэка "
          "пропущен")
    dlg._on_close()
    wait_for(app, lambda: not dlg.winfo_exists())

# Смена направления из диалога: сохранение запускает (пере)загрузку —
# статус честный, а после загрузки видны модель и (если была) заметка.
app._open_settings()
check("dlg2: диалог открыт",
      wait_for(app, lambda: (app._settings_dialog is not None
                             and app._settings_dialog.winfo_exists())))
dlg2 = app._settings_dialog
pump(0.3, app)
dlg2.source_lang_var.set("Русский (RU)")
dlg2._on_source_lang("Русский (RU)")
pump(0.2, app)
check("dlg2: направление RU→EN",
      dlg2._model_direction() == "ru-en", dlg2._model_direction())
if not hy_avail:
    # Выбор НЕДОСТУПНОЙ модели в НОВОМ направлении: при сохранении
    # _set_model разрешит её в дефолт с заметкой о фолбэке — заметка
    # должна прожить до init_done и быть видна ПОСЛЕ загрузки (а не
    # только в момент сохранения).
    labels_ruen = dict((mid, lab)
                       for lab, mid in dlg2._model_labels("ru-en"))
    dlg2.model_var.set(labels_ruen["hy-mt2-1.8b"])
    dlg2._on_model_selected(labels_ruen["hy-mt2-1.8b"])
    pump(0.1, app)
dlg2.save_btn.invoke()
check("save: при (пере)загрузке — честный статус",
      app._status[1] == "Настройки сохранены. Загрузка модели...",
      app._status)
check("save: ready после перезагрузки",
      wait_for(app, lambda: app.translator is not None
               and app._translator_model_id == "marian-ru-en"
               and app._status[0] == "ready"))
st = app._status[1]
check("save: в статусе после загрузки — РЕАЛЬНАЯ модель",
      "Marian RU → EN" in st, st)
if not hy_avail:
    check("save: заметка о фолбэке видна ПОСЛЕ загрузки",
          "сейчас недоступна — использую" in st, st)
for dev in ("model_registry", "реестр моделей", "ModuleNotFoundError",
            "No module named", "Traceback", "marian-en-ru",
            "marian-ru-en", "hy-mt2-1.8b"):
    check("status: в статусе нет developer-детали %r" % dev,
          dev not in st, st)

# Без изменений — обычное сообщение (без ложного «загрузка»).
if not hy_avail:
    # Выбор hy-mt2 (фолбэк) — намеренный: возвращаем модель, которая
    # реально работает, чтобы сохранение было «ничего не меняется».
    app._set_model("marian-ru-en")
    check("setup: возвращена рабочая модель",
          wait_for(app, lambda: app.translator is not None))
app._open_settings()
check("dlg3: диалог открыт",
      wait_for(app, lambda: (app._settings_dialog is not None
                             and app._settings_dialog.winfo_exists())))
dlg3 = app._settings_dialog
pump(0.3, app)
dlg3.save_btn.invoke()
check("save: без изменений — обычное сообщение",
      app._status[1] == "Настройки сохранены", app._status)
check("dlg3: сохранение закрыло диалог",
      wait_for(app, lambda: not dlg3.winfo_exists()))

# =====================================================================
# F. Стрим-прогресс: текущий юнит, а не число завершённых
# =====================================================================
app.clear_fields()
app.input_text.insert("1.0", "One. Two. Three.")
U3 = split_units("One. Two. Three.")
check("prog: пайплайн разбил на 3 юнита", len(U3) == 3, U3)
app._apply_stream_sentence(
    "start", 0, 3, StreamUnit(U3[0].text, U3[0].start, U3[0].end,
                              U3[0].new_paragraph))
check("prog: первый юнит — «1/3» (не «0/3»)",
      app._status[1] == "Перевод… (1/3)", app._status)
app._apply_stream_sentence(
    "start", 1, 3, StreamUnit(U3[1].text, U3[1].start, U3[1].end,
                              U3[1].new_paragraph))
check("prog: средний — «2/3»",
      app._status[1] == "Перевод… (2/3)", app._status)
app._apply_stream_sentence(
    "start", 2, 3, StreamUnit(U3[2].text, U3[2].start, U3[2].end,
                              U3[2].new_paragraph))
check("prog: последний юнит — «3/3»",
      app._status[1] == "Перевод… (3/3)", app._status)
before = app._status
app._apply_stream_sentence(
    "start", 0, 1, StreamUnit(U3[0].text, U3[0].start, U3[0].end,
                              U3[0].new_paragraph))
check("prog: total=1 — прогресс-статус не показывается",
      app._status == before, (before, app._status))

# ===========================================================
# Финал: окружение и приложение
# ===========================================================
if _orig_gguf is None:
    os.environ.pop(GGUF_ENV_VAR, None)
else:
    os.environ[GGUF_ENV_VAR] = _orig_gguf
app.destroy()
print("OK: %d checks passed" % len(PASS))