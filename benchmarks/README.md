# Benchmark: Marian vs Hy-MT2 GGUF (Этап 7)

Локальный, воспроизводимый benchmark OfflineTranslator на типичных для
приложения задачах (EN↔RU). **Главный результат — данные**: latency,
память, структурные свойства, качество (где есть reference),
детерминизм, streaming. Benchmark **не выбирает победителя** — нет
overall score, рейтинга и рекомендаций; интерпретация — за разработчиком.

## Что сравнивается

| | Marian | Hy-MT2 GGUF |
|---|---|---|
| backend | `marian` (default) | `llama_cpp` (Этап 6, POC) |
| модель | Helsinki-NLP/opus-mt-en-ru / opus-mt-ru-en | tencent/Hy-MT2-1.8B, `Hy-MT2-1.8B-Q4_K_M.gguf` |
| runtime | transformers (MarianMT), `model.generate` | llama-cpp-python, CPU (`n_gpu_layers=0`) |
| settings | production (num_beams=4, max_length=512) | POC (temperature=0.0, top_p=0.6, top_k=20, repeat_penalty=1.05, n_ctx=4096) |

Benchmark работает поверх **существующего production API**
(`translator.OfflineTranslator`) и **не меняет production-код**:
production-файлы на этом этапе не изменялись.

## Установка

Зависимостей **дополнительно нет** (только stdlib; chrF реализован на
stdlib). Нужны те же пакеты, что и у приложения (см. `requirements.txt`),
плюс `llama-cpp-python` — только для GGUF-backend.

```bash
source .venv/bin/activate          # окружение проекта
# Marian работает без llama-cpp-python; для GGUF нужен:
pip install llama-cpp-python
```

GGUF-модель скачивается **вручную** (автоматического скачивания нет):
`tencent/Hy-MT2-1.8B-GGUF` → `Hy-MT2-1.8B-Q4_K_M.gguf` (~1.1 ГБ).

## Запуск

```bash
# Marian + (если задан) GGUF — полный benchmark:
OFFLINE_TRANSLATOR_GGUF=/path/to/Hy-MT2-1.8B-Q4_K_M.gguf \
    python benchmarks/run_benchmark.py

# Только один backend (raw JSON в benchmarks/results/):
python benchmarks/run_benchmark.py --backend marian
OFFLINE_TRANSLATOR_GGUF=/path/to/Hy-MT2-1.8B-Q4_K_M.gguf \
    python benchmarks/run_benchmark.py --backend llama_cpp
```

Первый запуск Marian скачивает модели в `model_cache/`
(существующий механизм кэша приложения). Каждый backend выполняется в
**отдельном процессе**: per-backend RSS честный, сбой одного не убивает
другой. Без `OFFLINE_TRANSLATOR_GGUF` GGUF честно помечается
`unavailable` в отчёте.

Ориентировочное время: Marian — минуты (warm cache); Hy-MT2 1.8B CPU —
порядка 15–30 минут (200 основных + 20 determinism + 20 streaming
вызовов).

## Dataset

`benchmarks/data/dataset.jsonl` — **фиксирован** (зафиксирован до
запуска, не меняется по результатам моделей; неудобные примеры не
удаляются):

- 200 примеров: 100 EN→RU (`en001`–`en100`) + 100 RU→EN (`ru001`–`ru100`);
- 12 категорий: basic, conversational, technical, terminology, names,
  numbers, punctuation, urls, long_sentence, multi_sentence, ambiguity,
  formatting;
- поля: `id`, `direction`, `source`, `category`, опционально
  `reference` и `exact`.

`reference` — один корректный перевод (не «единственно правильный»;
для естественных фраз несколько вариантов допустимы). `exact: true` —
reference ожидается строгим (только такие примеры входят в метрику


## Результаты

`benchmarks/results/`:

- `benchmark_<ts>.json` — полные данные (все backend'ы, все sample'ы,
  события, память, окружение);
- `benchmark_<ts>.csv` — по одному sample на строку (основной прогон);
- `benchmark_<ts>.md` — читаемый summary;
- `benchmark_<ts>__marian.json`, `benchmark_<ts>__llama_cpp.json` —
  raw per-backend.

## Что измеряется

- **Per sample**: success/failure (production-контракт: `translate()`
  при ошибке возвращает «Ошибка перевода: …»), `latency_ms` (wall-time
  одного `translate()`), длины source/output, `source_token_count`
  (своим токенизатором бэкенда, read-only; None — если недоступно),
  `units` (split_units) и `chunks` (backend.split_sentence), structural
  checks, output/error.
- **Aggregate** (по направлению): N, success, failed, total/avg/median/
  **p95** (nearest-rank: ceil(0.95·n)-я величина)/min/max latency.
- **Load отдельно**: `model_load_time_ms` (конструирование
  `OfflineTranslator`, т.е. загрузка моделей) — НЕ смешивается с
  translation latency; затем короткий warm-up (в метрики не входит).
- **Memory** (Linux, stdlib): RSS до/после загрузки (VmRSS из
  `/proc/self/status`) и peak (ru_maxrss). Ограничение: peak — по всему
  процессу (интерпретатор + импорты + модель), атрибуция только к
  модели не делается.
- **Quality** — только для примеров с reference и только для успешных
  переводов: `chrF` (character n-gram F2, порядки 1–6, β²=2,
  stdlib-реализация) и exact match (только `exact: true` примеры,
  после нормализации whitespace, case-sensitive). **BLEU намеренно не
  используется**: sentence-BLEU на десятках коротких пар с одним
  reference нестабилен (brevity penalty + smoothing), а ради одной
  метрики лишняя инфраструктура не вводится.
- **Structural checks** (все примеры): output не пуст; не копия
  source; скрипт целевого языка присутствует; URL/пути, email,
  числа (digit-runs), проценты, code-токены (dotted-идентификаторы
  ≥3 частей, CLI-флаги) не исчезли; число строк сохраняется
  (если в source >1 строка). Это **структурные свойства, а не
  score**: `numbers_preserved=false` означает только, что число из
  source не найдено в output (могло быть переведено словами).
- **Determinism**: фиксированная подвыборка 20 примеров
  (en001–en010, ru001–ru010) переводится дважды, сравниваются
  выводы. Sampling **не меняется** при рассинхроне — фиксируется
  факт (для Hy-MT2 сохранён POC-режим temperature=0.0).
- **Streaming**: фиксированная подвыборка 10 EN→RU + 10 RU→EN
  (en011–en020, ru011–ru020): `translate() == final output
  translate_stream()`, последовательность start/done событий
  корректна, финал не пуст. Это regression check архитектуры, а не
  latency-метрика.

## Воспроизводимость

- dataset фиксирован в репозитории; подвыборки determinism/streaming и
  example-ID отчёта заданы константами **до** запуска;
- одна команда запуска после подготовки окружения (см. выше);
- timestamp используется только в именах файлов результатов;
- environment (OS/Python/CPU/RAM/версии runtime) сохраняется в JSON/MD.

## Тесты

```bash
python tests/test_benchmark.py
```

Проверяют сам framework **без реальных моделей**: dataset (загрузка/
схема/направления/категории/дубликаты/ошибки валидации), aggregate и
p95, chrF/exact match, structural checks, determinism/streaming
обработка (fake-переводчик), сериализация JSON/CSV/MD, handling
отсутствующего GGUF.

## Ограничения

- Marian: первый запуск скачивает модели (существующий механизм);
  latency Marian зависит от warm-состояния кэша (load отдельно).
- Hy-MT2: CPU-only, n_ctx=4096, temperature=0.0 (POC-режим; card
  рекомендует 0.7 — меняется одной правкой в `backends/prompts.py`,
  benchmark её не делает).
- peak RSS — по всему процессу, не только модели.
- `numbers_preserved` — сравнение digit-забега: числа, переведённые
  словами, считаются нарушением свойства (это факт, не приговор).
- Текущий пайплайн склеивает юниты предложения пробелом, поэтому
  `lines_preserved` на линейных вводах может легитимно «проваливаться»
  — свойство пайплайна, зафиксировано как данные.
- Отчёт не делает выводов «победитель» — это ограничение намеренное.

exact match). LLM-судей нет.