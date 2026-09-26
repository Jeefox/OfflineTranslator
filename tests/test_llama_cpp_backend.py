# -*- coding: utf-8 -*-
"""Этап 6: тесты LlamaCppBackend (GGUF/llama.cpp, CPU-POC).

Запуск (без реальной GGUF-модели, без сети, без pytest):

    python tests/test_llama_cpp_backend.py

Весь inference — через фейковый runtime (тестовый шов llama_class в
LlamaCppBackend), имитирующий интерфейс llama_cpp.Llama
(tokenize/create_completion). Реальная модель — в отдельном smoke
тесте tests/smoke_gguf.py (запускается только при заданной
переменной OFFLINE_TRANSLATOR_GGUF, иначе SKIP).

Покрытие: импорт без установленного llama_cpp (ленивый импорт);
создание бэкенда и валидация пути/настроек; token/context-лимит
(безопасный лимит: весь контекст для source text не используется);
prompt (явное направление, официальный chat template, sampling);
translate_chunk/split_sentence через fake runtime; контракт-
совместимость (TranslationService: translate/stream, словарь,
ошибки); фасад OfflineTranslator(backend='llama_cpp', ...);
GUI-агностичность (в main.py нет веток по конкретному движку).
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:300]))
    PASS.append(name)


TMP = tempfile.mkdtemp(prefix="oflt_llama_cpp_test_")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# Dummy-«GGUF»: бэкенд не читает содержимое до load(); load() —
# через fake runtime, поэтому содержимое не важно.
GGUF = os.path.join(TMP, "fake_model.gguf")
with open(GGUF, "wb") as f:
    f.write(b"GGUF" + b"\x00" * 64)


class FakeLlama:
    """Фейк llama.cpp-runtime: интерфейс llama_cpp.Llama
    (tokenize/create_completion), как его использует
    LlamaCppBackend. Токенизатор детерминированный: спец-маркеры
    Hy-MT2 — по одному токену, остальные слова — по одному токену на
    слово (в пределах процесса достаточно для лимитных проверок)."""

    instances = []

    def __init__(self, model_path, n_ctx, verbose=False,
                 response="Привет, как дела?"):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.verbose = verbose
        self.response = response
        self.raise_error = None
        self.completions = []      # (prompt_ids, kwargs) — вызовы inference
        self.tokenize_calls = []   # (text, add_bos, special)
        FakeLlama.instances.append(self)

    def _encode(self, text):
        ids = []
        for part in re.split(r"(<｜[^｜]*｜>)", text):
            if not part:
                continue
            if part.startswith("<｜") and part.endswith("｜>"):
                ids.append(zlib.crc32(part.encode("utf-8")) % 1000000 + 100000)
            else:
                ids.extend(zlib.crc32(w.encode("utf-8")) % 1000000 + 1
                           for w in part.split())
        return ids

    def tokenize(self, text, add_bos=True, special=False):
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        self.tokenize_calls.append((text, add_bos, special))
        return self._encode(text)

    def create_completion(self, prompt=None, max_tokens=None, stop=None, **kw):
        self.completions.append(
            (list(prompt), dict(max_tokens=max_tokens, stop=stop, **kw)))
        if self.raise_error:
            raise self.raise_error
        return {"choices": [{"text": self.response}],
                "usage": {"prompt_tokens": len(prompt),
                          "completion_tokens": 4}}


def make_factory(response="Привет, как дела?"):
    """Фабрика (model_path, n_ctx, verbose) -> FakeLlama (шов llama_class)."""
    def factory(model_path, n_ctx, verbose=False):
        return FakeLlama(model_path, n_ctx, verbose, response=response)
    return factory


# ---------------------------------------------------------------------
# Часть 1. Импорт без установленного llama_cpp (ленивый импорт)
# ---------------------------------------------------------------------
# В subprocess: sys.modules['llama_cpp'] = None -> «import llama_cpp»
# даёт ImportError; модули проекта импортироваться обязаны (llama_cpp
# нужен только в момент load(), а не при импорте).
_lazy_code = (
    "import sys; sys.modules['llama_cpp'] = None; "
    "sys.path.insert(0, %r); "
    "import backends, backends.llama_cpp, backends.prompts, translator; "
    "print('OK')" % ROOT
)
_r = subprocess.run([sys.executable, "-c", _lazy_code],
                    capture_output=True, text=True, cwd=ROOT, timeout=120)
check("lazy_import_no_llamacpp",
      _r.returncode == 0 and "OK" in _r.stdout, _r.stderr[-300:])

import backends  # noqa: E402
import backends.llama_cpp as lc  # noqa: E402
from backends import prompts as lp  # noqa: E402
from backends.base import TranslationBackend  # noqa: E402
from backends.llama_cpp import LlamaCppBackend  # noqa: E402
import translator as T  # noqa: E402


# ---------------------------------------------------------------------
# Часть 2. Создание бэкенда: путь, валидация, атрибуты
# ---------------------------------------------------------------------
b = LlamaCppBackend(GGUF, llama_class=make_factory())
check("llama_name", b.name == "llama_cpp", b.name)
check("llama_device_cpu", b.device == "cpu", b.device)
check("llama_model_path", b.model_path == GGUF, b.model_path)
check("llama_is_translation_backend", isinstance(b, TranslationBackend))
for attr in ("max_source_tokens", "split_sentence", "translate_chunk",
             "load"):
    check("llama_has_%s" % attr, hasattr(b, attr))

# Невалидный путь: файла нет / каталог / пустая строка
try:
    LlamaCppBackend(os.path.join(TMP, "nope.gguf"))
    check("llama_bad_path", False)
except FileNotFoundError:
    check("llama_bad_path", True)
try:
    LlamaCppBackend(TMP)
    check("llama_dir_path", False)
except (FileNotFoundError, ValueError):
    check("llama_dir_path", True)
try:
    LlamaCppBackend("")
    check("llama_empty_path", False)
except ValueError:
    check("llama_empty_path", True)

# Валидация POC-настроек
for name, kwargs in (("ctx_small", dict(context_length=500)),
                     ("ctx_huge", dict(context_length=200000)),
                     ("out_small", dict(max_output_tokens=8)),
                     ("src_small", dict(max_source_tokens=0))):
    try:
        LlamaCppBackend(GGUF, **kwargs)
        check("llama_invalid_%s" % name, False)
    except ValueError:
        check("llama_invalid_%s" % name, True)


# ---------------------------------------------------------------------
# Часть 3. Token/context-лимит (безопасный лимит POC)
# ---------------------------------------------------------------------
B = LlamaCppBackend(GGUF)  # POC-дефолты: ctx=4096, out=1024
check("llama_auto_limit_defaults",
      B.max_source_tokens
      == B.context_length - B.max_output_tokens
      - B.PROMPT_RESERVE_TOKENS - B.SAFETY_MARGIN_TOKENS,
      str(B.max_source_tokens))
check("llama_auto_limit_defaults_value",
      B.max_source_tokens == 4096 - 1024 - 128 - 32, str(B.max_source_tokens))
# Весь контекст для source text НЕ предназначен: prompt и генерация
# тоже живут в контексте.
check("llama_limit_not_whole_context",
      B.max_source_tokens < B.context_length
      and B.max_source_tokens <= B.context_length - B.max_output_tokens,
      str((B.max_source_tokens, B.context_length)))
B2 = LlamaCppBackend(GGUF, context_length=2048, max_output_tokens=512)
check("llama_auto_limit_custom_ctx",
      B2.max_source_tokens == 2048 - 512 - 128 - 32, str(B2.max_source_tokens))
B3 = LlamaCppBackend(GGUF, max_source_tokens=777)
check("llama_explicit_limit", B3.max_source_tokens == 777,
      str(B3.max_source_tokens))
B4 = LlamaCppBackend(GGUF, context_length=512, max_output_tokens=400)
check("llama_limit_floor", B4.max_source_tokens == 16,
      str(B4.max_source_tokens))


# ---------------------------------------------------------------------
# Часть 4. Prompt (backends.prompts): явное направление, template
# ---------------------------------------------------------------------
ins_ru = lp.build_translation_instruction("Hello", "en-ru")
check("prompt_en_ru_target", "into Russian" in ins_ru, ins_ru)
check("prompt_en_ru_no_english", "English" not in ins_ru, ins_ru)
check("prompt_text_kept", "Hello" in ins_ru, ins_ru)
check("prompt_official_constraints",
      "only output the translated result" in ins_ru
      and "without any additional explanation" in ins_ru, ins_ru)
ins_en = lp.build_translation_instruction("Привет", "ru-en")
check("prompt_ru_en_target",
      "into English" in ins_en and "Russian" not in ins_en, ins_en)
check("prompt_ru_text_kept", "Привет" in ins_en, ins_en)
for bad in ("fr-de", "en-en", ""):
    try:
        lp.build_translation_instruction("x", bad)
        check("prompt_bad_dir_%r" % bad, False)
    except ValueError:
        check("prompt_bad_dir_%r" % bad, True)
raw = lp.build_raw_prompt("Hello", "en-ru")
check("prompt_raw_skeleton",
      raw.startswith(lp.BOS + lp.USER) and raw.endswith(lp.ASSISTANT)
      and "into Russian" in raw and "Hello" in raw, raw[:160])
check("prompt_no_system", "system" not in raw.lower(), raw[:160])
check("prompt_sampling_params",
      lp.SAMPLING == {"temperature": 0.0, "top_p": 0.6, "top_k": 20,
                      "repeat_penalty": 1.05}, str(lp.SAMPLING))
check("prompt_eos_token",
      lp.EOS == "<｜hy_place▁holder▁no▁2｜>", lp.EOS)


# ---------------------------------------------------------------------
# Часть 5. translate_chunk / load через fake runtime
# ---------------------------------------------------------------------
b = LlamaCppBackend(GGUF, llama_class=make_factory())
# До load() — RuntimeError (модель не загружена)
for op in ("translate_chunk", "split_sentence"):
    try:
        getattr(b, op)("hello", "en-ru")
        check("llama_%s_before_load" % op, False)
    except RuntimeError:
        check("llama_%s_before_load" % op, True)

b.load()
fake = FakeLlama.instances[-1]
check("llama_load_factory_args",
      fake.n_ctx == 4096 and fake.model_path == GGUF,
      str((fake.n_ctx, fake.model_path)))
check("llama_llm_loaded", b._llm is fake)
b.load()  # повторный load — идемпотентен
check("llama_load_idempotent", b._llm is fake)

r = b.translate_chunk("Hello, how are you?", "en-ru")
check("llama_infer_result", r == "Привет, как дела?", r)
check("llama_infer_once", len(fake.completions) == 1,
      str(len(fake.completions)))
prompt_ids, kw = fake.completions[0]
check("llama_infer_prompt_ids",
      isinstance(prompt_ids, list) and len(prompt_ids) >= 10,
      str(len(prompt_ids)))
check("llama_infer_max_tokens",
      kw.get("max_tokens") == b.max_output_tokens, str(kw))
check("llama_infer_stop_eos", kw.get("stop") == [lp.EOS], str(kw.get("stop")))
for k, v in lp.SAMPLING.items():
    check("llama_infer_sampling_%s" % k, kw.get(k) == v, str(kw))
call = fake.tokenize_calls[-1]
check("llama_prompt_tokenized_special",
      call[1] is False and call[2] is True, str(call[1:]))
ptext = call[0]
check("llama_prompt_en_ru",
      ptext.startswith(lp.BOS + lp.USER) and ptext.endswith(lp.ASSISTANT)
      and "into Russian" in ptext and "Hello, how are you?" in ptext,
      ptext[:160])
b.translate_chunk("Привет, как у тебя дела?", "ru-en")
ptext2 = fake.tokenize_calls[-1][0]
check("llama_prompt_ru_en",
      "into English" in ptext2 and "into Russian" not in ptext2
      and "Привет, как у тебя дела?" in ptext2, ptext2[:160])

# Пустой результат — RuntimeError (общий сервис обернёт в «Ошибка перевода»)
fake.response = ""
try:
    b.translate_chunk("x", "en-ru")
    check("llama_infer_empty_error", False)
except RuntimeError as e:
    check("llama_infer_empty_error", "пустой" in str(e), str(e))

# Ошибка runtime пробрасывается вызывающему
fake.response = "ok"
fake.raise_error = RuntimeError("model crash")
try:
    b.translate_chunk("x", "en-ru")
    check("llama_infer_error_propagates", False)
except RuntimeError as e:
    check("llama_infer_error_propagates", "model crash" in str(e), str(e))
fake.raise_error = None

# Валидация направления (модель загружена)
for bad in ("fr-de", "en-en", ""):
    try:
        b.translate_chunk("x", bad)
        check("llama_bad_dir_infer_%r" % bad, False)
    except ValueError:
        check("llama_bad_dir_infer_%r" % bad, True)
    try:
        b.split_sentence("x", bad)
        check("llama_bad_dir_split_%r" % bad, False)
    except ValueError:
        check("llama_bad_dir_split_%r" % bad, True)


# ---------------------------------------------------------------------
# Часть 6. split_sentence (общий алгоритм + свой счётчик/лимит)
# ---------------------------------------------------------------------
bs = LlamaCppBackend(GGUF, llama_class=make_factory(),
                     max_source_tokens=5)
bs.load()
fake_s = FakeLlama.instances[-1]
check("llama_split_short_single",
      bs.split_sentence("hello world", "en-ru") == ["hello world"])
chunks = bs.split_sentence(" ".join(["alpha"] * 20), "en-ru")
check("llama_split_chunks", len(chunks) >= 4, str(chunks))
check("llama_split_within_limit",
      all(len(c.split()) <= 5 for c in chunks), str(chunks))
check("llama_split_no_loss",
      " ".join(chunks).split() == ["alpha"] * 20, str(chunks))
check("llama_split_order", chunks[0] == " ".join(["alpha"] * 5), str(chunks))
check("llama_split_counter_used",
      any(t for t, _ab, _sp in fake_s.tokenize_calls), str(fake_s.tokenize_calls[:2]))


# ---------------------------------------------------------------------
# Часть 7. Контракт: TranslationService поверх LlamaCppBackend (fake)
# ---------------------------------------------------------------------
from translation_service import TranslationService  # noqa: E402

dict_llama = os.path.join(TMP, "llama_dict.json")
write_json(dict_llama, {"workspace": "рабочее пространство"})

b7 = LlamaCppBackend(GGUF, llama_class=make_factory(response="⟪translated⟫"))
svc = TranslationService(b7, dictionary_path=dict_llama)
# load() уже вызван сервисом в __init__
fake7 = FakeLlama.instances[-1]
check("svc_llama_loaded", b7._llm is fake7)
r = svc.translate("Hello world.", "en-ru")
check("svc_llama_translate", r == "⟪translated⟫", r)
check("svc_llama_infer_en",
      len(fake7.completions) == 1
      and "into Russian" in fake7.tokenize_calls[-1][0],
      str(fake7.completions))
fake7.completions.clear()
events = []
final = svc.translate_stream(
    "Alpha one. Beta two.", "en-ru",
    on_sentence=lambda ph, d, t, u: events.append(ph))
check("svc_llama_stream_final", final == "⟪translated⟫ ⟪translated⟫", final)
check("svc_llama_stream_events",
      events == ["start", "done", "start", "done"], str(events))
# Приоритет словаря: backend не вызывается
fake7.completions.clear()
r = svc.translate("workspace", "en-ru")
check("svc_llama_dict_priority",
      r == "рабочее пространство" and not fake7.completions,
      (r, str(fake7.completions)))
# Ошибки inference: translate() оборачивает в «Ошибка перевода: ...»
fake7.raise_error = RuntimeError("model crash")
r = svc.translate("Hello world.", "en-ru")
check("svc_llama_error_wrap",
      r.startswith("Ошибка перевода: model crash"), r)
fake7.raise_error = None
# Пустой результат inference также → «Ошибка перевода: ...»
fake7.response = ""
r = svc.translate("Hello world.", "en-ru")
check("svc_llama_empty_wrap", r.startswith("Ошибка перевода:"), r)
fake7.response = "⟪translated⟫"


# ---------------------------------------------------------------------
# Часть 8. Фасад OfflineTranslator(backend='llama_cpp', ...)
# ---------------------------------------------------------------------
# Фасад создаёт LlamaCppBackend без llama_class — подменяем модульный
# _default_llama_factory (monkeypatch-стиль, как translator.load_snapshot
# в test_dictionary.py).
_factory_holder = {"f": make_factory(response="⟪facade⟫")}
_orig_factory = lc._default_llama_factory
lc._default_llama_factory = (
    lambda mp, n_ctx, verbose=False, _h=_factory_holder: _h["f"](
        mp, n_ctx, verbose))
try:
    t = T.OfflineTranslator(backend="llama_cpp", gguf_path=GGUF)
    check("facade_llama_backend",
          isinstance(t.backend, LlamaCppBackend),
          str(type(t.backend)))
    check("facade_llama_is_service", isinstance(t, TranslationService))
    check("facade_llama_max_source_tokens",
          t.max_source_tokens == 4096 - 1024 - 128 - 32,
          str(t.max_source_tokens))
    check("facade_llama_device", t.device == "cpu", t.device)
    check("facade_llama_cache_manager_none", t.cache_manager is None)
    check("facade_llama_dictionary_path_attr",
          bool(t.dictionary_path), t.dictionary_path)
    check("facade_llama_translate",
          t.translate("Hello world.", "en-ru") == "⟪facade⟫")
    ev = []
    fin = t.translate_stream(
        "Alpha one. Beta two.", "en-ru",
        on_sentence=lambda ph, d, t2, u: ev.append(ph))
    check("facade_llama_stream",
          fin == "⟪facade⟫ ⟪facade⟫"
          and ev == ["start", "done", "start", "done"], (fin, str(ev)))
finally:
    lc._default_llama_factory = _orig_factory

# Валидация выбора бэкенда (без загрузки моделей)
try:
    T.OfflineTranslator(backend="llama_cpp")
    check("facade_llama_requires_path", False)
except ValueError as e:
    check("facade_llama_requires_path", "gguf_path" in str(e), str(e))
try:
    T.OfflineTranslator(backend="nonsense")
    check("facade_unknown_backend", False)
except ValueError as e:
    check("facade_unknown_backend", "backend" in str(e), str(e))


# ---------------------------------------------------------------------
# Часть 9. GUI не знает, какой движок выполняется
# ---------------------------------------------------------------------
main_src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
low = main_src.lower()
check("gui_no_engine_words",
      not any(w in low for w in ("gguf", "llama", "hy-mt2", "marian")),
      str([w for w in ("gguf", "llama", "hy-mt2", "marian") if w in low]))
ot_lines = [l.strip() for l in main_src.splitlines()
            if "OfflineTranslator(" in l]
check("gui_translator_no_engine_args",
      bool(ot_lines) and not any("backend" in l.lower() for l in ot_lines),
      str(ot_lines))


print()
print("=" * 60)
print("test_llama_cpp_backend: ПРОШЛО ПРОВЕРОК: %d" % len(PASS))
print("Список проверок:")
for name in PASS:
    print("  ✓", name)
