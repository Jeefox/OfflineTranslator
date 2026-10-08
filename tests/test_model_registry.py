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
- Этап 9: model_id в OfflineTranslator (конфликт model_id/backend,
  неизвестный id, доступность и понятные ошибки без скачивания,
  direction mismatch, GGUF-путь gguf_path/env, lazy loading);
- lazy loading: `import model_registry`, перечисление моделей и
  get_available_models() НЕ импортируют torch/transformers/llama_cpp
  и не создают файлов/каталогов (чистый subprocess).
"""
import dataclasses
import inspect
import os
import subprocess
import sys


def expect_translation_error(function, *args):
    try:
        function(*args)
    except (RuntimeError, ValueError) as exc:
        return "Ошибка перевода: " + str(exc)
    raise AssertionError("Translation must raise on failure")

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

        def __init__(self, cache_dir=None, directions=None):
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
    # Этап 9: default-выбор — legacy-путь, model_id не задан
    check("facade_default_model_id_attr", facade.model_id is None)
    # Явный legacy backend="marian" — поведение не изменилось
    translator.MarianBackend = _FakeMarian
    try:
        facade_m = translator.OfflineTranslator(backend="marian")
    finally:
        translator.MarianBackend = _orig_marian
    check("facade_legacy_backend_marian",
          isinstance(facade_m.backend, _FakeMarian)
          and facade_m.model_id is None)
    # Этап 9: сигнатура расширена параметром model_id; дефолт backend —
    # None (== 'marian': поведение не изменилось; None позволяет
    # отличить «backend не указан» для правила конфликта
    # model_id/backend).
    _sig = inspect.signature(translator.OfflineTranslator.__init__)
    check("facade_signature_stage9",
          list(_sig.parameters) == ["self", "cache_dir", "backend",
                                    "gguf_path", "model_id", "auto_load"]
          and _sig.parameters["cache_dir"].default is None
          and _sig.parameters["backend"].default is None
          and _sig.parameters["gguf_path"].default is None
          and _sig.parameters["model_id"].default is None)

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
# 6. Этап 9: model_id в OfflineTranslator
# ---------------------------------------------------------------------
# Выбор — через ModelManager (дескриптор + доступность, без загрузки
# моделей); создание бэкенда — в фасаде. Бэкенды — фейки через тестовые
# швы: translator.MarianBackend (глобальное имя фасада) и
# backends.llama_cpp._default_llama_factory (тот же приём, что в
# test_llama_cpp_backend.py). Без реальных моделей, без сети.
if translator is not None:
    import backends.llama_cpp as _lc  # лёгкий модуль: без импорта llama_cpp

    class _FakeMarian9:
        """Фейк Marian-бэкенда с полным контрактом TranslationBackend
        (запоминает args конструктора; translate_chunk — эхо)."""
        name = "marian"
        instances = []

        def __init__(self, cache_dir=None, directions=None):
            self.cache_dir = cache_dir
            self.cache_manager = None
            self.device = "cpu"
            self.max_source_tokens = 480
            _FakeMarian9.instances.append(self)

        def load(self):
            pass

        def split_sentence(self, sentence, direction):
            return [sentence]

        def translate_chunk(self, chunk, direction):
            return "⟪%s⟫" % chunk

    class _FakeLlama9:
        """Минимальный фейк llama_cpp.Llama
        (tokenize/create_completion — как использует LlamaCppBackend)."""

        def __init__(self, model_path, n_ctx, verbose=False,
                     response="⟪llama⟫"):
            self.model_path = model_path
            self.n_ctx = n_ctx
            self.response = response

        def tokenize(self, text, add_bos=True, special=False):
            if isinstance(text, bytes):
                text = text.decode("utf-8")
            return list(range(1, len(text.split()) + 1))

        def create_completion(self, prompt=None, max_tokens=None,
                              stop=None, **kw):
            return {"choices": [{"text": self.response}]}

    TMP9 = os.path.join(TMP, "stage9")
    os.makedirs(TMP9, exist_ok=True)

    # --- model_id и backend одновременно — явный ValueError (конфликт) ---
    for _mid, _be in (("marian-en-ru", "marian"),
                      ("hy-mt2-1.8b", "llama_cpp"),
                      ("marian-ru-en", "marian")):
        try:
            translator.OfflineTranslator(model_id=_mid, backend=_be)
            check("facade9_conflict_%s_%s" % (_mid, _be), False)
        except ValueError as e:
            check("facade9_conflict_%s_%s" % (_mid, _be),
                  "model_id" in str(e) and "backend" in str(e), str(e))

    # Неизвестный model id — ModelNotFoundError (реестр; список known id)
    try:
        translator.OfflineTranslator(model_id="does-not-exist")
        check("facade9_unknown_model", False)
    except ModelNotFoundError as e:
        check("facade9_unknown_model",
              "does-not-exist" in str(e)
              and "marian-en-ru" in str(e)
              and "hy-mt2-1.8b" in str(e), str(e))

    # gguf_path для не-GGUF-модели — явный ValueError
    try:
        translator.OfflineTranslator(model_id="marian-en-ru",
                                     gguf_path="x.gguf")
        check("facade9_gguf_path_on_marian", False)
    except ValueError as e:
        check("facade9_gguf_path_on_marian", "gguf_path" in str(e), str(e))


    # GGUF должен по-прежнему отбрасываться до создания backend, а Marian
    # можно создавать и при пустом кэше: backend сам выполнит загрузку из
    # встроенного кэша или HuggingFace.
    class _BombBackend:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "бэкенд не должен создаваться при ошибке разрешения")

    _orig_marian9 = translator.MarianBackend
    _orig_llama9 = translator.LlamaCppBackend
    _saved_env9 = os.environ.pop(GGUF_ENV, None)
    translator.MarianBackend = _FakeMarian9
    translator.LlamaCppBackend = _BombBackend
    empty_cache9 = os.path.join(TMP9, "empty_cache")
    os.makedirs(empty_cache9, exist_ok=True)
    try:
        # неизвестный id — без создания бэкенда
        try:
            translator.OfflineTranslator(model_id="nope")
            check("facade9_unknown_no_backend", False)
        except ModelNotFoundError:
            check("facade9_unknown_no_backend", True)
        # Marian-модель не в кэше — backend создаётся для локальной
        # загрузки/первоначального скачивания.
        _FakeMarian9.instances.clear()
        facade_empty = translator.OfflineTranslator(
            model_id="marian-en-ru", cache_dir=empty_cache9)
        check("facade9_marian_empty_cache_allowed",
              isinstance(facade_empty.backend, _FakeMarian9)
              and len(_FakeMarian9.instances) == 1)
        # GGUF без env и без gguf_path — LlamaCppBackend не создаётся
        try:
            translator.OfflineTranslator(model_id="hy-mt2-1.8b")
            check("facade9_gguf_unavailable_no_backend", False)
        except ModelUnavailableError:
            check("facade9_gguf_unavailable_no_backend", True)
    finally:
        translator.MarianBackend = _orig_marian9
        translator.LlamaCppBackend = _orig_llama9
        if _saved_env9 is not None:
            os.environ[GGUF_ENV] = _saved_env9
        else:
            os.environ.pop(GGUF_ENV, None)

    # Marian без локального кэша не является ошибкой разрешения: загрузка
    # выполняется самим backend (из бандля, кэша или сети).
    _FakeMarian9.instances.clear()
    translator.MarianBackend = _FakeMarian9
    try:
        facade_empty = translator.OfflineTranslator(
            model_id="marian-en-ru", cache_dir=empty_cache9)
    finally:
        translator.MarianBackend = _orig_marian9
    check("facade9_marian_empty_cache_msg_removed",
          isinstance(facade_empty.backend, _FakeMarian9))
    os.environ.pop(GGUF_ENV, None)
    try:
        translator.OfflineTranslator(model_id="hy-mt2-1.8b")
        check("facade9_gguf_unavailable_msg", False)
    except ModelUnavailableError as e:
        msg = str(e)
        check("facade9_gguf_unavailable_msg",
              "hy-mt2-1.8b" in msg
              and "OFFLINE_TRANSLATOR_GGUF" in msg
              and "gguf_path" in msg
              and "не скачивается" in msg, msg)


    # --- model_id="marian-en-ru" → Marian-бэкенд, только направление
    #     en-ru (direction mismatch — явная ошибка, а не тихий перевод) ---
    cache9 = os.path.join(TMP9, "hf_cache")
    make_hf_cache(cache9, "Helsinki-NLP/opus-mt-en-ru")
    _FakeMarian9.instances.clear()
    translator.MarianBackend = _FakeMarian9
    try:
        t9 = translator.OfflineTranslator(
            model_id="marian-en-ru", cache_dir=cache9)
        check("facade9_marian_backend",
              isinstance(t9.backend, _FakeMarian9), str(type(t9.backend)))
        check("facade9_marian_model_id_attr", t9.model_id == "marian-en-ru")
        check("facade9_marian_cache_dir",
              _FakeMarian9.instances[-1].cache_dir == cache9,
              str(_FakeMarian9.instances[-1].cache_dir))
        check("facade9_marian_attrs",
              t9.max_source_tokens == 480
              and t9.device == "cpu"
              and t9.cache_manager is None)
        # Пайплайн: translate/translate_stream работают (словарь изолирован)
        t9.dictionary_path = os.path.join(TMP9, "no_dict.json")
        r = t9.translate("Hello world.", "en-ru")
        check("facade9_marian_translate", r == "⟪Hello world.⟫", r)
        ev = []
        fin = t9.translate_stream(
            "Alpha one. Beta two.", "en-ru",
            on_sentence=lambda ph, d, t, u: ev.append(ph))
        check("facade9_marian_stream",
              fin == "⟪Alpha one.⟫ ⟪Beta two.⟫"
              and ev == ["start", "done", "start", "done"], (fin, str(ev)))
        # Direction mismatch: translate() — «Ошибка перевода: ...»
        # (Этап 14: пользовательский текст БЕЗ внутренних идентификаторов —
        # model_id/направление уходят в лог, а не в статус GUI)
        r = expect_translation_error(t9.translate, "Привет, мир.", "ru-en")
        check("facade9_marian_dir_mismatch_translate",
              r.startswith("Ошибка перевода:")
              and "не поддерживает выбранное направление" in r
              and "marian-en-ru" not in r
              and "ru-en" not in r, r)
        # translate_stream() — исключение (контракт: stream не глотает);
        # Этап 14: текст исключения так же санитизирован
        try:
            t9.translate_stream("Привет, мир.", "ru-en")
            check("facade9_marian_dir_mismatch_stream", False)
        except ValueError as e:
            check("facade9_marian_dir_mismatch_stream",
                  "не поддерживает выбранное направление" in str(e)
                  and "marian-en-ru" not in str(e), str(e))
    finally:
        translator.MarianBackend = _orig_marian9

    # --- model_id="marian-ru-en" → только направление ru-en ---
    cache9b = os.path.join(TMP9, "hf_cache_ru")
    make_hf_cache(cache9b, "Helsinki-NLP/opus-mt-ru-en")
    translator.MarianBackend = _FakeMarian9
    try:
        t9b = translator.OfflineTranslator(
            model_id="marian-ru-en", cache_dir=cache9b)
        t9b.dictionary_path = os.path.join(TMP9, "no_dict.json")
        check("facade9_marian_ru_translate",
              t9b.translate("Привет, мир.", "ru-en") == "⟪Привет, мир.⟫")
        r = expect_translation_error(t9b.translate, "Hello world.", "en-ru")
        check("facade9_marian_ru_dir_mismatch",
              r.startswith("Ошибка перевода:")
              and "не поддерживает выбранное направление" in r
              and "marian-ru-en" not in r
              and "en-ru" not in r, r)
    finally:
        translator.MarianBackend = _orig_marian9


    # --- model_id="hy-mt2-1.8b" → llama_cpp-бэкенд, без реальной загрузки ---
    gguf9 = os.path.join(TMP9, "fake-Hy-MT2-1.8B-Q4_K_M.gguf")
    with open(gguf9, "wb") as f:
        f.write(b"GGUF")
    _factory_holder = {
        "f": lambda mp, n_ctx, verbose=False: _FakeLlama9(mp, n_ctx, verbose)}
    _orig_factory = _lc._default_llama_factory
    _llama_in_sys_before = "llama_cpp" in sys.modules
    _lc._default_llama_factory = (
        lambda mp, n_ctx, verbose=False, _h=_factory_holder:
            _h["f"](mp, n_ctx, verbose))
    try:
        # Явный gguf_path (существующий контракт: файл должен существовать)
        t10 = translator.OfflineTranslator(
            model_id="hy-mt2-1.8b", gguf_path=gguf9)
        check("facade9_gguf_backend",
              isinstance(t10.backend, translator.LlamaCppBackend),
              str(type(t10.backend)))
        check("facade9_gguf_model_id_attr", t10.model_id == "hy-mt2-1.8b")
        check("facade9_gguf_path_used", t10.backend.model_path == gguf9,
              t10.backend.model_path)
        # Lazy loading: разрешение + конструирование не импортируют
        # llama_cpp раньше существующего момента load()
        check("facade9_gguf_no_llama_cpp_import",
              ("llama_cpp" in sys.modules) == _llama_in_sys_before)
        check("facade9_gguf_attrs",
              t10.device == "cpu"
              and t10.max_source_tokens == 4096 - 1024 - 128 - 32
              and t10.cache_manager is None,
              str((t10.device, t10.max_source_tokens)))
        # Оба направления модели допустимы — ограничений нет
        t10.dictionary_path = os.path.join(TMP9, "no_dict.json")
        check("facade9_gguf_translate_en",
              t10.translate("Hello world.", "en-ru") == "⟪llama⟫")
        check("facade9_gguf_translate_ru",
              t10.translate("Привет, мир.", "ru-en") == "⟪llama⟫")
        ev = []
        fin = t10.translate_stream(
            "Alpha one. Beta two.", "en-ru",
            on_sentence=lambda ph, d, t, u: ev.append(ph))
        check("facade9_gguf_stream",
              fin == "⟪llama⟫ ⟪llama⟫"
              and ev == ["start", "done", "start", "done"], (fin, str(ev)))
        # Явный gguf-файл отсутствует — ModelUnavailableError (не молча)
        try:
            translator.OfflineTranslator(
                model_id="hy-mt2-1.8b",
                gguf_path=os.path.join(TMP9, "missing.gguf"))
            check("facade9_gguf_missing_path", False)
        except ModelUnavailableError as e:
            check("facade9_gguf_missing_path",
                  "missing.gguf" in str(e) and "hy-mt2-1.8b" in str(e),
                  str(e))
    finally:
        _lc._default_llama_factory = _orig_factory
        os.environ.pop(GGUF_ENV, None)

    # --- gguf-путь из env OFFLINE_TRANSLATOR_GGUF (механизм Этапа 6) ---
    _lc._default_llama_factory = (
        lambda mp, n_ctx, verbose=False, _h=_factory_holder:
            _h["f"](mp, n_ctx, verbose))
    os.environ[GGUF_ENV] = gguf9
    try:
        t11 = translator.OfflineTranslator(model_id="hy-mt2-1.8b")
        check("facade9_gguf_env_path", t11.backend.model_path == gguf9,
              t11.backend.model_path)
    finally:
        _lc._default_llama_factory = _orig_factory
        os.environ.pop(GGUF_ENV, None)


# ---------------------------------------------------------------------
# 7. Lazy loading: без тяжёлых импортов и без side effects
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
