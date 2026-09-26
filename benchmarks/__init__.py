# -*- coding: utf-8 -*-
"""Локальный benchmark OfflineTranslator (Этап 7): Marian vs Hy-MT2 GGUF.

Пакет содержит только измерения поверх существующего production API
(OfflineTranslator); production-код benchmark не меняет. Бенчмарк
показывает данные (latency, memory, structural checks, quality на
reference) и НЕ делает выводов «победитель»/рейтинг.

Точки запуска:
    python benchmarks/run_benchmark.py                # все доступные backend
    OFFLINE_TRANSLATOR_GGUF=/path/to/model.gguf \
        python benchmarks/run_benchmark.py --backend llama_cpp

См. benchmarks/README.md (методика, ограничения, воспроизводимость).
"""
