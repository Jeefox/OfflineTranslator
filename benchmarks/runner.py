# -*- coding: utf-8 -*-
"""Benchmark runner: измерения поверх production API (OfflineTranslator).

Всё переводится через translator.OfflineTranslator — ту же точку
входа, что и GUI. Benchmark не меняет production behavior и не
копирует внутреннюю логику бэкендов:

- model_load_time_ms — время конструирования OfflineTranslator
  (backend.load() вызывается в __init__, поэтому конструирование и
  включает загрузку моделей; холодный старт процесса и загрузка
  моделей НЕ смешиваются с latency перевода);
- latency_ms — wall-time одного вызова translate(text, direction);
- warm-up — несколько переводов до основного прогона, результаты
  warm-up в метрики не входят;
- units/chunks — те же публичные функции, что использует сервис
  (sentence_pipeline.split_units + backend.split_sentence);
- source_token_count — read-only доступ к СВОЕМУ токенизатору
  бэкенда (Marian: HF-токенизатор направления; llama_cpp:
  токенизатор GGUF); при любой ошибке — None (benchmark не
  прерывается, метрика помечается недостоверной);
- словарь — после конструирования dictionary_path переадресуется
  (атрибут экземпляра, production-код не тронут) на несуществующий
  файл: snapshot = None, измеряется «чистая» модель, без словарных
  оверраидов и без влияния пользовательского dictionary.json.

Каждый backend запускается в ОТДЕЛЬНОМ процессе (run_benchmark.py
--backend X): per-backend RSS честный (peak процесса одного бэкенда),
а сбой одного бэкенда не убивает другой.
"""
import importlib.metadata
import os
import re
import resource
import tempfile
import time

from benchmarks import dataset as bdataset
from benchmarks import metrics as m

#: Префикс, который TranslationService.translate() возвращает при
#: ошибке бэкенда (production-контракт; translate() не бросает, а
#: возвращает «Ошибка перевода: ...»).
ERROR_PREFIX = "Ошибка перевода: "


# -------------------------------------------------------------------- #
#  Окружение (для отчёта)                                               #
# -------------------------------------------------------------------- #
def _os_release():
    try:
        with open("/etc/os-release", encoding="utf-8") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return None


def _cpu_model():
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return None


def _ram_total_mb():
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, IndexError):
        pass
    return None


def collect_environment():
    """Окружение процесса: OS/Python/CPU/RAM + версии runtime-пакетов
    (llama-cpp-python/transformers/torch; None, если не установлены).

    Имя дистрибутива llama-cpp-python («llama-cpp-python») отличается
    от имени модуля («llama_cpp») — проверяются оба."""
    import platform
    candidates = {
        "llama_cpp": ("llama-cpp-python", "llama_cpp"),
        "transformers": ("transformers",),
        "torch": ("torch",),
    }
    versions = {}
    for name, dist_names in candidates.items():
        ver = None
        for dist in dist_names:
            try:
                ver = importlib.metadata.version(dist)
                break
            except Exception:
                continue
        versions[name] = ver
    return {
        "os": platform.system(),
        "os_release": _os_release(),
        "python": platform.python_version(),
        "cpu": _cpu_model(),
        "cpu_count": os.cpu_count(),
        "ram_total_mb": _ram_total_mb(),
        "versions": versions,
    }


# -------------------------------------------------------------------- #
#  Memory (Linux, stdlib)                                               #
# -------------------------------------------------------------------- #
def current_rss_kb():
    """Текущий RSS процесса из /proc/self/status (kB; None — если
    недоступно, например, не Linux)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


def peak_rss_kb():
    """Peak RSS процесса (ru_maxrss; на Linux — kB)."""
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


# -------------------------------------------------------------------- #
#  Production facade                                                    #
# -------------------------------------------------------------------- #
def create_translator(backend_name, gguf_path=None):
    """Создаёт OfflineTranslator (конструирование == загрузка моделей:
    TranslationService.__init__ вызывает backend.load()).

    Marian — существующий механизм кэша (обычный OfflineTranslator());
    llama_cpp — явный локальный путь к GGUF (скачивания нет).
    """
    from translator import OfflineTranslator  # лениво: torch/llama_cpp
    if backend_name == "marian":
        return OfflineTranslator()
    if backend_name == "llama_cpp":
        if not gguf_path:
            raise RuntimeError(
                "backend 'llama_cpp': не задан путь к GGUF "
                "(установите переменную окружения OFFLINE_TRANSLATOR_GGUF)")
        return OfflineTranslator(backend="llama_cpp", gguf_path=gguf_path)
    raise ValueError("неизвестный backend %r" % backend_name)


def resolve_gguf_path():
    """Путь к GGUF из окружения (или None)."""
    return os.environ.get("OFFLINE_TRANSLATOR_GGUF") or None


def gguf_availability():
    """(доступен, причина) — для parent-режима запуска."""
    path = resolve_gguf_path()
    if not path:
        return False, "не задана переменная OFFLINE_TRANSLATOR_GGUF"
    if not os.path.isfile(path):
        return False, "GGUF-файл не найден: %r" % path
    return True, None


def describe_model(translator, backend_name):
    """Описание загруженной(ых) модели(ей) для отчёта (только факты)."""
    if backend_name == "marian":
        from backends.marian import MarianBackend
        return {
            "backend": "marian",
            "runtime": "transformers (MarianMT, model.generate)",
            "models": dict(MarianBackend.DIRECTIONS),
            "directions": list(MarianBackend.DIRECTIONS),
            "device": translator.device,
            "cache_dir": (translator.cache_manager.cache_dir
                          if translator.cache_manager else None),
            "max_source_tokens": translator.max_source_tokens,
        }
    backend = translator.backend
    fname = os.path.basename(backend.model_path)
    match = re.search(r"(Q\d+[A-Z0-9_]*)", fname)
    return {
        "backend": "llama_cpp",
        "runtime": "llama-cpp-python (CPU, n_gpu_layers=0)",
        "gguf_file": fname,
        "gguf_path": backend.model_path,
        "quantization": (match.group(1) if match else "не определён из имени"),
        "context_length": backend.context_length,
        "max_output_tokens": backend.max_output_tokens,
        "device": translator.device,
        "max_source_tokens": translator.max_source_tokens,
    }


def isolate_dictionary(translator):
    """Benchmark измеряет «чистую» модель: словарь переадресуется на
    несуществующий файл (load_snapshot вернёт None). Атрибут
    экземпляра; production-код и файл пользователя не меняются."""
    tmp = tempfile.mkdtemp(prefix="oflt_bench_dict_")
    translator.dictionary_path = os.path.join(tmp, "no_dictionary.json")


def count_source_tokens(translator, backend_name, direction, source):
    """Количество входных токенов СВОИМ токенизатором бэкенда
    (read-only, только для измерения). None — если недоступно
    (benchmark не прерывается)."""
    try:
        if backend_name == "marian":
            from backends.marian import count_tokens
            _model, tokenizer = translator.backend._models[direction]
            return count_tokens(tokenizer, source)
        if backend_name == "llama_cpp":
            return translator.backend._count_tokens(source)
    except Exception:
        return None
    return None


def count_units_chunks(translator, direction, source):
    """(units, chunks): логические юниты (split_units) и inference-чанки
    (backend.split_sentence) — те же публичные функции, что использует
    TranslationService внутри translate(). None при ошибке."""
    try:
        from sentence_pipeline import split_units
        units = split_units(source)
        chunks = sum(len(translator.backend.split_sentence(u.text, direction))
                     for u in units)
        return len(units), chunks
    except Exception:
        return None, None



def warmup(translator):
    """Короткий warm-up перед основным прогоном (в метрики НЕ входит):
    по одному короткому переводу в каждом направлении + split.
    Возвращает время в мс."""
    from sentence_pipeline import split_units
    t0 = time.perf_counter()
    translator.translate("Hello.", "en-ru")
    translator.translate("Привет.", "ru-en")
    sample = "This is a warm-up sentence for the benchmark."
    for unit in split_units(sample):
        translator.backend.split_sentence(unit.text, "en-ru")
    return (time.perf_counter() - t0) * 1000.0


def run_main_pass(translator, backend_name, examples):
    """Основной warm-proгон: translate() на каждом примере dataset.

    Успех определяется production-контрактом: translate() при ошибке
    бэкенда возвращает строку с префиксом ERROR_PREFIX (не бросает).
    """
    samples = []
    for ex in examples:
        direction = ex["direction"]
        source = ex["source"]
        t0 = time.perf_counter()
        output = translator.translate(source, direction)
        latency_ms = (time.perf_counter() - t0) * 1000.0
        output = output if isinstance(output, str) else str(output)
        success = bool(output.strip()) and not output.startswith(ERROR_PREFIX)
        units, chunks = count_units_chunks(translator, direction, source)
        samples.append({
            "id": ex["id"],
            "direction": direction,
            "category": ex["category"],
            "source": source,
            "reference": ex.get("reference") or None,
            "exact": bool(ex.get("exact", False)),
            "success": success,
            "output": output,
            "error": None if success else output,
            "latency_ms": round(latency_ms, 3),
            "source_length_chars": len(source),
            "output_length_chars": len(output),
            "source_token_count": count_source_tokens(
                translator, backend_name, direction, source),
            "units": units,
            "chunks": chunks,
            "structural": m.structural_checks(source, output, direction),
        })
    return samples


# Фиксированные подвыборки (заданы ДО запуска, не по результатам).
DETERMINISM_IDS = (["en%03d" % i for i in range(1, 11)]
                   + ["ru%03d" % i for i in range(1, 11)])
STREAMING_IDS = (["en%03d" % i for i in range(11, 21)]
                 + ["ru%03d" % i for i in range(11, 21)])


def run_determinism(translator, examples, run1_samples):
    """Повторный прогон фиксированной подвыборки (20 примеров) и
    сравнение с первым прогоном: output_run_1 == output_run_2.

    Настройки (sampling/temperature) НЕ меняются — фиксируется факт.
    """
    ids = set(DETERMINISM_IDS)
    by_id = {s["id"]: s for s in run1_samples}
    checked, mismatches = 0, []
    for ex in examples:
        if ex["id"] not in ids:
            continue
        checked += 1
        out2 = translator.translate(ex["source"], ex["direction"])
        first = by_id.get(ex["id"])
        if first is None or out2 != first["output"]:
            mismatches.append({
                "id": ex["id"],
                "run1": first["output"] if first else None,
                "run2": out2,
            })
    return {
        "subset": list(DETERMINISM_IDS),
        "checked": checked,
        "mismatches": mismatches,
        "all_match": not mismatches,
    }



def run_streaming(translator, examples):
    """Streaming regression-check на фиксированной подвыборке
    (10 EN→RU + 10 RU→EN): translate() == final output translate_stream(),
    события start/done корректны, порядок корректен, финал не пуст.

    Это проверка архитектуры (не latency-метрика).
    """
    ids = set(STREAMING_IDS)
    results = []
    for ex in examples:
        if ex["id"] not in ids:
            continue
        events = []

        def on_sentence(phase, done, total, unit, _events=events):
            _events.append([phase, done, total])

        try:
            from sentence_pipeline import split_units
            expected_total = len(split_units(ex["source"]))
            final = translator.translate_stream(
                ex["source"], ex["direction"], on_sentence)
        except Exception as e:
            results.append({
                "id": ex["id"], "ok": False,
                "reason": "исключение: %s: %s" % (type(e).__name__, e),
                "n_events": len(events),
            })
            continue
        reference = translator.translate(ex["source"], ex["direction"])
        problems = []
        if not (final or "").strip():
            problems.append("пустой финальный вывод")
        if final != reference:
            problems.append("final != translate()")
        seq_ok = (
            len(events) == 2 * expected_total
            and all(ev[2] == expected_total for ev in events)
            and [ev[0] for ev in events] == ["start", "done"] * expected_total
            and [ev[1] for ev in events]
            == [j // 2 + (1 if j % 2 else 0) for j in range(2 * expected_total)]
        )
        if not seq_ok:
            problems.append("неверная последовательность событий: %r" % events)
        results.append({
            "id": ex["id"],
            "ok": not problems,
            "reason": "; ".join(problems) if problems else None,
            "total_units": expected_total,
            "n_events": len(events),
        })
    ok_count = sum(1 for r in results if r["ok"])
    return {
        "subset": list(STREAMING_IDS),
        "checked": len(results),
        "ok": ok_count,
        "failed": len(results) - ok_count,
        "all_ok": ok_count == len(results) and bool(results),
        "failures": [r for r in results if not r["ok"]],
    }


# -------------------------------------------------------------------- #
#  Полный прогон одного backend (отдельный процесс)                     #
# -------------------------------------------------------------------- #
def run_backend(backend_name, examples=None):
    """Полное измерение одного backend (предназначено для отдельного
    процесса). Возвращает JSON-сериализуемый dict результатов."""
    env = collect_environment()
    rss_before = current_rss_kb()
    gguf_path = resolve_gguf_path() if backend_name == "llama_cpp" else None
    t0 = time.perf_counter()
    translator = create_translator(backend_name, gguf_path=gguf_path)
    load_time_ms = (time.perf_counter() - t0) * 1000.0
    rss_after = current_rss_kb()
    isolate_dictionary(translator)
    warmup_ms = warmup(translator)
    if examples is None:
        examples = bdataset.load_dataset()
    samples = run_main_pass(translator, backend_name, examples)
    determinism = run_determinism(translator, examples, samples)
    streaming = run_streaming(translator, examples)
    rss_peak = peak_rss_kb()
    return {
        "backend": backend_name,
        "env": env,
        "model": describe_model(translator, backend_name),
        "model_load_time_ms": round(load_time_ms, 1),
        "warmup_ms": round(warmup_ms, 1),
        "memory": {
            "before_rss_kb": rss_before,
            "after_rss_kb": rss_after,
            "peak_rss_kb": rss_peak,
            "notes": [
                "before/after — RSS процесса до/после конструирования "
                "(т.е. до/после загрузки модели; импорт torch/llama_cpp "
                "уже учтён в before)",
                "peak — ru_maxrss (пиковый RSS всего процесса, Linux kB): "
                "включает интерпретатор, импорты и сам benchmark; точная "
                "attribution «модель vs остальное» не делается",
            ],
        },
        "samples": samples,
        "aggregates": m.per_direction_aggregates(samples),
        "quality": m.compute_quality(samples),
        "structural": m.structural_summary(samples),
        "determinism": determinism,
        "streaming": streaming,
    }


def child_main(backend_name, out_path):
    """Режим одного backend: полный прогон + запись raw JSON.
    Возвращает код выхода (0 — прогон завершён, даже с failed
    sample'ами; != 0 — фатальная ошибка, например нет GGUF)."""
    try:
        result = run_backend(backend_name)
    except Exception as e:
        print("[bench:%s] ФАТАЛЬНО: %s: %s" % (backend_name, type(e).__name__, e))
        return 1
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        import json
        json.dump(result, f, ensure_ascii=False, indent=2)
    for direction in ("en-ru", "ru-en"):
        a = result["aggregates"][direction]
        print("[bench:%s] %s: n=%d ok=%d failed=%d avg=%.0fms median=%.0fms "
              "p95=%.0fms (load %.1fs, warmup %.1fms)"
              % (backend_name, direction, a["n"], a["success"], a["failed"],
                 a["avg_ms"] or 0, a["median_ms"] or 0, a["p95_ms"] or 0,
                 result["model_load_time_ms"] / 1000.0,
                 result["warmup_ms"] / 1000.0))
    det = result["determinism"]
    print("[bench:%s] determinism: %d/%d совпали"
          % (backend_name, det["checked"] - len(det["mismatches"]), det["checked"]))
    st = result["streaming"]
    print("[bench:%s] streaming: %d/%d ок" % (backend_name, st["ok"], st["checked"]))
    print("[bench:%s] результаты: %s" % (backend_name, out_path))
    return 0

