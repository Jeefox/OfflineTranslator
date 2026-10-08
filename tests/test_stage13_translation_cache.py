# -*- coding: utf-8 -*-
"""Этап 13: in-memory инкрементальный кэш перевода.

Проверяет механизм кэша (translation_cache.TranslationCache внутри
TranslationService) — чистая логика: без GUI, без torch, без сети и
без скачивания моделей. Перевод выполняется реальным
TranslationService с детерминированным фейковым backend'ом (контракт
TranslationBackend: load / max_source_tokens / split_sentence /
translate_chunk — все вызовы учитываются).

Запуск:

    python tests/test_stage13_translation_cache.py

Структура:
1. TranslationCache: ключ (model_id, direction, текст юнита) —
   равенство/неравенство; put/get/contains/len/clear.
2. Первый перевод — все промахи; повтор — все попадания (backend
   не вызывается).
3. Изменение исходного текста: удаление последнего/среднего юнита,
   изменение одного юнита, добавление, переупорядочение — backend
   вызывается только для изменённых/новых юнитов; порядок вывода и
   границы абзацев сохраняются.
4. Смена направления — все промахи (направление входит в ключ).
5. Смена model_id — все промахи (model_id входит в ключ);
   дубликаты юнитов разделяют одну запись.
6. Мультичанковый юнит: промах — вызовы на каждый чанк; попадание
   — backend не вызывается.
7. Стриминг: события start/done (порядок, счётчики, перевод в done),
   финал == assemble_output == translate(); смешанные попадания/
   промахи — backend вызывается только для промахов.
8. Словарь: точный lookup всего текста — до кэша и с приоритетом;
   в кэш не попадает.
9. Ошибка backend не кэшируется; пустой текст — без вызовов.
"""
import os
import sys


def expect_translation_error(function, *args):
    try:
        function(*args)
    except (RuntimeError, ValueError) as exc:
        return "Ошибка перевода: " + str(exc)
    raise AssertionError("Translation must raise on failure")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sentence_pipeline import assemble_output, split_units  # noqa: E402
from translation_cache import TranslationCache  # noqa: E402
from translation_service import TranslationService  # noqa: E402

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:500]))
    PASS.append(name)


# =====================================================================
#  Фейки
# =====================================================================
class FakeBackend:
    """Детерминированный backend (контракт TranslationBackend):
    перевод чанка — "[dir:чанк]"; все вызовы записываются в списки."""

    def __init__(self, chunks_per_unit=1):
        self.chunks_per_unit = chunks_per_unit
        self.loaded = False
        self.split_calls = []   # (direction, юнит)
        self.chunk_calls = []   # (direction, чанк)

    def load(self):
        self.loaded = True

    @property
    def max_source_tokens(self):
        return 10 ** 6

    def split_sentence(self, sentence, direction):
        self.split_calls.append((direction, sentence))
        n = self.chunks_per_unit
        if n <= 1:
            return [sentence]
        size = max(1, len(sentence) // n)
        return [sentence[i:i + size] for i in range(0, len(sentence), size)]

    def translate_chunk(self, chunk, direction):
        self.chunk_calls.append((direction, chunk))
        return "[%s:%s]" % (direction, chunk)


class BoomBackend(FakeBackend):
    """Backend с ошибкой inference (проверка: ошибка не кэшируется)."""

    def translate_chunk(self, chunk, direction):
        self.chunk_calls.append((direction, chunk))
        raise RuntimeError("boom")


class FakeSnapshot:
    """Срез словаря: точный lookup всего текста (как настоящий)."""

    def __init__(self, entries=None):
        self.entries = entries or {}

    def lookup(self, text):
        return self.entries.get(text)


class ModelService(TranslationService):
    """Сервис с фиксированной идентичностью модели — аналог
    OfflineTranslator(model_id=...): model_identity — заданный id."""

    def __init__(self, backend, model_id, **kwargs):
        super().__init__(backend, **kwargs)
        self._model = model_id

    @property
    def model_identity(self):
        return self._model


def make_service(backend=None, model_id="model-a", entries=None,
                 chunks_per_unit=1):
    backend = backend or FakeBackend(chunks_per_unit)
    svc = ModelService(backend, model_id,
                       snapshot_loader=lambda path: FakeSnapshot(entries))
    return svc


def run_stream(svc, text, direction):
    """translate_stream + учёт событий (phase, done, total, src,
    translation, new_paragraph)."""
    events = []

    def on_sentence(phase, done, total, unit):
        events.append((phase, done, total, unit.src, unit.translation,
                       unit.new_paragraph))

    result = svc.translate_stream(text, direction, on_sentence)
    return result, events



# =====================================================================
#  1. TranslationCache: ключ и операции
# =====================================================================
c = TranslationCache()
k1 = c.make_key("m1", "en-ru", "Hello.")
k2 = c.make_key("m1", "en-ru", "Hello.")
k3 = c.make_key("m2", "en-ru", "Hello.")
k4 = c.make_key("m1", "ru-en", "Hello.")
k5 = c.make_key("m1", "en-ru", "Other.")
check("key: одинаковые поля — одинаковый ключ", k1 == k2)
check("key: другой model_id — другой ключ", k1 != k3)
check("key: другое направление — другой ключ", k1 != k4)
check("key: другой текст — другой ключ", k1 != k5)
c.put(k1, "Привет.")
check("put/get: значение возвращается", c.get(k1) == "Привет.")
check("get: отсутствующий ключ — None", c.get(k3) is None)
check("contains: есть/нет", (k1 in c) and (k3 not in c))
check("len: одна запись", len(c) == 1)
c.clear()
check("clear: пусто", len(c) == 0 and c.get(k1) is None)

# =====================================================================
#  2. Первый перевод — все промахи; повтор — все попадания
# =====================================================================
backend = FakeBackend()
svc = make_service(backend)
text1 = "One. Two. Three. Four."
out1 = svc.translate(text1, "en-ru")
check("first: 4 юнита — 4 вызова split/translate в backend",
      len(backend.split_calls) == 4 and len(backend.chunk_calls) == 4,
      (backend.split_calls, backend.chunk_calls))
check("first: порядок и текст вывода",
      out1 == "[en-ru:One.] [en-ru:Two.] [en-ru:Three.] [en-ru:Four.]", out1)
check("first: 4 записи в кэше", len(svc.translation_cache) == 4)

out2 = svc.translate(text1, "en-ru")
check("repeat: backend не вызывается (все попадания)",
      len(backend.split_calls) == 4 and len(backend.chunk_calls) == 4,
      (backend.split_calls[4:], backend.chunk_calls[4:]))
check("repeat: тот же вывод", out2 == out1)

# =====================================================================
#  3. Изменение исходного текста — только изменённые/новые юниты
# =====================================================================
out3 = svc.translate("One. Two. Three.", "en-ru")
check("del-last: backend не вызывается", len(backend.split_calls) == 4)
check("del-last: вывод по текущему тексту",
      out3 == "[en-ru:One.] [en-ru:Two.] [en-ru:Three.]", out3)

out4 = svc.translate("One. Three. Four.", "en-ru")
check("del-mid: backend не вызывается", len(backend.split_calls) == 4)
check("del-mid: порядок вывода сохранён",
      out4 == "[en-ru:One.] [en-ru:Three.] [en-ru:Four.]", out4)

before = len(backend.split_calls)
out5 = svc.translate("One. Two. Three! Four.", "en-ru")
check("modify-1: ровно один вызов backend",
      len(backend.split_calls) - before == 1, backend.split_calls[before:])
check("modify-1: вызов — только изменённый юнит",
      backend.split_calls[before] == ("en-ru", "Three!"),
      backend.split_calls[before:])
check("modify-1: вывод",
      out5 == "[en-ru:One.] [en-ru:Two.] [en-ru:Three!] [en-ru:Four.]", out5)

before = len(backend.split_calls)
out6 = svc.translate("One. Two. Three! Four. Five.", "en-ru")
check("add-1: ровно один вызов backend",
      len(backend.split_calls) - before == 1, backend.split_calls[before:])
check("add-1: вызов — только новый юнит",
      backend.split_calls[before] == ("en-ru", "Five."),
      backend.split_calls[before:])
check("add-1: вывод", out6 == out5 + " [en-ru:Five.]", out6)

before = len(backend.split_calls)
out7 = svc.translate("Five. Three! One. Two. Four.", "en-ru")
check("reorder: backend не вызывается (позиция не в ключе)",
      len(backend.split_calls) == before)
check("reorder: вывод в НОВОМ порядке",
      out7 == ("[en-ru:Five.] [en-ru:Three!] [en-ru:One.]"
               " [en-ru:Two.] [en-ru:Four.]"), out7)


# =====================================================================
#  4. Смена направления — все промахи
# =====================================================================
before = len(backend.split_calls)
out8 = svc.translate("One. Two.", "ru-en")
check("direction: все юниты — промахи",
      len(backend.split_calls) - before == 2, backend.split_calls[before:])
check("direction: вывод другого направления",
      out8 == "[ru-en:One.] [ru-en:Two.]", out8)
before = len(backend.split_calls)
check("direction: повтор — попадания",
      svc.translate("One. Two.", "ru-en") == out8
      and len(backend.split_calls) == before)

# =====================================================================
#  5. model_id в ключе; дубликаты юнитов
# =====================================================================
backend_b = FakeBackend()
svc_b = make_service(backend_b, model_id="model-b")
out9 = svc_b.translate("One. Two.", "en-ru")
check("model-b: свой экземпляр — все промахи",
      len(backend_b.split_calls) == 2 and out9 == "[en-ru:One.] [en-ru:Two.]")
kA = svc.translation_cache.make_key("model-a", "en-ru", "One.")
kB = svc.translation_cache.make_key("model-b", "en-ru", "One.")
check("model: ключи разных моделей различаются", kA != kB)
check("model: запись model-a не отдаётся под ключом model-b",
      svc.translation_cache.get(kA) == "[en-ru:One.]"
      and svc.translation_cache.get(kB) is None)

backend_d = FakeBackend()
svc_d = make_service(backend_d)
out10 = svc_d.translate("One. Two. One.", "en-ru")
check("duplicates: 3 юнита — 2 вызова backend",
      len(backend_d.split_calls) == 2 and len(backend_d.chunk_calls) == 2,
      (backend_d.split_calls, backend_d.chunk_calls))
check("duplicates: вывод по всем трём",
      out10 == "[en-ru:One.] [en-ru:Two.] [en-ru:One.]", out10)
check("duplicates: одна общая запись в кэше", len(svc_d.translation_cache) == 2)

# =====================================================================
#  6. Границы абзацев
# =====================================================================
backend_p = FakeBackend()
svc_p = make_service(backend_p)
ptext = "One. Two.\n\nThree. Four."
out11 = svc_p.translate(ptext, "en-ru")
check("paragraphs: \"\\n\\n\" между абзацами",
      out11 == "[en-ru:One.] [en-ru:Two.]\n\n[en-ru:Three.] [en-ru:Four.]",
      out11)
out11b = svc_p.translate(ptext, "en-ru")
check("paragraphs: все попадания — тот же вывод (границы сохранены)",
      out11b == out11)
units = split_units(ptext)
check("paragraphs: new_paragraph у первого юнита второго абзаца",
      [u.new_paragraph for u in units] == [False, False, True, False],
      [(u.text, u.new_paragraph) for u in units])

# =====================================================================
#  7. Мультичанковый юнит
# =====================================================================
backend_m = FakeBackend(chunks_per_unit=2)
svc_m = make_service(backend_m)
out12 = svc_m.translate("Hello.", "en-ru")
check("chunks: промах — 1 split и 2 translate_chunk",
      len(backend_m.split_calls) == 1 and len(backend_m.chunk_calls) == 2,
      (backend_m.split_calls, backend_m.chunk_calls))
check("chunks: чанки склеены",
      out12 == "[en-ru:Hel] [en-ru:lo.]", out12)
before_split = len(backend_m.split_calls)
before_chunk = len(backend_m.chunk_calls)
out12b = svc_m.translate("Hello.", "en-ru")
check("chunks: попадание — backend не вызывается",
      len(backend_m.split_calls) == before_split
      and len(backend_m.chunk_calls) == before_chunk)
check("chunks: тот же вывод", out12b == out12)



# =====================================================================
#  8. Стриминг: события, финал, смешанные попадания/промахи
# =====================================================================
backend_s = FakeBackend()
svc_s = make_service(backend_s)
stext = "One. Two.\n\nThree. Four."
res1, ev1 = run_stream(svc_s, stext, "en-ru")
check("stream: все 4 юнита — промахи", len(backend_s.split_calls) == 4)
check("stream: 8 событий, чередование start/done",
      [e[0] for e in ev1] == ["start", "done"] * 4, ev1)
check("stream: done/total в событиях",
      all(e[2] == 4 for e in ev1)
      and [e[1] for e in ev1] == [0, 1, 1, 2, 2, 3, 3, 4],
      [e[1] for e in ev1])
check("stream: start — перевод пуст, done — заполнен",
      all(e[4] == "" for e in ev1[0::2])
      and [e[4] for e in ev1[1::2]]
      == ["[en-ru:One.]", "[en-ru:Two.]", "[en-ru:Three.]",
          "[en-ru:Four.]"], ev1)
check("stream: new_paragraph в событиях (оба уровня)",
      [e[5] for e in ev1] == [False, False, False, False, True, True,
                              False, False], ev1)
check("stream: финал == translate()", res1 == svc_s.translate(stext, "en-ru"))
check("stream: финал == assemble_output",
      res1 == assemble_output(split_units(stext),
                              ["[en-ru:One.]", "[en-ru:Two.]",
                               "[en-ru:Three.]", "[en-ru:Four.]"]), res1)

before = len(backend_s.split_calls)
res2, ev2 = run_stream(svc_s, "One. Two.\n\nThree! Four.", "en-ru")
check("stream-mix: ровно один вызов backend",
      len(backend_s.split_calls) - before == 1, backend_s.split_calls[before:])
check("stream-mix: вызов — только изменённый юнит",
      backend_s.split_calls[before] == ("en-ru", "Three!"),
      backend_s.split_calls[before:])
check("stream-mix: порядок юнитов в done-событиях",
      [e[3] for e in ev2[1::2]] == ["One.", "Two.", "Three!", "Four."],
      [e[3] for e in ev2])
check("stream-mix: переводы в done-событиях",
      [e[4] for e in ev2[1::2]]
      == ["[en-ru:One.]", "[en-ru:Two.]", "[en-ru:Three!]",
          "[en-ru:Four.]"], ev2)
check("stream-mix: последовательность start/done",
      [e[0] for e in ev2] == ["start", "done"] * 4
      and [e[1] for e in ev2] == [0, 1, 1, 2, 2, 3, 3, 4], ev2)
check("stream-mix: разделитель абзацев в финале",
      res2 == "[en-ru:One.] [en-ru:Two.]\n\n[en-ru:Three!] [en-ru:Four.]",
      res2)

# =====================================================================
#  9. Словарь: приоритет до кэша, в кэш не попадает
# =====================================================================
backend_x = FakeBackend()
entries = {"One. Two.": "custom translation"}
svc_x = make_service(backend_x, entries=entries)
outx = svc_x.translate("One. Two.", "en-ru")
check("dict: точный текст — результат словаря, backend не вызывается",
      outx == "custom translation" and len(backend_x.split_calls) == 0)
check("dict: в кэш ничего не сохранено", len(svc_x.translation_cache) == 0)
outx2 = svc_x.translate("One. Two.", "en-ru")
check("dict: повтор — снова словарь, backend не вызывается",
      outx2 == "custom translation" and len(backend_x.split_calls) == 0)
entries.clear()
outx3 = svc_x.translate("One. Two.", "en-ru")
check("dict: запись убрана — backend вызывается",
      len(backend_x.split_calls) == 2, backend_x.split_calls)
check("dict: вывод после промаха",
      outx3 == "[en-ru:One.] [en-ru:Two.]", outx3)



# =====================================================================
#  10. Базовый сервис (model_identity — None), ошибки, пустой текст
# =====================================================================
backend_e0 = FakeBackend()
svc_plain = TranslationService(backend_e0,
                               snapshot_loader=lambda path: FakeSnapshot())
check("plain: model_identity — None", svc_plain.model_identity is None)
check("plain: ключ с None-моделью отличен от ключа с id",
      svc_plain.translation_cache.make_key(None, "en-ru", "Hi.")
      != svc_plain.translation_cache.make_key("m", "en-ru", "Hi."))
outp1 = svc_plain.translate("Hi.", "en-ru")
check("plain: перевод", outp1 == "[en-ru:Hi.]", outp1)
check("plain: повтор — попадание",
      svc_plain.translate("Hi.", "en-ru") == outp1
      and len(backend_e0.split_calls) == 1)

backend_b2 = BoomBackend()
svc_b2 = make_service(backend_b2)
err = expect_translation_error(svc_b2.translate, "One.", "en-ru")
check("error: формат 'Ошибка перевода:'",
      err == "Ошибка перевода: boom", err)
check("error: в кэш ничего не попало", len(svc_b2.translation_cache) == 0)
err2 = expect_translation_error(svc_b2.translate, "One.", "en-ru")
check("error: повтор снова идёт в backend (не кэшируется)",
      err2 == err and len(backend_b2.split_calls) == 2,
      (err2, backend_b2.split_calls))

backend_e = FakeBackend()
svc_e = make_service(backend_e)
res_e, ev_e = run_stream(svc_e, "   ", "en-ru")
check("empty: translate — ''", svc_e.translate("   ", "en-ru") == "")
check("empty: stream — '', без событий и вызовов",
      res_e == "" and ev_e == [] and len(backend_e.split_calls) == 0)

print("OK: %d checks passed" % len(PASS))

