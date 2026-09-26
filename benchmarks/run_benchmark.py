#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Benchmark запуска: Marian vs Hy-MT2 GGUF (Этап 7, локальный).

Использование:

    # Все доступные backend'ы. Marian — всегда; GGUF — если задан
    # OFFLINE_TRANSLATOR_GGUF=/path/to/Hy-MT2-1.8B-Q4_K_M.gguf
    # (иначе backend честно помечается «unavailable» в отчёте):
    python benchmarks/run_benchmark.py

    # Один backend (raw JSON в benchmarks/results/):
    python benchmarks/run_benchmark.py --backend marian
    OFFLINE_TRANSLATOR_GGUF=/path/to/model.gguf \
        python benchmarks/run_benchmark.py --backend llama_cpp

Результаты: benchmarks/results/
    benchmark_<timestamp>.json   — полные данные (все backend'ы)
    benchmark_<timestamp>.csv    — по одному sample на строку
    benchmark_<timestamp>.md     — читаемый summary (без winner/ranking)
    benchmark_<timestamp>__marian.json / __llama_cpp.json — raw per-backend

Каждый backend выполняется в ОТДЕЛЬНОМ процессе: per-backend RSS/память
честные, а сбой одного backend'а не убивает другой. Parent-процесс
лёгкий (не импортирует torch/llama_cpp), тяжёлые импорты — только в
дочернем процессе.

Dataset фиксирован: benchmarks/data/dataset.jsonl (см. README.md).
"""
import argparse
import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks import dataset as bdataset  # noqa: E402
from benchmarks import report               # noqa: E402


def _timestamp():
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _parent(argv_ts=None):
    """Parent-режим: запустить каждый доступный backend в отдельном
    процессе, собрать raw JSON'ы, выдать merged JSON/CSV/MD."""
    from benchmarks import runner  # лёгкий импорт (тяжёлое — лениво)
    ts = argv_ts or _timestamp()
    results_dir = HERE / "results"
    results_dir.mkdir(exist_ok=True)

    backend_results = {}
    backends = ["marian"]
    ok, reason = runner.gguf_availability()
    if ok:
        backends.append("llama_cpp")
    else:
        backend_results["llama_cpp"] = {"available": False, "reason": reason}
        print("[bench] llama_cpp не запускается: %s" % reason)

    for name in backends:
        out = results_dir / ("benchmark_%s__%s.json" % (ts, name))
        print("=" * 70)
        print("[bench] backend: %s" % name)
        print("=" * 70)
        proc = subprocess.run(
            [sys.executable, str(HERE / "run_benchmark.py"),
             "--backend", name, "--out", str(out)],
            cwd=str(ROOT))
        if proc.returncode == 0 and out.is_file():
            with open(out, "r", encoding="utf-8") as f:
                backend_results[name] = json.load(f)
        else:
            backend_results[name] = {
                "available": False,
                "reason": "дочерний процесс завершился с кодом %d"
                          % proc.returncode,
            }

    examples = bdataset.load_dataset()
    merged = report.build_merged(
        runner.collect_environment(),
        str(bdataset.dataset_path()),
        bdataset.summary(examples),
        backend_results)
    json_path = results_dir / ("benchmark_%s.json" % ts)
    csv_path = results_dir / ("benchmark_%s.csv" % ts)
    md_path = results_dir / ("benchmark_%s.md" % ts)
    report.write_json(merged, str(json_path))
    n_csv = report.write_csv(merged, str(csv_path))
    report.write_markdown(merged, str(md_path))
    print("=" * 70)
    print("[bench] Готово. Результаты:")
    print("  %s" % json_path)
    print("  %s (%d строк)" % (csv_path, n_csv))
    print("  %s" % md_path)
    for name in backends:
        print("  %s" % (results_dir / ("benchmark_%s__%s.json" % (ts, name))))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="OfflineTranslator benchmark: Marian vs Hy-MT2 GGUF")
    parser.add_argument("--backend", choices=["marian", "llama_cpp", "all"],
                        default="all",
                        help="all (default) — все доступные; "
                             "marian/llama_cpp — один (raw JSON)")
    parser.add_argument("--out", default=None,
                        help="путь к raw JSON (только в одном backend'е)")
    args = parser.parse_args(argv)
    if args.backend == "all":
        return _parent()
    # Один backend: запускаем прямо в этом процессе (или из parent).
    from benchmarks import runner
    out = args.out or str(HERE / "results" / (
        "benchmark_%s__%s.json" % (_timestamp(), args.backend)))
    os.makedirs(os.path.dirname(out), exist_ok=True)
    return runner.child_main(args.backend, out)


if __name__ == "__main__":
    sys.exit(main())
