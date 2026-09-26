# -*- coding: utf-8 -*-
"""Регрессионные/контрактные тесты на абстракцию бэкендов:
TranslationBackend / MarianBackend / TranslationService.

Запуск (без реальных моделей, без сети, без pytest):

    python tests/test_backends.py

Часть 1 — translation_service (общий слой, без движка):
модуль импортируется и работает, когда torch/transformers недоступны
(общий сервис не зависит от библиотек конкретного движка); словарь
приоритетнее движка; translate()/translate_stream() по юнитам и чанкам;
ошибки движка обрабатываются по контракту.
Часть 2 — backends.marian (MarianBackend со стабами torch/transformers):
создание и load() на фейках; лимит из config; выбор модели по
направлению; inference через backend; общий сервис и фасад
OfflineTranslator поверх (стаб-)MarianBackend.
"""
import contextlib
import json
import os
import re
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:300]))
    PASS.append(name)


TMP = tempfile.mkdtemp(prefix="oflt_backend_test_")


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------
# Часть 1. Общий слой без torch/transformers
# ---------------------------------------------------------------------
# Неразрешимость torch/transformers: «import torch» -> ImportError.
sys.modules["torch"] = None
sys.modules["transformers"] = None

import translation_service  # noqa: E402  (должен импортироваться без torch)
from translation_service import (  # noqa: E402
    TranslationService,
    split_sentence_to_chunks,
)

check("svc_module_no_engine",
      "torch" not in vars(translation_service)
      and "transformers" not in vars(translation_service),
      str([n for n in vars(translation_service)
           if "torch" in n.lower() or "transformers" in n.lower()]))

GEN_CALLS = []  # (direction, chunk) — вызовы inference стаб-движка


class StubBackend:
    """Стаб «движка»: предложение → чанки по 2 слова, chunk → «⟪chunk⟫».

    Намеренно НЕ наследуется от TranslationBackend (в части 1 пакет
    backends недоступен — torch выключен): сервис работает с любым
    объектом, реализующим контракт split_sentence/translate_chunk.
    """

    name = "stub"

    def __init__(self, chunk_words=2):
        self.chunk_words = chunk_words
        self.max_source_tokens = 10 ** 6

    def load(self):
        pass

    def split_sentence(self, sentence, direction):
        words = sentence.split()
        if not words:
            return [sentence]
        return [" ".join(words[i:i + self.chunk_words])
                for i in range(0, len(words), self.chunk_words)]

    def translate_chunk(self, chunk, direction):
        GEN_CALLS.append((direction, chunk))
        return "⟪" + chunk + "⟫"


dict_path = os.path.join(TMP, "stub_dict.json")
write_json(dict_path, {"workspace": "рабочее пространство"})

# 1.1 Словарный приоритет: backend не вызывается (двунаправленный lookup)
GEN_CALLS.clear()
svc = TranslationService(StubBackend(), dictionary_path=dict_path)
r = svc.translate("workspace", "en-ru")
check("stub_dict_en_ru", r == "рабочее пространство", r)
check("stub_dict_no_infer", len(GEN_CALLS) == 0, str(GEN_CALLS))
r = svc.translate("рабочее пространство", "ru-en")
check("stub_dict_ru_en", r == "workspace", r)

# 1.2 translate(): предложение → чанки → перевод, направление
GEN_CALLS.clear()
r = svc.translate("First sentence. Second one here.", "en-ru")
check("stub_two_sentences",
      r == "⟪First sentence.⟫ ⟪Second one⟫ ⟪here.⟫", r)
check("stub_direction_en_ru",
      all(d == "en-ru" for d, _ in GEN_CALLS), str(GEN_CALLS))

# 1.3 Длинное предложение — несколько чанков, порядок, без потерь
GEN_CALLS.clear()
long_s = " ".join("w%d" % i for i in range(7))
r = svc.translate(long_s, "ru-en")
chunks = re.findall(r"⟪(.*?)⟫", r)
check("stub_long_multi_chunk", len(chunks) == 4, str(chunks))
check("stub_long_order", " ".join(chunks).split() == long_s.split())
check("stub_direction_ru_en",
      all(d == "ru-en" for d, _ in GEN_CALLS), str(GEN_CALLS))

# 1.4 Пустой текст
check("stub_empty", svc.translate("   ", "en-ru") == "")


# 1.5 translate_stream: события start/done по юнитам, финал как translate
GEN_CALLS.clear()
events = []


def on_sentence(phase, done, total, unit):
    events.append((phase, done, total, unit.src, unit.translation))


text5 = "Alpha one. Beta two gamma. Delta."
final = svc.translate_stream(text5, "en-ru", on_sentence)
check("stub_stream_final_eq_translate",
      final == svc.translate(text5, "en-ru"), final)
check("stub_stream_final",
      final == "⟪Alpha one.⟫ ⟪Beta two⟫ ⟪gamma.⟫ ⟪Delta.⟫", final)
check("stub_stream_sources", [e[3] for e in events] ==
      ["Alpha one.", "Alpha one.",
       "Beta two gamma.", "Beta two gamma.",
       "Delta.", "Delta."], str(events))
check("stub_stream_phases",
      [e[0] for e in events] == ["start", "done"] * 3, str(events))
check("stub_stream_done_counts",
      [e[1] for e in events] == [0, 1, 1, 2, 2, 3], str(events))
check("stub_stream_totals", all(e[2] == 3 for e in events))
check("stub_stream_start_empty_translation",
      all(e[4] == "" for e in events if e[0] == "start"), str(events))
check("stub_stream_done_translation_set",
      all(e[4] != "" for e in events if e[0] == "done"), str(events))

# 1.6 Поток с точным словарным совпадением: один юнит
events.clear()
r = svc.translate_stream("workspace", "en-ru", on_sentence)
check("stub_stream_dict", r == "рабочее пространство", r)
check("stub_stream_dict_events",
      [e[0] for e in events] == ["start", "done"]
      and events[1][1] == 1, str(events))


# 1.7 Ошибка движка: translate() — «Ошибка перевода», stream — исключение
class BrokenBackend(StubBackend):
    def translate_chunk(self, chunk, direction):
        raise RuntimeError("boom")


svc_broken = TranslationService(BrokenBackend(), dictionary_path=dict_path)
r = svc_broken.translate("hello world", "en-ru")
check("broken_translate_error_msg", r == "Ошибка перевода: boom", r)
try:
    svc_broken.translate_stream("hello world", "en-ru")
    check("broken_stream_raises", False)
except RuntimeError as e:
    check("broken_stream_raises", str(e) == "boom", str(e))

# 1.8 split_sentence_to_chunks — общий алгоритм с произвольным счётчиком
cnt_words = lambda s: len(s.split())  # noqa: E731
check("chunk_short_1",
      split_sentence_to_chunks("one two", cnt_words, 4) == ["one two"])
nine = " ".join("x%d" % i for i in range(9))
chunks9 = split_sentence_to_chunks(nine, cnt_words, 4)
check("chunk_within_limit",
      all(len(c.split()) <= 4 for c in chunks9), str(chunks9))
check("chunk_words_preserved",
      " ".join(chunks9).split() == nine.split())
natural = split_sentence_to_chunks(
    "part one, part two; part three: four five", cnt_words, 4)
check("chunk_natural_within",
      all(len(c.split()) <= 4 for c in natural), str(natural))
check("chunk_natural_preserved",
      " ".join(natural).split()
      == "part one, part two; part three: four five".split())
cnt_chars = lambda s: len(s)  # noqa: E731
big = "a" * 25 + " " + "b" * 25
chunks_big = split_sentence_to_chunks(big, cnt_chars, 10)
check("char_split_no_loss",
      "".join(chunks_big) == "a" * 25 + "b" * 25, str(chunks_big))
check("char_split_limit",
      all(len(c) <= 10 for c in chunks_big), str(chunks_big))


# ---------------------------------------------------------------------
# Часть 2. MarianBackend со стабами torch/transformers
# ---------------------------------------------------------------------
del sys.modules["torch"]
del sys.modules["transformers"]

# Стабы (тот же приём, что в tests/test_long_text.py)
fake_torch = types.ModuleType("torch")
fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
fake_torch.no_grad = lambda: contextlib.nullcontext()
sys.modules["torch"] = fake_torch

GEN = []   # (model_name, kwargs) — вызовы model.generate
ENQ = []   # чанки, зашедшие в inference (FIFO)


class FakeEncoded:
    def __init__(self, ids, text):
        self._ids = ids
        self.text = text

    def __getitem__(self, key):
        return self._ids if key == "input_ids" else None

    def keys(self):
        return ("input_ids",)

    def to(self, *a, **k):
        return self


class FakeTokenizer:
    name = "?"

    def __init__(self, name):
        self.name = name

    @staticmethod
    def token_count(text):
        """2 special-токена + 1 за каждое слово (детерминированный счёт)."""
        return 2 + len(text.split())

    def __call__(self, text, return_tensors=None, padding=False,
                 truncation=False, max_length=None, add_special_tokens=True):
        if return_tensors:
            ENQ.append(text)
        return FakeEncoded([0] * self.token_count(text), text)

    def decode(self, item, skip_special_tokens=False):
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
        GEN.append((self.name, kwargs))
        text = ENQ.pop(0)
        return [FakeEncoded([0], text)]


class FakeAutoTokenizer:
    @staticmethod
    def from_pretrained(name, cache_dir=None, **kw):
        return FakeTokenizer(name)


class FakeAutoModel:
    @staticmethod
    def from_pretrained(name, cache_dir=None, **kw):
        return FakeModel(name)


fake_transformers = types.ModuleType("transformers")
fake_transformers.AutoTokenizer = FakeAutoTokenizer
fake_transformers.AutoModelForSeq2SeqLM = FakeAutoModel
sys.modules["transformers"] = fake_transformers

from backends import MarianBackend, TranslationBackend  # noqa: E402
from translator import OfflineTranslator  # noqa: E402

EN = "Helsinki-NLP/opus-mt-en-ru"
RU = "Helsinki-NLP/opus-mt-ru-en"

# 2.1 Бэкенд создаётся со стаб-объектами: конструктор → load()
b = MarianBackend(cache_dir=os.path.join(TMP, "cache"))
check("marian_name", MarianBackend.name == "marian")
check("marian_is_backend", isinstance(b, TranslationBackend))
check("marian_not_loaded_yet", b._models == {})
b.load()
check("marian_loaded_both_dirs",
      set(b._models) == {"en-ru", "ru-en"}, str(b._models))
check("marian_device_cpu", b.device == "cpu", b.device)
check("marian_limit_from_config",
      b.max_source_tokens == 480, str(b.max_source_tokens))
check("marian_en_ru_model", b._models["en-ru"][0].name == EN,
      b._models["en-ru"][0].name)
check("marian_ru_en_model", b._models["ru-en"][0].name == RU,
      b._models["ru-en"][0].name)
check("marian_tokenizers",
      b._models["en-ru"][1].name == EN
      and b._models["ru-en"][1].name == RU)

# 2.2 split_sentence: чанки в лимите; короткое предложение — 1 чанк
short = "hello world"
check("marian_split_short_1", b.split_sentence(short, "en-ru") == [short])
long_s2 = " ".join("w%d" % i for i in range(700))
chunks2 = b.split_sentence(long_s2, "en-ru")
check("marian_split_multi", len(chunks2) >= 2, str(len(chunks2)))
check("marian_chunk_within_limit",
      all(FakeTokenizer.token_count(c) <= 480 for c in chunks2),
      str([FakeTokenizer.token_count(c) for c in chunks2]))
check("marian_split_words_preserved",
      " ".join(chunks2).split() == long_s2.split())

# 2.3 Inference идёт через backend.translate_chunk; направление — модель
GEN.clear(); ENQ.clear()
r = b.translate_chunk("hello world", "en-ru")
check("marian_infer_echo", r == "⟪hello world⟫", r)
check("marian_infer_model_en", GEN and GEN[0][0] == EN, str(GEN))
check("marian_infer_kwargs",
      GEN[0][1].get("max_length") == 512 and GEN[0][1].get("num_beams") == 4,
      str(GEN[0][1]))
check("marian_encode_consumed", len(ENQ) == 0, str(ENQ))
GEN.clear()
b.translate_chunk("привет мир", "ru-en")
check("marian_infer_model_ru", GEN and GEN[0][0] == RU, str(GEN))

# 2.4 Неизвестное направление — ValueError (и на split, и на inference)
for bad in ("fr-de", "en-en", ""):
    try:
        b.translate_chunk("x", bad)
        check("marian_bad_dir_infer_%r" % bad, False)
    except ValueError:
        check("marian_bad_dir_infer_%r" % bad, True)
    try:
        b.split_sentence("x y", bad)
        check("marian_bad_dir_split_%r" % bad, False)
    except ValueError:
        check("marian_bad_dir_split_%r" % bad, True)

# 2.5 Общий сервис с (стаб-)MarianBackend: inference через бэкенд
dict2 = os.path.join(TMP, "marian_dict.json")
write_json(dict2, {"workspace": "рабочее пространство"})
svc2 = TranslationService(b, dictionary_path=dict2)
GEN.clear()
r = svc2.translate("hello world. How are you?", "en-ru")
check("svc2_result", r == "⟪hello world.⟫ ⟪How are you?⟫", r)
check("svc2_infer_via_backend", [g[0] for g in GEN] == [EN, EN], str(GEN))
GEN.clear()
r = svc2.translate("workspace", "en-ru")
check("svc2_dict_priority",
      r == "рабочее пространство" and not GEN, (r, str(GEN)))
GEN.clear()
r = svc2.translate("Привет, мир. Как дела?", "ru-en")
check("svc2_ru_en",
      r == "⟪Привет, мир.⟫ ⟪Как дела?⟫"
      and [g[0] for g in GEN] == [RU, RU], (r, str(GEN)))

# 2.6 translate_stream через бэкенд
events2 = []
final2 = svc2.translate_stream(
    "Alpha one. Beta two.", "en-ru",
    on_sentence=lambda ph, d, t, u: events2.append(ph))
check("svc2_stream_final", final2 == "⟪Alpha one.⟫ ⟪Beta two.⟫", final2)
check("svc2_stream_events",
      events2 == ["start", "done", "start", "done"], str(events2))

# 2.7 Фасад translator.OfflineTranslator: публичный API сохранён
t = OfflineTranslator(cache_dir=os.path.join(TMP, "cache2"))
check("facade_backend_marian", isinstance(t.backend, MarianBackend))
check("facade_max_source_tokens",
      t.max_source_tokens == 480, str(t.max_source_tokens))
check("facade_device", t.device == "cpu", t.device)
check("facade_dictionary_path_attr",
      bool(t.dictionary_path), t.dictionary_path)
GEN.clear()
r = t.translate("hello world.", "en-ru")
check("facade_translate", r == "⟪hello world.⟫", r)
check("facade_infer_en", [g[0] for g in GEN] == [EN], str(GEN))
GEN.clear()
r = t.translate("Привет, мир.", "ru-en")
check("facade_translate_ru",
      r == "⟪Привет, мир.⟫" and [g[0] for g in GEN] == [RU], (r, str(GEN)))

# Исторические методы (контракт регрессионных тестов)
ot = OfflineTranslator.__new__(OfflineTranslator)
check("facade_split_text",
      ot._split_text("A. B.\n\nC.") == [["A.", "B."], ["C."]])
ot.max_source_tokens = 4
tok = FakeTokenizer("test")
ch = ot._split_sentence_to_chunks("one two three four five six seven", tok)
check("facade_compat_chunks",
      all(FakeTokenizer.token_count(c) <= 4 for c in ch), str(ch))
check("facade_compat_words",
      " ".join(ch).split() == "one two three four five six seven".split())

print()
print("=" * 60)
print("test_backends: ПРОШЛО ПРОВЕРОК: %d" % len(PASS))
print("Список проверок:")
for name in PASS:
    print("  ✓", name)