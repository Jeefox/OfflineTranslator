# -*- coding: utf-8 -*-
"""Тесты обработки длинного текста в translator.py (stub torch/transformers).

Запуск (реальные модели не скачиваются, сеть и pytest не нужны):

    python tests/test_long_text.py

Проверяют:
- отсутствие тихой потери текста (все фрагменты обработаны, порядок сохранён);
- разбиение по абзацам/предложениям/знакам/словам/символам;
- лимит входных ТОКЕНОВ (config − запас), а не символов;
- max_length токенизатора == OfflineTranslator.max_source_tokens:
  куски не длиннее лимита, truncation=True оставлен страховкой и
  ни один путь chunking не приводит к скрытому truncation;
- generate(max_length=512) не изменён (это отдельный выходной лимит);
- короткие тексты → один inference;
- оба направления; словарь срабатывает раньше нейросети.
"""
import contextlib
import os
import re
import sys
import tempfile
import types

# Реальный модуль проекта: корень репозитория — родитель tests/
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CALLS = []  # (model_name, chunk) — все вызовы generate в порядке выполнения
ENQ = []   # очередь текстов, закодированных для inference
TOK = []   # (text, truncation, max_length) — все inference-вызовы токенизатора
GEN = []   # kwargs всех вызовов model.generate


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
    @staticmethod
    def count_tokens(text):
        """Детерминированный 'токенизатор': 2 спецтокена + по каждому
        whitespace-слову: 1 токен + 1 токен за каждые 8 символов сверх 20.
        Имитация: символы != токены, длинные 'слова' (URL) дороже."""
        n = 2
        for w in text.split():
            n += 1 + max(0, (len(w) - 20) // 8)
        return n

    def __call__(self, text, return_tensors=None, padding=False, truncation=False,
                 max_length=None, add_special_tokens=True):
        n = self.count_tokens(text)
        if return_tensors:
            ENQ.append(text)
            TOK.append((text, truncation, max_length))
        return FakeEncoded([0] * n, text)

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
        GEN.append(kwargs)
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

import translator  # реальный модуль проекта (осознанно после установки стыков)

t = translator.OfflineTranslator(
    cache_dir=os.path.join(tempfile.gettempdir(), "oflt_test_cache")
)

PASS = []
def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:300]))
    PASS.append(name)

def reset():
    CALLS.clear()
    ENQ.clear()
    TOK.clear()
    GEN.clear()

def norm(s):
    return re.sub(r"\s+", "", s)

def assert_full(result, original, model_name):
    """Весь исходный текст обработан, порядок сохранён, ошибок нет,
    все вызовы — нужной модели, все куски ≤ лимита токенов,
    max_length токенизатора == лимиту (скрытого truncation нет),
    выходной лимит generate (512) не изменён."""
    check("no_error", not result.startswith("Ошибка"), result[:120])
    cs = re.findall(r"⟪(.*?)⟫", result)
    check("nonempty_chunks", len(cs) > 0)
    check("completeness+order", norm(" ".join(cs)) == norm(original),
          "\n got=%r\n exp=%r" % (norm(" ".join(cs))[:200], norm(original)[:200]))
    check("all_calls_model", all(n == model_name for n, _ in CALLS),
          str(set(n for n, _ in CALLS)))
    check("one_tok_call_per_infer", len(TOK) == len(CALLS),
          "tok=%d infer=%d" % (len(TOK), len(CALLS)))
    for text, trunc, ml in TOK:
        n_tok = FakeTokenizer.count_tokens(text)
        check("chunk_within_limit", n_tok <= t.max_source_tokens,
              "count=%d limit=%d" % (n_tok, t.max_source_tokens))
        check("tok_max_length_is_limit", ml == t.max_source_tokens,
              "max_length=%r limit=%d" % (ml, t.max_source_tokens))
        check("tok_truncation_kept", trunc is True, "truncation=%r" % (trunc,))
        check("no_hidden_truncation", n_tok <= ml,
              "count=%d max_length=%r" % (n_tok, ml))
    for kw in GEN:
        check("gen_max_length_512", kw.get("max_length") == 512, str(kw.get("max_length")))

EN = "Helsinki-NLP/opus-mt-en-ru"
RU = "Helsinki-NLP/opus-mt-ru-en"

# 0. Лимит берётся из config (512 − 32 = 480), а не хардкод символов
check("limit_from_config", t.max_source_tokens == 480, str(t.max_source_tokens))

# 1. Короткий EN -> RU: два предложения, каждое -> один inference
reset()
r = t.translate("Hello world. How are you?", "en-ru")
assert_full(r, "Hello world. How are you?", EN)
check("short_en_2_infer", len(CALLS) == 2, str(CALLS))

# 2. Короткий RU -> EN
reset()
r = t.translate("Привет, мир. Как дела?", "ru-en")
assert_full(r, "Привет, мир. Как дела?", RU)
check("short_ru_2_infer", len(CALLS) == 2, str(CALLS))

# 3. Несколько предложений — точный порядок
reset()
r = t.translate("One. Two! Three?", "en-ru")
assert_full(r, "One. Two! Three?", EN)
check("sentence_order", [c for _, c in CALLS] == ["One.", "Two!", "Three?"])

# 4. Длинное английское предложение > лимита (разбивка по запятам)
long_en = ", ".join("w%d" % i for i in range(700))
reset()
r = t.translate(long_en, "en-ru")
assert_full(r, long_en, EN)
check("long_en_multi_chunk", len(CALLS) >= 2, str(len(CALLS)))

# 5. Длинное русское предложение > лимита (без запятых -> по словам)
long_ru = " ".join("слово%d" % i for i in range(700))
reset()
r = t.translate(long_ru, "ru-en")
assert_full(r, long_ru, RU)
check("long_ru_multi_chunk", len(CALLS) >= 2, str(len(CALLS)))

# 6. Очень длинный текст из нескольких абзацев
para = " ".join("p%d" % i for i in range(300))
text6 = para + "\n\n" + para + "\n\n" + para
reset()
r = t.translate(text6, "en-ru")
assert_full(r, text6, EN)
check("paragraphs_preserved", r.count("\n\n") == 2, repr(r[:60]))
check("3_paragraphs_3_infer", len(CALLS) == 3, str(len(CALLS)))

# 7. Текст без завершающей точки -> один inference, результат без потери
reset()
r = t.translate("just some words no end", "en-ru")
assert_full(r, "just some words no end", EN)
check("no_trailing_dot_1_infer", len(CALLS) == 1)
check("no_trailing_dot_result", r == "⟪just some words no end⟫", r)

# 8. Несколько пробелов
reset()
r = t.translate("A.   B    C", "en-ru")
assert_full(r, "A.   B    C", EN)
check("multi_space_2_infer", [c for _, c in CALLS] == ["A.", "B    C"], str(CALLS))

# 9. Знаки , ; : — ! ? (короткое предложение + длинное с запятыми)
punct = "First, second; third: fourth — fifth. " + ", ".join("x%d" % i for i in range(600)) + "!"
reset()
r = t.translate(punct, "en-ru")
assert_full(r, punct, EN)
check("punct_multi_chunk", len(CALLS) >= 2, str(len(CALLS)))

# 10. URL / техническая строка: одно «слово» длиннее лимита -> fallback по символам
url = "https://" + "a" * 4000
reset()
r = t.translate("See %s for details." % url, "en-ru")
assert_full(r, "See %s for details." % url, EN)
check("url_multi_chunk", len(CALLS) >= 3, str(len(CALLS)))

# 11. Текст ровно в лимите (480 токенов) -> один chunk
exact = " ".join("w%d" % i for i in range(478))  # 2 спец + 478 слов = 480
reset()
r = t.translate(exact, "en-ru")
assert_full(r, exact, EN)
check("exact_limit_1_chunk", len(CALLS) == 1,
      "count=%d limit=%d" % (FakeTokenizer.count_tokens(exact), t.max_source_tokens))

# 12. Чуть больше лимита (482 токена) -> два chunk
over = " ".join("w%d" % i for i in range(480))
reset()
r = t.translate(over, "en-ru")
assert_full(r, over, EN)
check("over_limit_2_chunks", len(CALLS) == 2, str(len(CALLS)))

# 13. Очень длинный текст, много chunk (оба направления)
huge = " ".join("h%d" % i for i in range(3000))
reset()
r = t.translate(huge, "en-ru")
assert_full(r, huge, EN)
check("huge_en_chunks", len(CALLS) >= 6, str(len(CALLS)))
reset()
r = t.translate(huge, "ru-en")
assert_full(r, huge, RU)
check("huge_ru_chunks", len(CALLS) >= 6, str(len(CALLS)))

# Регрессия: словарь срабатывает раньше нейросети (нейросеть не вызывается)
reset()
r = t.translate("workspace", "en-ru")
check("dict_en_ru", r == "рабочее пространство", r)
check("dict_no_infer", len(CALLS) == 0)
reset()
r = t.translate("рабочее пространство", "ru-en")
check("dict_ru_en", r == "workspace", r)
check("dict_ru_en_no_infer", len(CALLS) == 0)

# Регрессия: пустой текст
check("empty_text", t.translate("   ", "en-ru") == "")

print("OK: %d checks passed" % len(PASS))

