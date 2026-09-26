# -*- coding: utf-8 -*-
"""Этап 6: REAL smoke test GGUF-бэкенда (отдельный от regression suite).

Запускается ТОЛЬКО если задан путь к локальной GGUF-модели:

    OFFLINE_TRANSLATOR_GGUF=/path/to/Hy-MT2-1.8B-Q4_K_M.gguf \
        python tests/smoke_gguf.py

Если переменная не задана (или llama-cpp-python не установлен) —
печатает SKIP и завершается с кодом 0 (НЕ failure): обычный
regression suite (tests/test_llama_cpp_backend.py) не требует
наличия GGUF-модели.

POC-модель: tencent/Hy-MT2-1.8B (GGUF tencent/Hy-MT2-1.8B-GGUF).

Проверяем минимум (качество числовым score НЕ оцениваем — фактический
результат записывается в вывод):
- перевод завершается, исключений нет;
- вывод не пустой;
- направление: для en-ru в выводе есть кириллица, для ru-en — латиница;
- translate() работает (оба направления, короткий и длинный текст);
- translate_stream() работает и final output == translate() output.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILURES = []


def check(name, cond, extra=""):
    mark = "PASS" if cond else "FAIL"
    line = "%s %s" % (mark, name)
    if extra and not cond:
        line += " | %s" % str(extra)[:400]
    print(line, flush=True)
    if not cond:
        FAILURES.append(name)


def has_cyrillic(s):
    return any("а" <= c <= "я" or "А" <= c <= "Я" for c in s)


def has_latin(s):
    return any("a" <= c.lower() <= "z" for c in s)


def main():
    path = os.environ.get("OFFLINE_TRANSLATOR_GGUF")
    if not path:
        print("SKIP: OFFLINE_TRANSLATOR_GGUF не задана — реальный "
              "smoke test не запускается (regression suite не требует "
              "наличия GGUF-модели).")
        return 0
    if not os.path.isfile(path):
        print("FAIL: файл модели не найден: %r" % path)
        return 1
    try:
        import llama_cpp  # noqa: F401
    except ImportError as e:
        print("SKIP: llama-cpp-python не установлен (%s) — реальный "
              "smoke test не запускается." % e)
        return 0

    from translator import OfflineTranslator

    t = OfflineTranslator(backend="llama_cpp", gguf_path=path)
    print("Backend: %s | device: %s | max_source_tokens: %d | "
          "context_length: %d"
          % (type(t.backend).__name__, t.device, t.max_source_tokens,
             t.backend.context_length), flush=True)

    # --- EN -> RU: короткий ---
    out = t.translate("Hello, how are you?", "en-ru")
    print("\n[EN->RU] 'Hello, how are you?'")
    print("  -> %r" % out, flush=True)
    check("en_ru_short_non_empty", bool(out.strip()), out)
    check("en_ru_short_cyrillic", has_cyrillic(out), out)

    # --- RU -> EN: короткий ---
    out = t.translate("Привет, как у тебя дела?", "ru-en")
    print("\n[RU->EN] 'Привет, как у тебя дела?'")
    print("  -> %r" % out, flush=True)
    check("ru_en_short_non_empty", bool(out.strip()), out)
    check("ru_en_short_latin", has_latin(out), out)

    # --- EN -> RU: длинный (3 предложения) ---
    long_en = ("Hello world. How are you? "
               "I am testing an offline translation model. "
               "This application must work without an internet connection.")
    out_long = t.translate(long_en, "en-ru")
    print("\n[EN->RU long]")
    print("  -> %r" % out_long, flush=True)
    check("en_ru_long_non_empty", bool(out_long.strip()), out_long)
    check("en_ru_long_cyrillic", has_cyrillic(out_long), out_long)

    # --- translate_stream: final == translate ---
    events = []
    final = t.translate_stream(
        long_en, "en-ru",
        on_sentence=lambda ph, d, tt, u: events.append((ph, d, tt)))
    print("\n[EN->RU long, stream]")
    print("  -> %r" % final, flush=True)
    print("  events: %s" % (events,), flush=True)
    check("stream_final_equals_translate", final == out_long,
          "stream=%r translate=%r" % (final, out_long))
    starts = sum(1 for e in events if e[0] == "start")
    dones = sum(1 for e in events if e[0] == "done")
    # В long_en 4 логических предложения (split_units) — 4 start/done.
    check("stream_events_balanced", starts == dones and starts == 4,
          str(events))

    # --- RU -> EN: длинный ---
    long_ru = ("Привет, мир. Как дела? "
               "Я тестирую офлайн-модель перевода. "
               "Это приложение должно работать без подключения "
               "к интернету.")
    out = t.translate(long_ru, "ru-en")
    print("\n[RU->EN long]")
    print("  -> %r" % out, flush=True)
    check("ru_en_long_non_empty", bool(out.strip()), out)
    check("ru_en_long_latin", has_latin(out), out)

    print()
    if FAILURES:
        print("SMOKE RESULT: FAIL (%d): %s" % (len(FAILURES), FAILURES))
        return 1
    print("SMOKE RESULT: OK — все проверки пройдены, фактические "
          "результаты зафиксированы выше.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
