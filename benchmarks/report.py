# -*- coding: utf-8 -*-
"""Результаты benchmark: JSON (полные), CSV (по примеру строка),
Markdown (читаемый summary).

Markdown-отчёт содержит ТОЛЬКО измеренные данные и НЕ делает вывода
«какой backend лучше»/рейтинга — интерпретация за разработчиком.
"""
import csv
import json
import os

# Фиксированные ID для секции «Examples» (по одному на категорию,
# зафиксированы ДО запуска benchmark — НЕ выбираются по результатам).
EXAMPLE_IDS = (
    ["%s%03d" % (p, i) for p, i in (
        ("en", 1), ("en", 11), ("en", 21), ("en", 31), ("en", 39), ("en", 47),
        ("en", 55), ("en", 63), ("en", 71), ("en", 79), ("en", 87), ("en", 95))]
    + ["%s%03d" % (p, i) for p, i in (
        ("ru", 1), ("ru", 11), ("ru", 21), ("ru", 31), ("ru", 39), ("ru", 47),
        ("ru", 55), ("ru", 63), ("ru", 71), ("ru", 79), ("ru", 87), ("ru", 95))]
)

CSV_COLUMNS = [
    "backend", "id", "direction", "category", "success", "latency_ms",
    "source_length_chars", "output_length_chars", "source_token_count",
    "units", "chunks", "reference", "exact", "output", "error",
]


def write_json(obj, path):
    """Полные результаты в JSON (utf-8, без экранирования кириллицы)."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _backend_samples(merged):
    """{backend_name: {id: sample}} по доступным backend'ам."""
    out = {}
    for name, res in merged.get("backends", {}).items():
        if not isinstance(res, dict) or res.get("available") is False:
            continue
        out[name] = {s["id"]: s for s in res.get("samples", [])}
    return out


def build_merged(env, dataset_file, dataset_summary, backend_results):
    """Собирает merged-структуру (исход данных для JSON/CSV/MD).

    backend_results: {backend_name: result_dict | {"available": False,
    "reason": str}} — недоступный backend честно помечается,
    метрики НЕ подменяются.
    """
    examples = []
    by_backend = _backend_samples(
        {"backends": backend_results})
    for ex_id in EXAMPLE_IDS:
        row = {"id": ex_id}
        for name, by_id in by_backend.items():
            s = by_id.get(ex_id)
            row[name] = None if s is None else {
                "source": s["source"],
                "reference": s.get("reference"),
                "output": s["output"],
                "success": s["success"],
            }
        examples.append(row)
    return {
        "benchmark": "OfflineTranslator local benchmark (Stage 7): "
                     "Marian vs Hy-MT2 GGUF",
        "note": "Отчёт содержит только измеренные данные; вывод "
                "«победитель»/рейтинг/overall score не делается.",
        "env": env,
        "dataset": {"file": dataset_file, **dataset_summary},
        "backends": backend_results,
        "examples": examples,
    }


def write_csv(merged, path):
    """CSV: по одному sample-строке (основной прогон) на строку.
    Возвращает количество строк данных."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    rows = 0
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for name in sorted(merged.get("backends", {})):
            for s in _backend_samples(merged).get(name, {}).values():
                row = {c: s.get(c) for c in CSV_COLUMNS}
                row["backend"] = name
                writer.writerow(row)
                rows += 1
    return rows


def _md_cell(text):
    """Ячейка markdown-таблицы: без |, без переносов (per-line -> ' ⏎ ')."""
    if text is None:
        return "—"
    s = str(text).replace("|", "\\|")
    s = " ⏎ ".join(p for p in (seg.strip() for seg in s.splitlines()) if p)
    return s or "—"


def _fmt_ms(v):
    return "—" if v is None else "%.0f" % v


def _fmt_mb(v_kb):
    return "—" if v_kb is None else "%.0f" % (v_kb / 1024.0)


def _sec_environment(lines, env):
    lines.append("## Environment")
    lines.append("")
    lines.append("- OS: %s %s" % (env.get("os") or "—", env.get("os_release") or ""))
    lines.append("- Python: %s" % (env.get("python") or "—"))
    lines.append("- CPU: %s (logical cores: %s)"
                 % (env.get("cpu") or "n/a", env.get("cpu_count") or "n/a"))
    lines.append("- RAM total: %s MB" % (env.get("ram_total_mb") or "n/a"))
    for pkg in ("llama_cpp", "transformers", "torch"):
        ver = (env.get("versions") or {}).get(pkg)
        lines.append("- %s: %s" % (pkg, ver or "not installed"))
    lines.append("")


def _sec_models(lines, backends):
    lines.append("## Models")
    lines.append("")
    for name in ("marian", "llama_cpp"):
        res = backends.get(name)
        lines.append("### %s" % ("Marian" if name == "marian" else "Hy-MT2 GGUF"))
        lines.append("")
        if not isinstance(res, dict) or res.get("available") is False:
            reason = (res or {}).get("reason", "backend not run")
            lines.append("- unavailable: %s" % reason)
            lines.append("")
            continue
        model = res.get("model", {})
        if name == "marian":
            for direction in model.get("directions", []):
                lines.append("- %s: %s" % (direction, model.get("models", {}).get(direction)))
        else:
            lines.append("- GGUF: %s" % model.get("gguf_file"))
            lines.append("- quantization: %s (parsed from filename)" % model.get("quantization"))
            lines.append("- context (n_ctx): %s" % model.get("context_length"))
            lines.append("- max_output_tokens: %s" % model.get("max_output_tokens"))
        lines.append("- device: %s" % model.get("device"))
        if model.get("cache_dir"):
            lines.append("- cache: %s" % model.get("cache_dir"))
        lines.append("- max_source_tokens: %s" % model.get("max_source_tokens"))
        lines.append("- runtime: %s" % model.get("runtime"))
        lines.append("- model_load_time_ms: %s (cold, separate from translation latency)"
                     % _fmt_ms(res.get("model_load_time_ms")))
        lines.append("- warmup_ms (excluded from metrics): %s" % _fmt_ms(res.get("warmup_ms")))
        lines.append("")


def _sec_dataset(lines, ds):
    lines.append("## Dataset")
    lines.append("")
    lines.append("- file: %s (fixed before run)" % ds.get("file"))
    lines.append("- total: %s" % ds.get("total"))
    for direction in ("en-ru", "ru-en"):
        lines.append("- %s: %s" % (direction, ds.get("per_direction", {}).get(direction, 0)))
    lines.append("- categories:")
    for cat in ("basic", "conversational", "technical", "terminology", "names",
                "numbers", "punctuation", "urls", "long_sentence",
                "multi_sentence", "ambiguity", "formatting"):
        lines.append("  - %s: %s" % (cat, ds.get("per_category", {}).get(cat, 0)))
    lines.append("- with reference: %s (chrF); exact-flag reference: %s (exact match)"
                 % (ds.get("with_reference"), ds.get("exact_reference")))
    lines.append("")



def _sec_performance(lines, backends):
    lines.append("## Performance")
    lines.append("")
    lines.append("Warm inference (after model load and warm-up). Latency is")
    lines.append("the wall time of one translate() call; failed attempts are")
    lines.append("included in latency stats and counted in Failed.")
    lines.append("")
    lines.append("| Backend | Direction | N | Success | Failed | Avg ms | Median ms | P95 ms | Min ms | Max ms | Total s |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for name in ("marian", "llama_cpp"):
        res = backends.get(name)
        if not isinstance(res, dict) or res.get("available") is False:
            lines.append("| %s | — | — | unavailable | — | — | — | — | — | — | — |"
                         % ("Marian" if name == "marian" else "Hy-MT2 GGUF"))
            continue
        label = "Marian" if name == "marian" else "Hy-MT2 GGUF"
        for direction in ("en-ru", "ru-en"):
            a = res.get("aggregates", {}).get(direction, {})
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                label, direction,
                a.get("n", "—"), a.get("success", "—"), a.get("failed", "—"),
                _fmt_ms(a.get("avg_ms")), _fmt_ms(a.get("median_ms")),
                _fmt_ms(a.get("p95_ms")), _fmt_ms(a.get("min_ms")),
                _fmt_ms(a.get("max_ms")),
                "—" if a.get("total_ms") is None else "%.1f" % (a["total_ms"] / 1000.0),
            ))
    lines.append("")


def _sec_memory(lines, backends):
    lines.append("## Memory")
    lines.append("")
    lines.append("Per-backend process RSS (each backend runs in its own process;")
    lines.append("before/after — around model load; peak — ru_maxrss of the whole")
    lines.append("process, includes interpreter/imports; attribution to the model")
    lines.append("alone is not claimed).")
    lines.append("")
    lines.append("| Backend | Before MB | After Load MB | Peak MB | Load MB (after-before) |")
    lines.append("|---|---|---|---|---|")
    for name in ("marian", "llama_cpp"):
        res = backends.get(name)
        label = "Marian" if name == "marian" else "Hy-MT2 GGUF"
        if not isinstance(res, dict) or res.get("available") is False:
            lines.append("| %s | — | — | — | — |" % label)
            continue
        mem = res.get("memory", {})
        before, after = mem.get("before_rss_kb"), mem.get("after_rss_kb")
        delta = None
        if before is not None and after is not None:
            delta = after - before
        lines.append("| %s | %s | %s | %s | %s |" % (
            label, _fmt_mb(before), _fmt_mb(after),
            _fmt_mb(mem.get("peak_rss_kb")),
            "—" if delta is None else "%.0f" % (delta / 1024.0)))
    lines.append("")


def _sec_quality(lines, backends):
    lines.append("## Quality metrics")
    lines.append("")
    lines.append("Computed ONLY for samples with a reference (successful")
    lines.append("translations). chrF — character n-gram F2 (stdlib")
    lines.append("implementation); reference is one correct translation, not")
    lines.append("the only one. Exact match — only for exact-flag samples")
    lines.append("(deterministic references). BLEU is intentionally not used.")
    lines.append("")
    lines.append("| Backend | Direction | N(ref) | chrF mean | Exact (n) | Exact match |")
    lines.append("|---|---|---|---|---|---|")
    for name in ("marian", "llama_cpp"):
        res = backends.get(name)
        label = "Marian" if name == "marian" else "Hy-MT2 GGUF"
        if not isinstance(res, dict) or res.get("available") is False:
            lines.append("| %s | — | — | — | — | — |" % label)
            continue
        for direction in ("en-ru", "ru-en"):
            q = res.get("quality", {}).get(direction)
            if not q:
                lines.append("| %s | %s | 0 | — | 0 | — |" % (label, direction))
                continue
            lines.append("| %s | %s | %s | %s | %s | %s |" % (
                label, direction, q.get("n_ref", 0),
                "—" if q.get("chrF_mean") is None else "%.4f" % q["chrF_mean"],
                q.get("exact_total", 0),
                "%s/%s" % (q.get("exact_matched", 0), q.get("exact_total", 0))))
    lines.append("")



def _sec_structural(lines, backends):
    lines.append("## Structural checks")
    lines.append("")
    lines.append("Structural properties only (NOT a correctness score): a 'fail'")
    lines.append("means one specific property is violated (e.g. a number from")
    lines.append("the source is not found in the output, possibly translated")
    lines.append("into words). 'na' — the property is not applicable to the")
    lines.append("sample (no URL/email/number/etc. in the source).")
    lines.append("")
    headers = ["| Check |"]
    seps = ["|---|"]
    for name in ("marian", "llama_cpp"):
        label = "Marian" if name == "marian" else "Hy-MT2 GGUF"
        headers.append(" %s (pass/fail/na) |" % label)
        seps.append("---|")
    lines.append("".join(headers))
    lines.append("".join(seps))
    check_order = ["output_non_empty", "not_source_copy", "target_script",
                   "urls_preserved", "emails_preserved", "numbers_preserved",
                   "percent_preserved", "code_tokens_preserved",
                   "lines_preserved"]
    for check in check_order:
        row = ["| %s |" % check]
        for name in ("marian", "llama_cpp"):
            res = backends.get(name)
            if not isinstance(res, dict) or res.get("available") is False:
                row.append(" — |")
                continue
            s = res.get("structural", {}).get(check)
            if not s:
                row.append(" — |")
            else:
                row.append(" %d/%d/%d |" % (s.get("pass", 0), s.get("fail", 0), s.get("na", 0)))
        lines.append("".join(row))
    lines.append("")


def _sec_stability(lines, backends):
    lines.append("## Determinism")
    lines.append("")
    lines.append("Fixed subset of 20 samples (10 per direction) translated twice;")
    lines.append("output_run_1 vs output_run_2. Sampling settings are NOT changed")
    lines.append("if there is a mismatch — the fact is recorded.")
    lines.append("")
    lines.append("| Backend | Checked | All match | Mismatches |")
    lines.append("|---|---|---|---|")
    for name in ("marian", "llama_cpp"):
        res = backends.get(name)
        label = "Marian" if name == "marian" else "Hy-MT2 GGUF"
        if not isinstance(res, dict) or res.get("available") is False:
            lines.append("| %s | — | — | — |" % label)
            continue
        det = res.get("determinism", {})
        mism = det.get("mismatches", [])
        lines.append("| %s | %s | %s | %s |" % (
            label, det.get("checked", 0),
            "yes" if det.get("all_match") else "NO",
            ", ".join(x["id"] for x in mism) if mism else "0"))
    lines.append("")
    lines.append("## Streaming (architecture regression check)")
    lines.append("")
    lines.append("Fixed subset (10 EN→RU + 10 RU→EN): translate() == final")
    lines.append("translate_stream() output, start/done events correct, final")
    lines.append("output non-empty. Not a latency metric.")
    lines.append("")
    lines.append("| Backend | Checked | OK | Failed | Failures |")
    lines.append("|---|---|---|---|---|")
    for name in ("marian", "llama_cpp"):
        res = backends.get(name)
        label = "Marian" if name == "marian" else "Hy-MT2 GGUF"
        if not isinstance(res, dict) or res.get("available") is False:
            lines.append("| %s | — | — | — | — |" % label)
            continue
        st = res.get("streaming", {})
        fails = st.get("failures", [])
        lines.append("| %s | %s | %s | %s | %s |" % (
            label, st.get("checked", 0), st.get("ok", 0), st.get("failed", 0),
            ", ".join("%s (%s)" % (x["id"], x.get("reason") or "?") for x in fails)
            if fails else "0"))
    lines.append("")



def _sec_examples(lines, examples):
    lines.append("## Examples")
    lines.append("")
    lines.append("Fixed set of IDs (one per category, chosen BEFORE the run):")
    lines.append("")
    lines.append("| ID | Source | Marian | Hy-MT2 GGUF | Reference |")
    lines.append("|---|---|---|---|---|")
    for row in examples:
        data = {name: row.get(name) for name in ("marian", "llama_cpp")}
        src = next((d.get("source") for d in data.values() if d), None)
        ref = next((d.get("reference") for d in data.values()
                    if d and d.get("reference")), None)
        cells = [row["id"], _md_cell(src)]
        for name in ("marian", "llama_cpp"):
            d = data[name]
            if not d:
                cells.append("—")
            else:
                mark = "" if d.get("success") else " [FAILED]"
                cells.append(_md_cell(d.get("output")) + mark)
        cells.append(_md_cell(ref))
        lines.append("| %s | %s | %s | %s | %s |" % tuple(cells))
    lines.append("")


def _sec_limits(lines):
    lines.append("## Limitations")
    lines.append("")
    lines.append("- Each backend runs in a separate process; peak RSS (ru_maxrss)")
    lines.append("  covers the whole process (interpreter + imports + model) — it is")
    lines.append("  not attributed to the model alone.")
    lines.append("- Marian first run may download models into the existing model")
    lines.append("  cache (existing project mechanism); load time is reported")
    lines.append("  separately from translation latency.")
    lines.append("- Hy-MT2 runs at POC settings (temperature=0.0, n_ctx=4096,")
    lines.append("  CPU only) — current production parameters, not tuned for the")
    lines.append("  benchmark.")
    lines.append("- numbers_preserved compares digit runs; numbers translated into")
    lines.append("  words count as a structural fail (a fact, not a quality score).")
    lines.append("- The current pipeline joins sentence units with spaces, so")
    lines.append("  lines_preserved may legitimately fail for line-based inputs —")
    lines.append("  a property of the pipeline, reported as data.")
    lines.append("- This report contains data only; it does not rank or pick a")
    lines.append("  winner — interpretation is left to the developer.")
    lines.append("")


def write_markdown(merged, path):
    """Читаемый summary (только данные, без winner/ranking)."""
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    backends = merged.get("backends", {})
    lines = []
    lines.append("# OfflineTranslator benchmark: Marian vs Hy-MT2 GGUF (Stage 7)")
    lines.append("")
    lines.append("> Отчёт содержит только измеренные данные. Вывода «какой")
    lines.append("> backend лучше», рейтинга и overall score в нём нет —")
    lines.append("> интерпретация остаётся за разработчиком.")
    lines.append("")
    _sec_environment(lines, merged.get("env", {}))
    _sec_models(lines, backends)
    _sec_dataset(lines, merged.get("dataset", {}))
    _sec_performance(lines, backends)
    _sec_memory(lines, backends)
    _sec_quality(lines, backends)
    _sec_structural(lines, backends)
    _sec_stability(lines, backends)
    _sec_examples(lines, merged.get("examples", []))
    _sec_limits(lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

