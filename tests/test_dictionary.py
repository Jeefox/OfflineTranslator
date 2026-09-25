# -*- coding: utf-8 -*-
"""Тесты словаря: общий core (snapshot), DictionaryManager,
подключение к OfflineTranslator (stub torch/transformers).

Запуск (реальные модели не скачиваются, сеть и pytest не нужны):

    python tests/test_dictionary.py

Проверяют:
- загрузку существующего legacy dictionary.json (40 записей -> 20 пар);
- двунаправленный exact lookup, регистронезависимость, регистр значений;
- add/edit/delete логических пар (зеркальные записи работают сами);
- политику 1:1 (O3): конфликты запрещены, зеркало той же пары — не конфликт;
- импорт: валидация, частичные ошибки, top-level != dict, broken JSON,
  один атомарный save, отсутствие partial state;
- атомарный save: при ошибке файл не трогается, память не меняется;
- reload после внешнего изменения файла;
- translator видит изменения БЕЗ перезапуска объекта и читает snapshot
  ровно ОДИН раз за translate() (включая multi-chunk текст);
- при отсутствии match используется нейросеть, при match — не используется;
- оба направления EN->RU и RU->EN; graceful fallback при отсутствии/
  повреждении dictionary.json.
"""
import contextlib
import json
import os
import sys
import tempfile
import types

# Корень репозитория — родитель tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ENQ = []    # очередь текстов, закодированных для inference
CALLS = []  # (model_name, chunk) — все вызовы generate


class FakeEncoded:
    """Имитация BatchEncoding: [], .to() и **распаковку."""

    def __init__(self, ids, text):
        self._ids = ids
        self.text = text

    def __getitem__(self, key):
        return self._ids if key == "input_ids" else None

    def keys(self):
        return ("input_ids",)

    def to(self, *args, **kwargs):
        return self


class FakeTokenizer:
    """Детерминированный 'токенизатор': 2 спецтокена + 1 на слово."""

    def __call__(self, text, return_tensors=None, padding=False,
                 truncation=False, max_length=None, add_special_tokens=True):
        if return_tensors:
            ENQ.append(text)
        return FakeEncoded([0] * (2 + len(text.split())), text)

    def decode(self, item, skip_special_tokens=False):
        # 'Модель' эхо-отвечает куском — так проверяем полноту и порядок
        return "⟪" + item.text + "⟫"


class FakeModel:
    def __init__(self, name):
        self.name = name

        class _Cfg:
            max_position_embeddings = 512

        self.config = _Cfg()

    def to(self, *a, **k):
        return self

    def eval(self):
        pass

    def generate(self, **kwargs):
        text = ENQ.pop()
        CALLS.append((self.name, text))
        return [FakeEncoded([0], text)]


class FakeAutoTokenizer:
    @staticmethod
    def from_pretrained(name, cache_dir=None, **kw):
        return FakeTokenizer()


class FakeAutoModel:
    @staticmethod
    def from_pretrained(name, cache_dir=None, **kw):
        return FakeModel(name)


# Стыки должны быть установлены ПЕРЕД импортом translator
fake_torch = types.ModuleType("torch")
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake_torch
fake_transformers = types.ModuleType("transformers")
fake_transformers.AutoTokenizer = FakeAutoTokenizer
fake_transformers.AutoModelForSeq2SeqLM = FakeAutoModel
sys.modules["transformers"] = fake_transformers

import translator  # noqa: E402  (реальный модуль, после установки стыков)
from translator import OfflineTranslator  # noqa: E402
from dictionary_manager import (  # noqa: E402
    DictionaryManager,
    default_dictionary_path,
    load_snapshot,
    serialize_pairs,
)

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:300]))
    PASS.append(name)


def reset():
    CALLS.clear()
    ENQ.clear()


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def read_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


TMP = tempfile.mkdtemp(prefix="oflt_dict_test_")

# ===========================================================================
# 1. Core: существующий legacy dictionary.json (только чтение, файл не трогаем)
# ===========================================================================
LEGACY = os.path.join(ROOT, "dictionary.json")
snap = load_snapshot(LEGACY)
check("legacy_loaded", snap is not None)
check("legacy_20_pairs", len(snap) == 20, "pairs=%d" % len(snap))
check("legacy_pairs_unique",
      len({frozenset((a.lower(), b.lower())) for a, b in snap.pairs}) == 20)
check("legacy_no_mirror_rows",
      all((b, a) not in snap.pairs for a, b in snap.pairs), str(snap.pairs))
check("legacy_pair_workspace",
      ("workspace", "рабочее пространство") in snap.pairs)
check("legacy_en_lookup",
      snap.lookup("workspace") == "рабочее пространство")
check("legacy_ru_lookup",
      snap.lookup("рабочее пространство") == "workspace")
check("legacy_case_insensitive",
      snap.lookup("  WORKSPACE  ") == "рабочее пространство")

# ===========================================================================
# 2. Core: нормализация и ориентация пар
# ===========================================================================
core = os.path.join(TMP, "core.json")
write_json(core, {"Car": "Машина", "pull request": "пулл-реквест"})
snap = load_snapshot(core)
check("core_loaded", snap is not None)
check("core_en_lookup", snap.lookup("car") == "Машина")
check("core_value_case_preserved", snap.lookup("car") == "Машина")
check("core_ru_lookup", snap.lookup("МАШИНА") == "car")
check("core_pairs",
      snap.pairs == [("car", "Машина"), ("pull request", "пулл-реквест")],
      str(snap.pairs))

# RU-ключ в файле -> ориентация пары всё равно (en, ru)
write_json(core, {"машина": "car"})
snap = load_snapshot(core)
check("core_ru_key_oriented", snap.pairs == [("car", "машина")],
      str(snap.pairs))
check("core_ru_key_both_dirs",
      snap.lookup("car") == "машина" and snap.lookup("машина") == "car")

# Legacy-конфликт: last-wins по порядку файла + первая запись не теряется
write_json(core, {"car": "машина", "vehicle": "машина",
                  "bad": 42, "empty": "   "})
snap = load_snapshot(core)
check("legacy_conflict_last_wins",
      snap.lookup("машина") == "vehicle", str(snap.lookup("машина")))
check("legacy_conflict_first_kept", snap.lookup("car") == "машина")
check("legacy_invalid_skipped",
      snap.lookup("bad") is None and snap.lookup("empty") is None)

# Top-level не dict -> None
write_json(core, [1, 2, 3])
check("core_toplevel_list_none", load_snapshot(core) is None)

# Broken JSON -> None
with open(core, "w", encoding="utf-8") as f:
    f.write("{broken json")
check("core_broken_none", load_snapshot(core) is None)

# Файл отсутствует -> None
check("core_missing_none",
      load_snapshot(os.path.join(TMP, "no_such_file.json")) is None)

# serialize_pairs: совместимый flat-формат, зеркала пишутся
check("serialize_format",
      serialize_pairs([("car", "машина")]) ==
      {"car": "машина", "машина": "car"})

# ===========================================================================
# 3. DictionaryManager: путь, базовый CRUD
# ===========================================================================
check("default_path_dev",
      default_dictionary_path() == os.path.join(ROOT, "dictionary.json"),
      default_dictionary_path())

mpath = os.path.join(TMP, "manager.json")
m = DictionaryManager(mpath)
check("mgr_missing_file_empty", m.get_count() == 0 and m.load() is False)
check("mgr_add", m.add("car", "машина") is True)
check("mgr_count_1", m.get_count() == 1)
data = read_json(mpath)
check("mgr_file_flat_format",
      isinstance(data, dict) and set(data) == {"car", "машина"}, str(data))
check("mgr_file_mirror",
      data["car"] == "машина" and data["машина"] == "car")
check("mgr_get_all", m.get_all() == [("car", "машина")])
check("mgr_search_en", m.search("car") == [("car", "машина")])
check("mgr_search_ru", m.search("машин") == [("car", "машина")])
check("mgr_search_case", m.search("CAR") == [("car", "машина")])
check("mgr_search_empty", m.search("   ") == [])
check("mgr_search_no_hit", m.search("zzz") == [])

# Дубликат пары (другой регистр/пробелы) — no-op
check("mgr_add_duplicate", m.add("CAR", " машина ") is True)
check("mgr_duplicate_no_change", m.get_count() == 1)
# Зеркальная ориентация той же пары — не конфликт
check("mgr_mirror_add", m.add("машина", "car") is True)
check("mgr_mirror_no_change", m.get_count() == 1)
# Пустые данные -> False
check("mgr_add_empty",
      m.add("   ", "x") is False and m.add("y", "    ") is False)

# ===========================================================================
# 4. Политика 1:1 (O3)
# ===========================================================================
check("mgr_ru_conflict", m.add("vehicle", "машина") is False)
check("mgr_ru_conflict_found",
      m.find_conflict("vehicle", "машина") == ("car", "машина"))
check("mgr_en_conflict", m.add("car", "автомобиль") is False)
check("mgr_en_conflict_found",
      m.find_conflict("car", "автомобиль") == ("car", "машина"))
check("mgr_conflict_none", m.find_conflict("house", "дом") is None)
check("mgr_state_after_conflicts", m.get_all() == [("car", "машина")])
check("mgr_add_second", m.add("house", "дом") is True)
check("mgr_count_2", m.get_count() == 2)

# ===========================================================================
# 5. edit: логическая пара, зеркало обновляется автоматически
# ===========================================================================
check("mgr_edit", m.edit("car", "automobile", "машина") is True)
check("mgr_edit_applied",
      m.get_all() == [("automobile", "машина"), ("house", "дом")],
      str(m.get_all()))
data = read_json(mpath)
check("mgr_edit_file_updated",
      data.get("automobile") == "машина" and data.get("машина") == "automobile"
      and "car" not in data, str(data))
check("mgr_edit_missing_old", m.edit("nope", "x", "y") is False)
check("mgr_edit_ru_conflict", m.edit("house", "home", "машина") is False)
check("mgr_edit_en_conflict", m.edit("house", "automobile", "дом") is False)
# Свободный после удаления: «машина» снова можно занять
check("mgr_delete_frees", m.delete("automobile") is True)
check("mgr_add_after_free", m.add("train", "машина") is True)
check("mgr_pairs_after_free",
      m.get_all() == [("house", "дом"), ("train", "машина")],
      str(m.get_all()))

# ===========================================================================
# 6. delete: удаляется логическая пара вместе с зеркальной записью
# ===========================================================================
dpath = os.path.join(TMP, "delete.json")
write_json(dpath, {"dog": "собака", "собака": "dog", "cat": "кот"})
d = DictionaryManager(dpath)
check("mgr_load_pairs",
      d.get_all() == [("cat", "кот"), ("dog", "собака")], str(d.get_all()))
check("mgr_delete_pair", d.delete("dog") is True)
data = read_json(dpath)
check("mgr_delete_removes_mirror",
      "собака" not in data and "dog" not in data, str(data))
check("mgr_delete_rest", d.get_all() == [("cat", "кот")])
check("mgr_delete_again", d.delete("dog") is False)
# delete с явной парой (en, ru) в зеркальной ориентации
check("mgr_delete_oriented", d.delete("кот", "cat") is True)
check("mgr_delete_all", d.get_count() == 0)
check("mgr_delete_file_empty", read_json(dpath) == {})

# ===========================================================================
# 7. import/export
# ===========================================================================
ipath = os.path.join(TMP, "import_target.json")
im = DictionaryManager(ipath)
im.add("car", "машина")
src = os.path.join(TMP, "import_src.json")
write_json(src, {"house": "дом", " cat ": "кот", "car": "машина"})
added, errors = im.import_from_json(src)
check("import_added", added == 2, "added=%d errors=%d" % (added, errors))
check("import_no_errors", errors == 0, str(errors))
check("import_state",
      im.get_all() == sorted([("car", "машина"), ("house", "дом"),
                              ("cat", "кот")]), str(im.get_all()))
check("import_file",
      set(read_json(ipath)) ==
      {"car", "машина", "house", "дом", "cat", "кот"}, str(read_json(ipath)))

# Частично ошибочные записи: errors растут, валидные обрабатываются
src2 = os.path.join(TMP, "import_src2.json")
write_json(src2, {"house2": "дом2", "num": 42, "emptyval": "   ",
                  "": "x", "str2": None})
added, errors = im.import_from_json(src2)
check("import_partial", added == 1 and errors == 4,
      "added=%d errors=%d" % (added, errors))
check("import_partial_state", ("house2", "дом2") in im.get_all())
check("import_partial_file", "house2" in read_json(ipath))

# Конфликт 1:1 при импорте: запись не импортируется, ошибки +1
src4 = os.path.join(TMP, "import_src4.json")
write_json(src4, {"vehicle": "машина", "ok": "хорошо"})
added, errors = im.import_from_json(src4)
check("import_conflict", added == 1 and errors == 1,
      "added=%d errors=%d" % (added, errors))
check("import_conflict_state",
      ("vehicle", "машина") not in im.get_all()
      and ("ok", "хорошо") in im.get_all(), str(im.get_all()))

# RU-ключ в импортируемом файле -> ориентация (en, ru)
src5 = os.path.join(TMP, "import_src5.json")
write_json(src5, {"поезд": "train"})
added, errors = im.import_from_json(src5)
check("import_ru_oriented", added == 1 and errors == 0
      and ("train", "поезд") in im.get_all(), str(im.get_all()[-2:]))

# Top-level != dict -> импорт не применяется
src3 = os.path.join(TMP, "import_src3.json")
write_json(src3, [{"a": "b"}])
before = im.get_count()
added, errors = im.import_from_json(src3)
check("import_toplevel_list", added == 0 and errors == 1,
      "added=%d errors=%d" % (added, errors))
check("import_toplevel_state", im.get_count() == before)

# Broken JSON -> импорт не применяется
with open(src3, "w", encoding="utf-8") as f:
    f.write("{oops")
added, errors = im.import_from_json(src3)
check("import_broken", added == 0 and errors == 1,
      "added=%d errors=%d" % (added, errors))

# Экспорт: тот же flat-формат, содержимое совпадает
exp = os.path.join(TMP, "export.json")
check("export_ok", im.export_to_json(exp) is True)
check("export_roundtrip", read_json(exp) == read_json(ipath))

# ===========================================================================
# 8. reload: внешнее изменение файла становится видимым
# ===========================================================================
rpath = os.path.join(TMP, "reload.json")
rm = DictionaryManager(rpath)
rm.add("one", "один")
write_json(rpath, {"one": "один", "two": "два", "два": "two"})
check("reload_before", rm.get_count() == 1)
check("reload_ok", rm.reload() is True)
check("reload_after", rm.get_all() == [("one", "один"), ("two", "два")],
      str(rm.get_all()))
# Внешнее повреждение -> reload False, старое состояние сохранено
with open(rpath, "w", encoding="utf-8") as f:
    f.write("{{{")
check("reload_broken_keeps",
      rm.reload() is False and rm.get_count() == 2)
check("reload_error_recorded", rm.last_error is not None)

# ===========================================================================
# 9. Атомарный save: ошибка -> файл нетронут, память не меняется,
#    временных файлов не остаётся
# ===========================================================================
apath = os.path.join(TMP, "atomic.json")
am = DictionaryManager(apath)
am.add("first", "первый")
orig_data = read_json(apath)

orig_replace = os.replace


def boom(*a, **k):
    raise OSError("simulated replace failure")


os.replace = boom
try:
    check("save_fail_returns_false", am.add("second", "второй") is False)
finally:
    os.replace = orig_replace
check("save_fail_file_intact", read_json(apath) == orig_data,
      str(read_json(apath)))
check("save_fail_state_unchanged", am.get_all() == [("first", "первый")],
      str(am.get_all()))
check("save_fail_no_tmp_left",
      not [f for f in os.listdir(TMP) if f.startswith(".dictionary-")])
check("save_ok_after_fail", am.add("second", "второй") is True)
check("save_state_after_fail", am.get_count() == 2)

# mkstemp-невозможная директория (путь проходит через обычный файл)
blocker = os.path.join(TMP, "notadir.json")
with open(blocker, "w", encoding="utf-8") as f:
    f.write("{}")
bm = DictionaryManager(os.path.join(blocker, "m.json"))
check("mkstemp_fail", bm.add("a", "b") is False)
check("mkstemp_state_unchanged", bm.get_all() == [])

# Импорт при ошибке сохранения: память НЕ меняется (нет рассинхронизации)
ipath2 = os.path.join(TMP, "imp_fail.json")
ifm = DictionaryManager(ipath2)
ifm.add("keep", "сохранить")
srcf = os.path.join(TMP, "import_fail_src.json")
write_json(srcf, {"new": "новый"})
os.replace = boom
try:
    added, errors = ifm.import_from_json(srcf)
finally:
    os.replace = orig_replace
check("import_save_fail", added == 0 and errors == 1,
      "added=%d errors=%d" % (added, errors))
check("import_save_fail_state", ifm.get_all() == [("keep", "сохранить")])
check("import_save_fail_file",
      set(read_json(ipath2)) == {"keep", "сохранить"})

# ===========================================================================
# 10. OfflineTranslator: snapshot на каждый translate(), изменения видны
#     без перезапуска, match не вызывает нейросеть
# ===========================================================================
t = OfflineTranslator(
    cache_dir=os.path.join(tempfile.gettempdir(), "oflt_dict_test_model_cache")
)
t_dict = os.path.join(TMP, "translator_dict.json")
t.dictionary_path = t_dict  # изоляция: не трогаем словарь проекта
write_json(t_dict, {"workspace": "рабочее пространство"})

reset()
r = t.translate("workspace", "en-ru")
check("t_dict_en_ru", r == "рабочее пространство", r)
check("t_dict_no_infer", len(CALLS) == 0, str(CALLS))
reset()
r = t.translate("рабочее пространство", "ru-en")
check("t_dict_ru_en", r == "workspace", r)
check("t_dict_ru_en_no_infer", len(CALLS) == 0, str(CALLS))
check("t_dict_case", t.translate("WORKSPACE", "en-ru") == "рабочее пространство")
check("t_empty_text", t.translate("   ", "en-ru") == "")

# Ключевой сценарий: изменение через DictionaryManager видно БЕЗ
# создания нового OfflineTranslator
dmgr = DictionaryManager(t_dict)
check("t_mgr_loads",
      dmgr.get_all() == [("workspace", "рабочее пространство")],
      str(dmgr.get_all()))
dmgr.delete("workspace")
dmgr.add("workspace", "рабочая область")
r = t.translate("workspace", "en-ru")
check("t_sees_change_no_restart", r == "рабочая область", r)
check("t_no_infer_after_change", len(CALLS) == 0)
check("t_reverse_new_value",
      t.translate("рабочая область", "ru-en") == "workspace")

# Нет match -> нейросетевая модель
reset()
r = t.translate("some random text", "en-ru")
check("t_nn_fallback",
      len(CALLS) == 1 and r == "⟪some random text⟫", (len(CALLS), r))

# Multi-chunk текст: snapshot читается ровно ОДИН раз за translate()
orig_load = translator.load_snapshot
load_calls = []


def spy(path):
    load_calls.append(path)
    return orig_load(path)


translator.load_snapshot = spy
reset()
try:
    long_text = " ".join("w%d" % i for i in range(2000))
    r = t.translate(long_text, "en-ru")
finally:
    translator.load_snapshot = orig_load
check("t_one_snapshot_per_translate", len(load_calls) == 1, str(load_calls))
check("t_snapshot_path", load_calls[0] == t_dict, str(load_calls))
check("t_multi_chunk_ok", r.count("⟪") >= 2, str(r[:80]))
check("t_multi_chunk_complete",
      len(CALLS) >= 2 and "".join(c for _, c in CALLS).replace(" ", "")
      == long_text.replace(" ", ""), str(len(CALLS)))

# Отсутствие/повреждение dictionary.json -> graceful fallback на модель
t.dictionary_path = os.path.join(TMP, "no_such_dict.json")
reset()
r = t.translate("hello world", "en-ru")
check("t_missing_dict_graceful",
      len(CALLS) == 1 and r == "⟪hello world⟫", (len(CALLS), r))
bad = os.path.join(TMP, "bad_dict.json")
with open(bad, "w", encoding="utf-8") as f:
    f.write("{broken")
t.dictionary_path = bad
reset()
r = t.translate("hello world", "en-ru")
check("t_broken_dict_graceful", len(CALLS) == 1, str(CALLS))
t.dictionary_path = t_dict
check("t_back_to_dict", t.translate("workspace", "en-ru") == "рабочая область")

print("OK: %d checks passed" % len(PASS))