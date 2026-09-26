# -*- coding: utf-8 -*-
"""Этап 8: тесты реестра моделей (ModelDescriptor/ModelRegistry/
ModelManager).

Запуск (без реальных моделей, без сети, без pytest):

    python tests/test_model_registry.py

Покрытие:
- ModelDescriptor: создание, обязательные поля, валидация направлений,
  неизменяемость (frozen), «только данные» (без runtime-объектов);
- ModelRegistry: register/get/list, дубликат id, неизвестный id,
  фильтрация по backend/направлению, детерминированность;
- ModelManager: list, get, default-модель, совместимость направления,
  доступность (Marian — фейковый HF-кэш, GGUF — env OFFLINE_TRANSLATOR_GGUF),
  resolve(), отсутствие side effects (каталоги не создаёт);
- backward compatibility: OfflineTranslator() — по умолчанию Marian
  (без загрузки реальной модели — фейковый бэкенд + фасадная проводка
  llama_cpp);
- lazy loading: `import model_registry`, перечисление моделей и
  get_available_models() НЕ импортируют torch/transformers/llama_cpp
  и не создают файлов/каталогов (чистый subprocess).
"""
import dataclasses
import inspect
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:300]))
    PASS.append(name)


TMP = tempfile.mkdtemp(prefix="oflt_model_registry_test_")

# Доступность GGUF не должна зависеть от окружения машины — env изолируем.
GGUF_ENV = "OFFLINE_TRANSLATOR_GGUF"
_saved_gguf_env = os.environ.pop(GGUF_ENV, None)

from dictionary_manager import model_cache_dir  # noqa: E402
from model_registry import (  # noqa: E402
    GGUF_ENV_VAR,
    ModelDescriptor,
    ModelManager,
    ModelNotFoundError,
    ModelRegistry,
    ModelUnavailableError,
    default_registry,
)

check("env_var_name", GGUF_ENV_VAR == GGUF_ENV)

# ---------------------------------------------------------------------
# 1. ModelDescriptor
# ---------------------------------------------------------------------
d = ModelDescriptor(id="m1", name="M1", backend="marian",
                    directions=("en-ru",), source="HF: x/y",
                    hf_model_id="x/y")
check("desc_create",
      d.id == "m1" and d.name == "M1" and d.source == "HF: x/y")
check("desc_backend", d.backend == "marian")
check("desc_directions",
      d.directions == ("en-ru",) and d.supports_direction("en-ru")
      and not d.supports_direction("ru-en"))
check("desc_optional_defaults",
      d.hf_model_id == "x/y" and d.gguf_model is None)


def _expect(desc_kwargs, exc, name):
    try:
        ModelDescriptor(**desc_kwargs)
    except Exception as e:
        check(name, isinstance(e, exc), "получено %r" % (e,))
    else:
        check(name, False, "ожидалось %s" % exc.__name__)


_expect(dict(id="", name="n", backend="b", directions=("en-ru",),
             source="s"), ValueError, "desc_empty_id")
_expect(dict(id="i", name="  ", backend="b", directions=("en-ru",),
             source="s"), ValueError, "desc_empty_name")
_expect(dict(id="i", name="n", backend="", directions=("en-ru",),
             source="s"), ValueError, "desc_empty_backend")
_expect(dict(id="i", name="n", backend="b", directions=("en-ru",)),
        TypeError, "desc_missing_source")
_expect(dict(id="i", name="n", backend="b", directions=("fr-de",),
             source="s"), ValueError, "desc_bad_direction")
_expect(dict(id="i", name="n", backend="b", directions=(),
             source="s"), ValueError, "desc_empty_directions")
_expect(dict(id="i", name="n", backend="b", directions=("en-ru", "en-ru"),
             source="s"), ValueError, "desc_duplicate_directions")
_expect(dict(id="i", name="n", backend="b", directions="en-ru",
             source="s"), ValueError, "desc_directions_not_tuple")

# Неизменяемость: frozen dataclass
try:
    d.id = "other"
    check("desc_frozen", False)
except dataclasses.FrozenInstanceError:
    check("desc_frozen", True)

# Лёгкий объект данных: только str/tuple/None, без runtime-атрибутов
check("desc_data_only",
      all(isinstance(getattr(d, f), (str, tuple, type(None)))
          for f in ModelDescriptor.__dataclass_fields__),
      str(sorted(ModelDescriptor.__dataclass_fields__)))
check("desc_no_runtime_attrs",
      not any(hasattr(d, a) for a in
              ("llm", "model", "tokenizer", "service", "tk")))
# ---------------------------------------------------------------------
# 2. ModelRegistry
# ---------------------------------------------------------------------
reg = ModelRegistry()
a = ModelDescriptor(id="a", name="A", backend="marian",
                    directions=("en-ru",), source="s1")
b = ModelDescriptor(id="b", name="B", backend="llama_cpp",
                    directions=("en-ru", "ru-en"), source="s2")
c = ModelDescriptor(id="c", name="C", backend="marian",
                    directions=("ru-en",), source="s3")
for x in (a, b, c):
    reg.register(x)
check("reg_list_order", [x.id for x in reg.list()] == ["a", "b", "c"])
check("reg_get", reg.get("b") is b)
check("reg_contains", ("a" in reg) and ("z" not in reg))
check("reg_len", len(reg) == 3)
check("reg_iter", {x.id for x in reg} == {"a", "b", "c"})

try:
    reg.register(ModelDescriptor(id="a", name="A2", backend="marian",
                                 directions=("en-ru",), source="s"))
    check("reg_duplicate_id", False)
except ValueError:
    check("reg_duplicate_id", True)
check("reg_duplicate_keeps_original", reg.get("a").name == "A")

try:
    reg.get("nope")
    check("reg_unknown_id", False)
except ModelNotFoundError as e:
    check("reg_unknown_id", "nope" in str(e))
try:
    reg.get("nope")
    check("reg_unknown_is_keyerror", False)
except KeyError:
    check("reg_unknown_is_keyerror", True)

try:
    reg.register("not-a-descriptor")
    check("reg_type_check", False)
except TypeError:
    check("reg_type_check", True)

check("reg_by_backend",
      [x.id for x in reg.find_by_backend("marian")] == ["a", "c"])
check("reg_by_backend_empty", reg.find_by_backend("nada") == [])
check("reg_by_direction",
      [x.id for x in reg.find_by_direction("en-ru")] == ["a", "b"])
try:
    reg.find_by_direction("fr-de")
    check("reg_by_direction_bad", False)
except ValueError:
    check("reg_by_direction_bad", True)

# Детерминированность: одинаковая конфигурация -> одинаковый список
check("reg_deterministic",
      [x.id for x in default_registry().list()]
      == [x.id for x in default_registry().list()])

# ---------------------------------------------------------------------
# 3. default_registry(): фактическая архитектура проекта
# ---------------------------------------------------------------------
dreg = default_registry()
ids = [x.id for x in dreg.list()]
check("default_ids",
      ids == ["marian-en-ru", "marian-ru-en", "hy-mt2-1.8b"], str(ids))
men = dreg.get("marian-en-ru")
check("default_marian_en",
      men.backend == "marian" and men.directions == ("en-ru",)
      and men.hf_model_id == "Helsinki-NLP/opus-mt-en-ru")
# Marian — ДВА direction-specific артефакта (как их реально грузит
# MarianBackend): en-ru не поддерживает ru-en, и наоборот.
check("default_marian_two_artifacts",
      dreg.get("marian-ru-en").hf_model_id == "Helsinki-NLP/opus-mt-ru-en"
      and dreg.get("marian-ru-en").directions == ("ru-en",)
      and not men.supports_direction("ru-en"))
hy = dreg.get("hy-mt2-1.8b")
check("default_gguf",
      hy.backend == "llama_cpp"
      and hy.directions == ("en-ru", "ru-en")
      and hy.gguf_model == "Hy-MT2-1.8B" and hy.hf_model_id is None)
# Machine-specific абсолютные пути в описаниях запрещены
check("default_no_machine_paths",
      all(("/mnt/" not in x.source) and ("C:\\\\" not in x.source)
          and ("C:/" not in x.source) for x in dreg.list()))
# Бэкенд-идентификаторы совпадают с фасадом
# OfflineTranslator(backend=...)
check("default_backend_ids",
      {x.backend for x in dreg.list()} == {"marian", "llama_cpp"})
# ---------------------------------------------------------------------
# 4. ModelManager: перечисление, default, доступность, resolve
# ---------------------------------------------------------------------
def make_hf_cache(cache_dir, repo_id):
    """Минимальный фейк HF-кэша (v2-layout): <snap>/config.json."""
    snap = os.path.join(cache_dir, "models--" + repo_id.replace("/", "--"),
                        "snapshots", "deadbeef")
    os.makedirs(snap)
    with open(os.path.join(snap, "config.json"), "w") as f:
        f.write("{}")


mgr = ModelManager(cache_dir=TMP)  # чистая, пустая кэш-директория
check("mgr_all_models", [x.id for x in mgr.list_models()] == ids)
check("mgr_get_model", mgr.get_model("hy-mt2-1.8b") == hy)
try:
    mgr.get_model("nope")
    check("mgr_unknown_id", False)
except ModelNotFoundError:
    check("mgr_unknown_id", True)
check("mgr_default_cache_dir",
      ModelManager(cache_dir="custom").cache_dir == "custom"
      and ModelManager().cache_dir == model_cache_dir())
check("mgr_no_side_effects_on_init",
      not os.path.exists(os.path.join(TMP, "never_created"))
      and ModelManager(cache_dir=os.path.join(TMP, "never_created2"))
      is not None and not os.path.exists(
          os.path.join(TMP, "never_created2")))
check("mgr_default_en", mgr.get_default_model("en-ru").id == "marian-en-ru")
check("mgr_default_ru", mgr.get_default_model("ru-en").id == "marian-ru-en")
try:
    mgr.get_default_model("fr-de")
    check("mgr_default_bad_dir", False)
except ValueError:
    check("mgr_default_bad_dir", True)

# --- Marian: доступность = наличие в локальном HF-кэше (per direction)
check("mgr_marian_unavailable_empty",
      not mgr.is_model_available("marian-en-ru")
      and not mgr.is_model_available("marian-ru-en"))
make_hf_cache(TMP, "Helsinki-NLP/opus-mt-en-ru")
check("mgr_marian_available_per_direction",
      mgr.is_model_available("marian-en-ru")
      and not mgr.is_model_available("marian-ru-en"),
      "доступность должна различаться по направлениям")

# --- GGUF: доступность = файл из OFFLINE_TRANSLATOR_GGUF
check("mgr_gguf_unavailable_no_env",
      not mgr.is_model_available("hy-mt2-1.8b"))
gguf_file = os.path.join(TMP, "Hy-MT2-1.8B-Q4_K_M.gguf")
with open(gguf_file, "wb") as f:
    f.write(b"GGUF")
os.environ[GGUF_ENV] = gguf_file
check("mgr_gguf_available", mgr.is_model_available("hy-mt2-1.8b"))
check("mgr_available_models",
      [x.id for x in mgr.get_available_models()]
      == ["marian-en-ru", "hy-mt2-1.8b"])
# default-модель не зависит от доступности (приоритет = порядок реестра)
check("mgr_default_still_marian",
      mgr.get_default_model("en-ru").id == "marian-en-ru")

# --- resolve()
check("mgr_resolve_by_id",
      mgr.resolve(model_id="hy-mt2-1.8b").id == "hy-mt2-1.8b")
check("mgr_resolve_by_direction",
      mgr.resolve(direction="en-ru").id == "marian-en-ru")
check("mgr_resolve_both_args",
      mgr.resolve(model_id="marian-en-ru", direction="en-ru").id
      == "marian-en-ru")
os.environ.pop(GGUF_ENV)
try:
    mgr.resolve(model_id="hy-mt2-1.8b")
    check("mgr_resolve_unavailable", False)
except ModelUnavailableError:
    check("mgr_resolve_unavailable", True)
check("mgr_resolve_require_available_false",
      mgr.resolve(model_id="hy-mt2-1.8b",
                  require_available=False).id == "hy-mt2-1.8b")
try:
    mgr.resolve()
    check("mgr_resolve_no_args", False)
except ValueError:
    check("mgr_resolve_no_args", True)
try:
    mgr.resolve(model_id="marian-en-ru", direction="ru-en")
    check("mgr_resolve_dir_mismatch", False)
except ValueError:
    check("mgr_resolve_dir_mismatch", True)
try:
    mgr.resolve(model_id="nope")
    check("mgr_resolve_unknown", False)
except ModelNotFoundError:
    check("mgr_resolve_unknown", True)
os.environ[GGUF_ENV] = gguf_file

# --- GGUF: edge-кейсы env
os.environ[GGUF_ENV] = os.path.join(TMP, "missing.gguf")
check("mgr_gguf_missing_file", not mgr.is_model_available("hy-mt2-1.8b"))
other_gguf = os.path.join(TMP, "other-model.gguf")
with open(other_gguf, "wb") as f:
    f.write(b"GGUF")
os.environ[GGUF_ENV] = other_gguf
check("mgr_gguf_wrong_model_name",
      not mgr.is_model_available("hy-mt2-1.8b"))
os.environ[GGUF_ENV] = TMP  # каталог, а не файл
check("mgr_gguf_dir_not_file", not mgr.is_model_available("hy-mt2-1.8b"))
os.environ.pop(GGUF_ENV)

# --- Свой (не стандартный) реестр
custom = ModelRegistry()
custom.register(hy)
mgr2 = ModelManager(registry=custom, cache_dir=TMP)
check("mgr_custom_registry",
      [x.id for x in mgr2.list_models()] == ["hy-mt2-1.8b"])
check("mgr2_default_en_is_gguf",
      mgr2.get_default_model("en-ru").id == "hy-mt2-1.8b")
# ---------------------------------------------------------------------
# 5. Backward compatibility: OfflineTranslator() — по умолчанию Marian
# ---------------------------------------------------------------------
# Реальная модель НЕ загружается: (a) дефолтный конструктор — через
# подмену класса бэкенда (тестовый шов: фасад обращается к глобальному
# имени translator.MarianBackend); (b) llama_cpp — проверка пути фасадом
# происходит ДО load().
try:
    import translator
except Exception as _import_err:  # torch не установлен — проверяем skip
    translator = None
    check("skip_translator_checks", True, str(_import_err))

if translator is not None:
    class _FakeMarian:
        name = "marian"

        def __init__(self, cache_dir=None):
            self.cache_manager = None
            self.device = "cpu"
            self.max_source_tokens = 480

        def load(self):
            pass

    _orig_marian = translator.MarianBackend
    translator.MarianBackend = _FakeMarian
    try:
        facade = translator.OfflineTranslator()  # БЕЗ параметров — дефолт
    finally:
        translator.MarianBackend = _orig_marian
    check("facade_default_marian", isinstance(facade.backend, _FakeMarian))
    check("facade_default_attrs",
          facade.device == "cpu"
          and facade.max_source_tokens == 480
          and facade.cache_manager is None)
    _sig = inspect.signature(translator.OfflineTranslator.__init__)
    check("facade_signature_unchanged",
          _sig.parameters["backend"].default == "marian"
          and _sig.parameters["cache_dir"].default is None
          and _sig.parameters["gguf_path"].default is None)

    # Проводка llama_cpp: отсутствующий файл — FileNotFoundError
    # (до load(); модель не загружается)
    try:
        translator.OfflineTranslator(
            backend="llama_cpp",
            gguf_path=os.path.join(TMP, "no_such_model.gguf"))
        check("facade_llama_cpp_missing_file", False)
    except FileNotFoundError:
        check("facade_llama_cpp_missing_file", True)

    # Неизвестный backend — ValueError (существующий контракт)
    try:
        translator.OfflineTranslator(backend="nada")
        check("facade_unknown_backend", False)
    except ValueError:
        check("facade_unknown_backend", True)

# ---------------------------------------------------------------------
# 6. Lazy loading: без тяжёлых импортов и без side effects
#    (чистый subprocess — основной процесс тестов мог уже что-то импортировать)
# ---------------------------------------------------------------------
_LAZY_CODE = '''
import os, shutil, sys, tempfile
sys.path.insert(0, __ROOT__)
os.environ.pop("OFFLINE_TRANSLATOR_GGUF", None)
work = tempfile.mkdtemp(prefix="oflt_lazy_")
try:
    os.chdir(work)
    before = set(os.listdir(work))
    import model_registry as m
    heavy = ("torch", "transformers", "llama_cpp", "backends",
             "translator", "tkinter", "customtkinter", "benchmarks")
    leaked = [n for n in heavy if n in sys.modules]
    assert not leaked, "model_registry импортировал тяжёлые модули: %s" % leaked
    ids = [d.id for d in m.default_registry().list()]
    assert ids == ["marian-en-ru", "marian-ru-en", "hy-mt2-1.8b"], ids
    mgr = m.ModelManager(cache_dir=os.path.join(work, "never"))
    assert [d.id for d in mgr.get_available_models()] == []
    assert mgr.get_default_model("en-ru").id == "marian-en-ru"
    assert not os.path.exists(os.path.join(work, "never"))
    assert before == set(os.listdir(work)), "появились файлы/каталоги"
finally:
    os.chdir(tempfile.gettempdir())
    shutil.rmtree(work, ignore_errors=True)
print("LAZY-OK")
'''
_proc = subprocess.run(
    [sys.executable, "-c",
     _LAZY_CODE.replace("__ROOT__", repr(ROOT))],
    capture_output=True, text=True, timeout=180)
check("lazy_no_heavy_imports_no_side_effects",
      _proc.returncode == 0 and "LAZY-OK" in _proc.stdout,
      (_proc.stderr or _proc.stdout)[-500:])

# ---------------------------------------------------------------------
# Итог
# ---------------------------------------------------------------------
if _saved_gguf_env is not None:
    os.environ[GGUF_ENV] = _saved_gguf_env
else:
    os.environ.pop(GGUF_ENV, None)

print()
print("=" * 60)
print("test_model_registry: ПРОШЛО ПРОВЕРОК: %d" % len(PASS))
print("Список проверок:")
for name in PASS:
    print("  ✓", name)