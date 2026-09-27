# -*- coding: utf-8 -*-
"""Этап 10: выбор модели в «Настройках» + двусторонняя подсветка пар
«предложение ↔ перевод» и синхронная прокрутка.

Запуск (реальные модели не скачиваются, сеть не нужна; для GUI-части
нужен дисплей, полный GUI запускается ОДИН раз):

    python tests/test_stage10_ui.py

Структура:
1. Чистая логика (без GUI): валидация settings.model_id;
   ModelManager.resolve_runtime (неизвестный id / несовместимое
   направление / недоступная модель с фолбэком / модель недоступна без
   фолбэка / нет модели для направления); backend-нейтральность
   model_registry/settings (torch/transformers/llama_cpp не импортируются).
2. GUI (одно приложение, FakeTranslator): диалог «Настройки» (список
   моделей из реестра, доступность, смена направления, заметка, сохранение
   через app._set_model); сброс mapping/подсветки при смене направления и
   модели; hover из обоих полей (по смещениям пайплайна, без текста);
   двусторонняя синхронная прокрутка (середина/верх/низ, без рекурсии);
   полный pipeline (стрим -> unit_map -> финальный текст -> отложенный
   сброс подсветки).
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
from model_registry import (  # noqa: E402
    GGUF_ENV_VAR, ModelDescriptor, ModelManager, ModelNotFoundError,
    ModelRegistry, default_registry)
from settings import Settings, validate_value  # noqa: E402

# При запуске под pytest до нас могут импортировать torch/transformers
# другие скрипт-тесты (известная проблема изоляции, см. README/задачи по
# Этапу 10) — в «грязном» процессе утверждаем только то, что НАШЕ
# импортирование model_registry/settings не тянет тяжёлые модули.
_pure = [m for m in ("torch", "transformers", "llama_cpp")
         if m not in sys.modules]
if _pure:
    for mod in _pure:
        check("pure: %s не импортирован" % mod, mod not in sys.modules)
else:
    print("note: torch/transformers уже в sys.modules (pytest) — "
          "чистота-проверка пропущена")

# --- settings: валидация model_id ---
check("settings: model_id None валиден",
      validate_value("model_id", None) == (True, None))
check("settings: model_id строка валидна",
      validate_value("model_id", "marian-en-ru") == (True, "marian-en-ru"))
check("settings: model_id обрезается",
      validate_value("model_id", "  hy-mt2-1.8b ") == (True, "hy-mt2-1.8b"))
check("settings: model_id пустая строка отклоняется",
      validate_value("model_id", "   ")[0] is False)
check("settings: model_id не-строка отклоняется",
      validate_value("model_id", 123)[0] is False)

CFG_DIR = tempfile.mkdtemp(prefix="ot_stage10_cfg_")
os.environ["OFFLINE_TRANSLATE_CONFIG"] = CFG_DIR
st = Settings()
check("settings: model_id по умолчанию None", st.get("model_id") is None)
check("settings: model_id устанавливается",
      st.set("model_id", "marian-ru-en") is True)
st.save()
with open(os.path.join(CFG_DIR, "settings.json"), encoding="utf-8") as f:
    saved_cfg = json.load(f)
check("settings: model_id записан в файл",
      saved_cfg.get("model_id") == "marian-ru-en")
st2 = Settings()
check("settings: model_id читается из файла",
      st2.get("model_id") == "marian-ru-en")

# --- ModelManager.resolve_runtime ---
REG_TMP = tempfile.mkdtemp(prefix="ot_stage10_reg_")


def _fake_hf_model(cache_dir, repo_id):
    """Фейковый HF-кэш (v2-layout):
    <cache>/models--<org>--<name>/snapshots/<rev>/config.json."""
    d = os.path.join(cache_dir, "models--" + repo_id.replace("/", "--"),
                     "snapshots", "snap1")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
        f.write("{}")


def _set_gguf_env(path_or_none):
    if path_or_none is None:
        os.environ.pop(GGUF_ENV_VAR, None)
    else:
        os.environ[GGUF_ENV_VAR] = path_or_none


_orig_gguf_env = os.environ.get(GGUF_ENV_VAR)
try:
    mgr = ModelManager(cache_dir=REG_TMP)  # стандартный реестр
    _fake_hf_model(REG_TMP, "Helsinki-NLP/opus-mt-en-ru")
    _fake_hf_model(REG_TMP, "Helsinki-NLP/opus-mt-ru-en")

    # 1. Первый запуск (выбор не делался) — дефолт направления, persist.
    run, persist, note = mgr.resolve_runtime(None, "en-ru")
    check("resolve: None -> дефолт направления, persist",
          run == "marian-en-ru" and persist == "marian-en-ru"
          and note is None, (run, persist, note))

    # 2. Неизвестный сохранённый id — дефолт направления, persist (без сбоя).
    run, persist, note = mgr.resolve_runtime("no-such-model", "en-ru")
    check("resolve: неизвестный id -> дефолт направления, persist",
          run == "marian-en-ru" and persist == "marian-en-ru"
          and note is None, (run, persist, note))

    # 3. Сохранённая модель несовместима с направлением — дефолт, persist.
    run, persist, note = mgr.resolve_runtime("marian-en-ru", "ru-en")
    check("resolve: несовместимая -> дефолт направления, persist",
          run == "marian-ru-en" and persist == "marian-ru-en"
          and note is None, (run, persist, note))

    # 4. Сохранённая модель НЕДОСТУПНА, дефолт направления доступен:
    #    запускаем дефолт, выбор пользователя НЕ меняется (persist None),
    #    заметка — есть; скачивания нет.
    _set_gguf_env(os.path.join(REG_TMP, "absent.gguf"))
    run, persist, note = mgr.resolve_runtime("hy-mt2-1.8b", "en-ru")
    check("resolve: недоступная + фолбэк -> запуск дефолта, выбор сохранён",
          run == "marian-en-ru" and persist is None
          and note is not None and "недоступна" in note,
          (run, persist, note))

    # 5. Сохранённая модель доступна и совместима — без изменений.
    gguf_path = os.path.join(REG_TMP, "Hy-MT2-1.8B-Q4_K_M.gguf")
    with open(gguf_path, "wb") as f:
        f.write(b"GGUF")
    _set_gguf_env(gguf_path)
    run, persist, note = mgr.resolve_runtime("hy-mt2-1.8b", "en-ru")
    check("resolve: доступная и совместимая -> как есть",
          run == "hy-mt2-1.8b" and persist is None and note is None,
          (run, persist, note))

    # 6. Ни одна модель не поддерживает направление — ModelNotFoundError.
    reg2 = ModelRegistry()
    reg2.register(ModelDescriptor(
        id="ru-only", name="RU only", backend="llama_cpp",
        directions=("ru-en",), source="local file", gguf_model="X"))
    _set_gguf_env(None)
    mgr2 = ModelManager(registry=reg2, cache_dir=REG_TMP)
    try:
        mgr2.resolve_runtime(None, "en-ru")
        check("resolve: нет модели для направления -> ModelNotFoundError",
              False)
    except ModelNotFoundError:
        check("resolve: нет модели для направления -> ModelNotFoundError",
              True)

    # 7. Модель для направления есть, но недоступна и фолбэка нет —
    #    resolve не падает: запускаем её как есть (clear error даст сама
    #    инициализация), persist устанавливается.
    reg3 = ModelRegistry()
    reg3.register(ModelDescriptor(
        id="gguf-only", name="GGUF only", backend="llama_cpp",
        directions=("en-ru", "ru-en"), source="local file", gguf_model="X"))
    mgr3 = ModelManager(registry=reg3, cache_dir=REG_TMP)
    run, persist, note = mgr3.resolve_runtime(None, "en-ru")
    check("resolve: недоступная без фолбэка -> как есть (ошибка даст запуск)",
          run == "gguf-only" and persist == "gguf-only", (run, persist))

    # 8. Без сети и бэкендов: resolve_runtime не импортирует тяжёлые модули.
    before = set(sys.modules)
    mgr.resolve_runtime("hy-mt2-1.8b", "ru-en")
    new_mods = set(sys.modules) - before
    check("resolve: тяжёлые модули не импортируются",
          not (new_mods & {"torch", "transformers", "llama_cpp",
                           "huggingface_hub", "requests"}), sorted(new_mods))
finally:
    if _orig_gguf_env is None:
        os.environ.pop(GGUF_ENV_VAR, None)
    else:
        os.environ[GGUF_ENV_VAR] = _orig_gguf_env

# =====================================================================
# Часть 2. GUI (одно приложение, FakeTranslator)
# =====================================================================
# Стабы torch/transformers — ПЕРЕД импортом main/translator (как в других
# тестах): реальные модели не загружаются.
fake_torch = types.ModuleType("torch")
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake_torch
fake_transformers = types.ModuleType("transformers")
fake_transformers.AutoTokenizer = type("AutoTokenizer", (), {})
fake_transformers.AutoModelForSeq2SeqLM = type("AutoModelForSeq2SeqLM", (), {})
sys.modules["transformers"] = fake_transformers

import tkinter as tk  # noqa: E402

from sentence_pipeline import (  # noqa: E402
    StreamUnit, assemble_output, off_to_tk, split_units)


class FakeTranslator:
    """Заместитель OfflineTranslator: детерминированный перевод «[...»»
    по юнитам и инкрементальный интерфейс того же контракта. model_id —
    backend-нейтральный (как в реальном фасаде, Этап 9)."""

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
    """Диапазоны тега (смещения в символах) — как hl_ranges в stage5."""
    text = tb.get("1.0", "end-1c")
    offs = [0]
    for j, ch in enumerate(text):
        if ch == "\n":
            offs.append(j + 1)

    def off(idx):
        ls, cs = str(idx).split(".", 1)  # str: index() вернул Tcl_Obj
        return offs[int(ls) - 1] + int(cs)

    ranges = tb.tag_ranges(tag) or ()
    return [(off(ranges[i]), off(ranges[i + 1]))
            for i in range(0, len(ranges), 2)]


def yview(tb):
    v = tb.yview()
    return (float(v[0]), float(v[1])) if v else (0.0, 1.0)


app = main.TranslatorApp()
check("gui: приложение запущено, переводчик загружен",
      wait_for(app, lambda: app.translator is not None))
app.settings.set("autotranslate", False)  # сценарий без автоперевода
check("gui: стартовый model_id разрешён в дефолт направления",
      app.model_id == app.model_manager.get_default_model(
          app.direction).id,
      (app.model_id, app.direction))

# =====================================================================
# A. Настройки: выбор модели (лёгкий, backend-нейтральный)
# =====================================================================
app._open_settings()
check("dlg: диалог открыт",
      wait_for(app, lambda: (app._settings_dialog is not None
                             and app._settings_dialog.winfo_exists())))
dlg = app._settings_dialog
pump(0.3, app)

labels = dlg._model_labels(dlg._model_direction())
check("dlg: список моделей — из реестра (без бэкендов)",
      sorted(mid for _l, mid in labels)
      == sorted(d.id for d in app.model_manager.list_models()), labels)
for label, mid in labels:
    avail = app.model_manager.is_model_available(mid)
    mark = "доступна" if avail else "недоступна локально"
    check("dlg: label %s показывает доступность" % mid, mark in label,
          label)
default_desc = app.model_manager.get_default_model(app.direction)
check("dlg: первичный выбор — дефолт направления",
      dlg._model_label_to_id.get(dlg.model_var.get()) == default_desc.id,
      dlg.model_var.get())
check("dlg: бэкенды не импортированы после открытия диалога",
      sys.modules.get("torch") is fake_torch
      and "llama_cpp" not in sys.modules)

# Смена направления в диалоге — список/выбор моделей актуализируются.
# Точная эмуляция выбора из дропдауна (CTk 5.x: command не зовётся при
# программном set; _dropdown_callback = «set переменной + command»).
dlg.source_lang_var.set("Русский (RU)")
dlg._on_source_lang("Русский (RU)")
pump(0.2, app)
check("dlg: направление стало RU->EN", dlg._model_direction() == "ru-en")
default_ru = app.model_manager.get_default_model("ru-en")
check("dlg: выбор переключился на дефолт нового направления",
      dlg._model_label_to_id.get(dlg.model_var.get()) == default_ru.id,
      dlg.model_var.get())
marked = [l for l, mid in dlg._model_labels("ru-en")
          if not app.model_manager.get_model(mid).supports_direction("ru-en")]
check("dlg: несовместимые с направлением модели помечены",
      len(marked) >= 1 and all("не поддерживается" in l for l in marked),
      marked)

# Выбор модели + сохранение — через app._set_model.
gguf_label = next(l for l, mid in dlg._model_labels("ru-en")
                  if mid == "hy-mt2-1.8b")
dlg.model_var.set(gguf_label)
dlg._on_model_selected(gguf_label)
note_text = dlg.model_note_label.cget("text")
check("dlg: заметка под меню модели не пуста", note_text != "", note_text)
dlg.save_btn.invoke()
check("dlg: сохранение закрыло диалог",
      wait_for(app, lambda: not dlg.winfo_exists()))
expected_run, _persist, _note = app.model_manager.resolve_runtime(
    "hy-mt2-1.8b", "ru-en")
check("dlg: model_id персистентен после сохранения",
      app.settings.get("model_id") == "hy-mt2-1.8b",
      app.settings.get("model_id"))
check("dlg: app.model_id обновлён", app.model_id == "hy-mt2-1.8b")
check("dlg: запуск идёт моделью из resolve_runtime",
      app._active_model_id == expected_run,
      (app._active_model_id, expected_run))
check("dlg: переводчик (пере)загружен под новую модель",
      wait_for(app, lambda: app.translator is not None
               and app._translator_model_id == app._active_model_id))
check("dlg: после смены модели бэкенды не импортированы",
      sys.modules.get("torch") is fake_torch
      and "llama_cpp" not in sys.modules)

# =====================================================================
# B. Смена направления/модели сбрасывает mapping и подсветку
# =====================================================================
app._unit_map = [(0, 5, 0, 3)]
app._src_starts = [0]
app._dst_starts = [0]
app._hl_unit = 0
app._stream_unit = 0
app._set_direction("en-ru")
check("reset: смена направления сбрасывает mapping/подсветку",
      app._unit_map == [] and app._src_starts == []
      and app._dst_starts == [] and app._hl_unit is None
      and app._stream_unit is None)
app._unit_map = [(0, 5, 0, 3)]
app._src_starts = [0]
app._dst_starts = [0]
app._hl_unit = 0
app._stream_unit = 0
app._set_model("marian-en-ru")
check("reset: смена модели сбрасывает mapping/подсветку",
      app._unit_map == [] and app._src_starts == []
      and app._dst_starts == [] and app._hl_unit is None
      and app._stream_unit is None)
check("gui: переводчик готов к pipeline-проверкам",
      wait_for(app, lambda: app.translator is not None
               and app._translator_model_id == app._active_model_id))

# =====================================================================
# C. Подсветка: unit_map из смещений, hover из обоих полей
# =====================================================================
SRC = "Hello world. How are you?"
U = split_units(SRC)
check("C: пайплайн разбил на два предложения", len(U) == 2, U)
app.clear_fields()
app.input_text.insert("1.0", SRC)
pump(0.1, app)

app._apply_stream_sentence(
    "start", 0, 2, StreamUnit(U[0].text, U[0].start, U[0].end,
                              U[0].new_paragraph))
app._apply_stream_sentence(
    "done", 1, 2, StreamUnit(U[0].text, U[0].start, U[0].end,
                             U[0].new_paragraph, "[Hello world.]"))
app._apply_stream_sentence(
    "done", 2, 2, StreamUnit(U[1].text, U[1].start, U[1].end,
                             U[1].new_paragraph, "[How are you?]"))
pump(0.1, app)

check("map: накопились два юнита", len(app._unit_map) == 2, app._unit_map)
e0, e1 = app._unit_map
check("map: src-смещения — из пайплайна",
      (e0[0], e0[1]) == (U[0].start, U[0].end)
      and (e1[0], e1[1]) == (U[1].start, U[1].end), app._unit_map)
out = app.output_text.get("1.0", "end-1c")
check("map: dst-смещения указывают на фактический текст перевода",
      out[e0[2]:e0[3]] == "[Hello world.]"
      and out[e1[2]:e1[3]] == "[How are you?]", out)
check("hl: активен последний юнит", app._hl_unit == 1
      and app._stream_unit == 1, app._hl_unit)
check("hl: оба поля подсвечены активную пару",
      tag_offsets(app.input_text._textbox, app._hl_src_tag)
      == [(e1[0], e1[1])]
      and tag_offsets(app.output_text._textbox, app._hl_dst_tag)
      == [(e1[2], e1[3])])


def hover_on(pane, off):
    """Эмуляция мыши над символьным смещением off поля (pane "src"/"dst"):
    index() временно заглушен — пиксельная точность дисплея не нужна."""
    tb = app.input_text._textbox if pane == "src" else app.output_text._textbox
    text = tb.get("1.0", "end-1c")
    orig_index = tb.index
    try:
        tb.index = lambda *a, **k: off_to_tk(text, off)
        ev = types.SimpleNamespace(widget=tb, x=1, y=1)
        app._on_hover_move(pane, ev)
    finally:
        tb.index = orig_index


hover_on("src", U[0].start + 1)
check("hover: из исходного поля — пара юнита 0 подсвечена в обоих",
      app._hl_unit == 0
      and tag_offsets(app.input_text._textbox, app._hl_src_tag)
      == [(e0[0], e0[1])]
      and tag_offsets(app.output_text._textbox, app._hl_dst_tag)
      == [(e0[2], e0[3])])
hover_on("dst", e1[2] + 1)
check("hover: из поля перевода — пара юнита 1 подсвечена в обоих",
      app._hl_unit == 1
      and tag_offsets(app.input_text._textbox, app._hl_src_tag)
      == [(e1[0], e1[1])]
      and tag_offsets(app.output_text._textbox, app._hl_dst_tag)
      == [(e1[2], e1[3])])
hover_on("src", e0[1])  # разделитель между предложениями — «зазор»
check("hover: зазор между предложениями — подсветка снята",
      app._hl_unit is None
      and tag_offsets(app.input_text._textbox, app._hl_src_tag) == []
      and tag_offsets(app.output_text._textbox, app._hl_dst_tag) == [])
app._translation_busy = True
app._stream_unit = 1
app._on_hover_leave("src", None)
check("hover: leave во время перевода — назад на стрим-юнит",
      app._hl_unit == 1, app._hl_unit)
app._translation_busy = False
app._on_hover_leave("dst", None)
check("hover: leave вне перевода — подсветка снята",
      app._hl_unit is None
      and tag_offsets(app.output_text._textbox, app._hl_dst_tag) == [])

# =====================================================================
# D. Двусторонняя синхронная прокрутка
# =====================================================================
app.clear_fields()
long_in = "\n".join("source line %03d payload" % i for i in range(400))
long_out = "\n".join("output line %03d payload" % i for i in range(600))
app.input_text.insert("1.0", long_in)
app.output_text.insert("1.0", long_out)
pump(0.3, app)
in_tb = app.input_text._textbox
out_tb = app.output_text._textbox
check("D: оба поля прокручиваются",
      yview(in_tb)[1] < 0.99 and yview(out_tb)[1] < 0.99,
      (yview(in_tb), yview(out_tb)))

in_tb.yview_moveto(0.3)
pump(0.3, app)
check("scroll: середина исходное -> вывод",
      abs(yview(out_tb)[0] - 0.3) < 0.04, (yview(in_tb), yview(out_tb)))

out_tb.yview_moveto(0.55)
pump(0.3, app)
check("scroll: середина вывод -> исходное",
      abs(yview(in_tb)[0] - 0.55) < 0.04, (yview(in_tb), yview(out_tb)))

in_tb.yview_moveto(0.0)
pump(0.3, app)
check("scroll: верх исходное -> вывод", yview(out_tb)[0] < 0.02,
      yview(out_tb))

in_tb.yview_moveto(0.9999)
pump(0.3, app)
check("scroll: низ исходное -> вывод", yview(out_tb)[1] > 0.98,
      yview(out_tb))

# Защита от рекурсии: одно прямое событие -> прямая синхронизация +
# не более одного заблокированного обратного callback'а (yview_moveto —
# синхронный, защита _syncing_scroll работает).
orig_partner = app._sync_partner_view
sync_calls = []


def counting_partner(partner_tb, first, last):
    sync_calls.append(partner_tb is out_tb)
    return orig_partner(partner_tb, first, last)


app._sync_partner_view = counting_partner
in_tb.yview_moveto(0.2)
pump(0.2, app)
app._sync_partner_view = orig_partner
check("scroll: рекурсивного цикла нет (кол-во callback'ов ограничено)",
      1 <= len(sync_calls) <= 4 and sync_calls[0] is True, sync_calls)
check("scroll: вид вывода совпал с исходным после синхронизации",
      abs(yview(out_tb)[0] - 0.2) < 0.04, yview(out_tb))

app._auto_follow = True
app._on_user_scroll(None)
check("scroll: ручная прокрутка отключает автопоказ",
      app._auto_follow is False)

# =====================================================================
# E. Полный pipeline: стрим -> unit_map -> финальный текст ->
#    отложенный сброс подсветки
# =====================================================================
app.clear_fields()
app.input_text.insert("1.0", "One. Two. Three.")
app._auto_follow = True  # автопоказ снова доступен (как при новом запуске)
app.start_translation()
check("pipe: перевод завершён",
      wait_for(app, lambda: (not app._translation_busy
                             and app.output_text.get("1.0", "end-1c")
                             .strip() != "")))
pump(0.2, app)
out = app.output_text.get("1.0", "end-1c")
U3 = split_units("One. Two. Three.")
check("pipe: в mapping три юнита", len(app._unit_map) == 3, app._unit_map)
check("pipe: dst-смещения совпадают с финальным текстом",
      all(out[s:e] == "[%s]" % u.text
          for u, (_ss, _se, s, e) in zip(U3, app._unit_map)), out)
check("pipe: src-смещения — из пайплайна",
      all((ss, se) == (u.start, u.end)
          for u, (ss, se, _s, _e) in zip(U3, app._unit_map)),
      app._unit_map)
check("pipe: dst-диапазоны упорядочены и не пересекаются",
      all(app._unit_map[i][2] >= app._unit_map[i - 1][3]
          for i in range(1, 3)), app._unit_map)
check("pipe: после translation_done подсвечена последняя пара",
      app._hl_unit == 2, app._hl_unit)
pump(1.2, app)  # отложенный сброс (grace ~0.7 c)
check("pipe: после завершения подсветка снята",
      app._hl_unit is None
      and tag_offsets(app.input_text._textbox, app._hl_src_tag) == []
      and tag_offsets(app.output_text._textbox, app._hl_dst_tag) == [])

app.destroy()
print("OK: %d checks passed" % len(PASS))