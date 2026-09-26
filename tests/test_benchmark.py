# -*- coding: utf-8 -*-
"""Этап 7: тесты benchmark-framework (БЕЗ реальных моделей).

Запуск (реальные модели и GGUF не нужны):

    python tests/test_benchmark.py

Покрытие:
- dataset: загрузка, schema, направления, категории, дубликаты id,
  ошибки валидации (невалидный direction/category, пустой source,
  битая JSON-строка);
- aggregate: avg/median/p95/min/max (известные значения, пустой
  список, малый n);
- quality: chrF (совпадение/расхождение/частичное), exact match
  (нормализация, case-sensitivity), compute_quality (только с
  reference, exact — только по exact-флагу);
- structural checks (URL/email/числа/проценты/code-токены/скрипт/
  копия source/строки, n/a-семантика);
- determinism/streaming: обработка результатов через duck-typed
  fake-переводчик (без torch, без моделей);
- сериализация: JSON round-trip, CSV (строки/заголовок), Markdown
  (секции, unavailable backend);
- missing GGUF: resolve/availability без реального файла.
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from benchmarks import dataset as bdataset
from benchmarks import metrics as m
from benchmarks import report
from benchmarks import runner

PASS = []


def check(name, cond, extra=""):
    if not cond:
        raise AssertionError("FAIL: %s %s" % (name, str(extra)[:300]))
    PASS.append(name)


TMP = tempfile.mkdtemp(prefix="oflt_bench_test_")

# --------------------------------------------------------------------- #
#  Dataset: реальный файл benchmarks/data/dataset.jsonl                 #
# --------------------------------------------------------------------- #
EXAMPLES = bdataset.load_dataset()
check("dataset_loads", len(EXAMPLES) >= 200, len(EXAMPLES))
EN = [e for e in EXAMPLES if e["direction"] == "en-ru"]
RU = [e for e in EXAMPLES if e["direction"] == "ru-en"]
check("dataset_100_en", len(EN) == 100, len(EN))
check("dataset_100_ru", len(RU) == 100, len(RU))
check("dataset_categories_all",
      set(bdataset.CATEGORIES) <= set(e["category"] for e in EXAMPLES),
      set(e["category"] for e in EXAMPLES))
check("dataset_ids_unique", len({e["id"] for e in EXAMPLES}) == len(EXAMPLES))
summ = bdataset.summary(EXAMPLES)
check("dataset_summary_counts",
      summ["total"] == len(EXAMPLES) and summ["per_direction"].get("en-ru") == 100
      and summ["per_direction"].get("ru-en") == 100, summ)
check("dataset_references_present", summ["with_reference"] >= 10, summ)
check("dataset_exact_flagged", 0 < summ["exact_reference"] <= summ["with_reference"])
check("dataset_example_ids_exist",
      set(report.EXAMPLE_IDS) <= {e["id"] for e in EXAMPLES},
      sorted(set(report.EXAMPLE_IDS) - {e["id"] for e in EXAMPLES})[:5])

# --------------------------------------------------------------------- #
#  Dataset: валидация (синтетические ошибки, временные файлы)           #
# --------------------------------------------------------------------- #
_JSONL_COUNTER = [0]


def write_jsonl(lines):
    _JSONL_COUNTER[0] += 1
    path = os.path.join(TMP, "dataset_%d.jsonl" % _JSONL_COUNTER[0])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path


def good_line(**over):
    base = {"id": "en001", "direction": "en-ru", "source": "Hello.",
            "category": "basic"}
    base.update(over)
    return json.dumps(base, ensure_ascii=False)


def expect_error(name, line_over=None, raw_line=None, err_part=None):
    line = raw_line if raw_line is not None else good_line(**(line_over or {}))
    path = write_jsonl([line])
    try:
        bdataset.load_dataset(path)
        check(name, False, "ожидался ValueError")
    except ValueError as e:
        ok = (err_part in str(e)) if err_part else True
        check(name, ok, str(e))


expect_error("ds_invalid_direction",
             {"id": "en002", "direction": "en-fr"}, err_part="direction")
expect_error("ds_invalid_category",
             {"id": "en002", "category": "quantum"}, err_part="category")
p_dup = write_jsonl([good_line(), good_line()])
try:
    bdataset.load_dataset(p_dup)
    check("ds_duplicate_id", False, "ожидался ValueError")
except ValueError as e:
    check("ds_duplicate_id", "дубликат" in str(e), str(e))
expect_error("ds_empty_source", {"source": "   "}, err_part="source")
expect_error("ds_missing_field", None,
             raw_line=json.dumps({"id": "en001", "direction": "en-ru",
                                  "category": "basic"}),
             err_part="source")
expect_error("ds_bad_json", None, raw_line="{not json", err_part="JSON")
expect_error("ds_bad_exact_flag", {"id": "en002", "exact": "yes"},
             err_part="exact")
p_empty = write_jsonl([])
try:
    bdataset.load_dataset(p_empty)
    check("ds_empty_dataset", False, "ожидался ValueError")
except ValueError as e:
    check("ds_empty_dataset", "пустой" in str(e), str(e))


# --------------------------------------------------------------------- #
#  Aggregate latency-метрики                                             #
# --------------------------------------------------------------------- #
agg = m.aggregate_latencies([10, 20, 30, 40, 50])
check("agg_avg_median_min_max",
      agg["avg_ms"] == 30 and agg["median_ms"] == 30
      and agg["min_ms"] == 10 and agg["max_ms"] == 50 and agg["n"] == 5, agg)
agg20 = m.aggregate_latencies(list(range(1, 21)))
check("agg_p95_n20", agg20["p95_ms"] == 19, agg20["p95_ms"])
agg100 = m.aggregate_latencies(list(range(1, 101)))
check("agg_p95_n100", agg100["p95_ms"] == 95, agg100["p95_ms"])
agg2 = m.aggregate_latencies([5, 10])
check("agg_p95_n2", agg2["p95_ms"] == 10 and agg2["median_ms"] == 7.5, agg2)
agg_empty = m.aggregate_latencies([])
check("agg_empty_none",
      agg_empty["n"] == 0 and agg_empty["p95_ms"] is None
      and agg_empty["avg_ms"] is None, agg_empty)
check("agg_p95_nearest_rank_empty", m.p95_nearest_rank([]) is None)
check("agg_median_empty", m.median([]) is None)

per_dir = m.per_direction_aggregates([
    {"id": "a", "direction": "en-ru", "success": True, "latency_ms": 10},
    {"id": "b", "direction": "en-ru", "success": False, "latency_ms": 20},
    {"id": "c", "direction": "ru-en", "success": True, "latency_ms": 30},
])
check("agg_per_direction",
      per_dir["en-ru"]["n"] == 2 and per_dir["en-ru"]["success"] == 1
      and per_dir["en-ru"]["failed"] == 1 and per_dir["ru-en"]["n"] == 1,
      per_dir)

# --------------------------------------------------------------------- #
#  Quality: exact match и chrF                                          #
# --------------------------------------------------------------------- #
check("exact_ws_normalized", m.exact_match("a  b", "a b") is True)
check("exact_case_sensitive", m.exact_match("Да.", "да.") is False)
check("exact_different", m.exact_match("привет", "мир") is False)

f_same, p_same, r_same = m.chr_f("привет, мир", "привет, мир")
check("chrf_identical", f_same == 1.0 and p_same == 1.0 and r_same == 1.0,
      (f_same, p_same, r_same))
f_dis, _, _ = m.chr_f("hello", "мир")
check("chrf_disjoint_zero", f_dis == 0.0, f_dis)
f_part, p_part, r_part = m.chr_f("привет, как дела", "Привет, мир")
check("chrf_partial_in_range", 0.0 < f_part < 1.0 and 0.0 <= p_part <= 1.0
      and 0.0 <= r_part <= 1.0, (f_part, p_part, r_part))



# --------------------------------------------------------------------- #
#  Structural checks                                                     #
# --------------------------------------------------------------------- #
st = m.structural_checks("Open https://example.com/docs now",
                         "Открой https://example.com/docs сейчас", "en-ru")
check("st_url_preserved", st["urls_preserved"] is True, st)
st = m.structural_checks("Open https://example.com/docs now",
                         "Открой документацию сейчас", "en-ru")
check("st_url_lost", st["urls_preserved"] is False, st)
check("st_target_script_cyrillic",
      m.structural_checks("Hello", "Привет", "en-ru")["target_script"] is True)
check("st_target_script_missing",
      m.structural_checks("Hello", "Hello again", "en-ru")["target_script"] is False)
check("st_target_script_latin",
      m.structural_checks("Привет", "Hello", "ru-en")["target_script"] is True)
st = m.structural_checks("Write to j.smith@example.com",
                         "Напиши на j.smith@example.com", "en-ru")
check("st_email_preserved", st["emails_preserved"] is True, st)
st = m.structural_checks("The price is 1,000,000 dollars",
                         "Цена — 1 000 000 долларов", "en-ru")
check("st_numbers_reformatted_ok", st["numbers_preserved"] is True, st)
st = m.structural_checks("The project is 42 percent complete",
                         "Проект завершён на сорока двух процентах", "en-ru")
check("st_numbers_to_words_fail", st["numbers_preserved"] is False, st)
st = m.structural_checks("42% done", "Готово 42%", "en-ru")
check("st_percent_preserved", st["percent_preserved"] is True, st)
st = m.structural_checks("Use com.example.service.AuthFilter here",
                         "Используй com.example.service.AuthFilter здесь", "en-ru")
check("st_code_token_preserved", st["code_tokens_preserved"] is True, st)
st = m.structural_checks("Run mvn -DskipTests package",
                         "Выполни mvn без тестов", "en-ru")
check("st_code_token_lost", st["code_tokens_preserved"] is False, st)
check("st_na_without_url",
      m.structural_checks("Simple sentence", "Простое предложение", "en-ru")
      ["urls_preserved"] is None)
check("st_na_without_number",
      m.structural_checks("Simple sentence", "Простое предложение", "en-ru")
      ["numbers_preserved"] is None)
check("st_not_source_copy",
      m.structural_checks("Hello, world!", "Привет, мир!", "en-ru")
      ["not_source_copy"] is True)
check("st_source_copy_detected",
      m.structural_checks("Hello, world!", "Hello, world!", "en-ru")
      ["not_source_copy"] is False)
check("st_empty_output",
      m.structural_checks("Hello", "", "en-ru")["output_non_empty"] is False)
multi_src = "First line.\nSecond line.\nThird line."
st = m.structural_checks(multi_src, "Первая.\nВторая.\nТретья.", "en-ru")
check("st_lines_preserved", st["lines_preserved"] is True, st)
st = m.structural_checks(multi_src, "Первая. Вторая. Третья.", "en-ru")
check("st_lines_joined_fail", st["lines_preserved"] is False, st)
check("st_lines_na_single",
      m.structural_checks("One line.", "Одна строка.", "en-ru")
      ["lines_preserved"] is None)
check("st_extract_urls_unix",
      m.extract_urls("config at /etc/app/settings.yaml")
      == ["/etc/app/settings.yaml"])
check("st_extract_urls_win",
      m.extract_urls("copy from C:\\tools\\backup.bat")
      == ["C:\\tools\\backup.bat"])
st_sum = m.structural_summary([
    {"structural": {"urls_preserved": True, "target_script": False}},
    {"structural": {"urls_preserved": None, "target_script": True}},
])
check("st_summary_counts",
      st_sum["urls_preserved"] == {"pass": 1, "fail": 0, "na": 1}
      and st_sum["target_script"] == {"pass": 1, "fail": 1, "na": 0}, st_sum)

# --------------------------------------------------------------------- #
#  Quality computation (только с reference)                             #
# --------------------------------------------------------------------- #
def fake_sample(id_, direction, output, reference=None, exact=False,
                success=True):
    return {
        "id": id_, "direction": direction, "category": "basic",
        "source": "x", "reference": reference, "exact": exact,
        "success": success, "output": output,
        "error": None if success else output, "latency_ms": 1.0,
    }


q = m.compute_quality([
    fake_sample("e1", "en-ru", "Привет, мир!", "Привет, мир!", exact=True),
    fake_sample("e2", "en-ru", "Доброе утро", "Доброе утро, все"),
    fake_sample("e3", "en-ru", "Готово", None),
    fake_sample("e4", "en-ru", "Готово", "Готово", exact=True, success=False),
])
check("quality_only_reference",
      q["en-ru"]["n_ref"] == 2 and q["en-ru"]["exact_total"] == 1, q)
check("quality_exact_counted",
      q["en-ru"]["exact_matched"] == 1 and q["en-ru"]["chrF_mean"] is not None, q)
check("quality_no_reference_empty", m.compute_quality(
    [fake_sample("e1", "en-ru", "x")]) == {})



# --------------------------------------------------------------------- #
#  Determinism / streaming: обработка результатов (fake-переводчик)     #
# --------------------------------------------------------------------- #
class FakeTranslator:
    """Duck-typed OfflineTranslator для runner.run_determinism/
    run_streaming: без torch и без моделей."""

    TABLE = {"Hello.": "Привет.", "Привет.": "Hello.",
             "Hello. Hello.": "Привет. Привет."}

    def __init__(self, flip=False, stream_error=None):
        self._flip = flip          # 2-й вызов на тот же текст -> + "!"
        self._counts = {}
        self._stream_error = stream_error

    def translate(self, text, direction="en-ru"):
        n = self._counts.get(text, 0)
        self._counts[text] = n + 1
        base = self.TABLE.get(text, text)
        if self._flip and n >= 1:
            return base + "!"
        return base

    def translate_stream(self, text, direction="en-ru", on_sentence=None):
        if self._stream_error is not None:
            raise self._stream_error
        from sentence_pipeline import split_units
        units = split_units(text)
        translations = []
        for i, u in enumerate(units):
            if on_sentence is not None:
                on_sentence("start", i, len(units), None)
            translations.append(self.translate(u.text, direction))
            if on_sentence is not None:
                on_sentence("done", i + 1, len(units), None)
        return " ".join(translations)


DET_EXAMPLES = [{"id": "en001", "direction": "en-ru", "category": "basic",
                 "source": "Hello."}]
run1 = [fake_sample("en001", "en-ru", "Привет.")
        ]
det_ok = runner.run_determinism(FakeTranslator(), DET_EXAMPLES, run1)
check("det_all_match", det_ok["all_match"] is True
      and det_ok["mismatches"] == [] and det_ok["checked"] == 1, det_ok)
run1_flip = [fake_sample("en001", "en-ru", "Привет.")]
flip_ft = FakeTranslator(flip=True)
check("det_first_call_matches", flip_ft.translate("Hello.", "en-ru") == "Привет.")
det_bad = runner.run_determinism(flip_ft, DET_EXAMPLES, run1_flip)
check("det_mismatch_recorded", det_bad["all_match"] is False
      and len(det_bad["mismatches"]) == 1
      and det_bad["mismatches"][0]["id"] == "en001"
      and det_bad["mismatches"][0]["run2"] == "Привет.!", det_bad)

STREAM_EXAMPLES = [{"id": "en011", "direction": "en-ru", "category": "basic",
                    "source": "Hello."},
                   {"id": "en012", "direction": "en-ru", "category": "basic",
                    "source": "Hello. Hello."}]
st_ok = runner.run_streaming(FakeTranslator(), STREAM_EXAMPLES)
check("stream_all_ok", st_ok["all_ok"] is True and st_ok["checked"] == 2
      and st_ok["failed"] == 0, st_ok)
st_err = runner.run_streaming(FakeTranslator(stream_error=RuntimeError("boom")),
                              STREAM_EXAMPLES)
check("stream_error_recorded", st_err["all_ok"] is False
      and st_err["failed"] == 2
      and all("boom" in f["reason"] for f in st_err["failures"]), st_err)



# --------------------------------------------------------------------- #
#  Сериализация результатов: JSON / CSV / Markdown                       #
# --------------------------------------------------------------------- #
def fake_sample_full(id_, direction, success=True):
    src = "Hello, world!" if direction == "en-ru" else "Привет, мир!"
    out = "Привет, мир!" if direction == "en-ru" else "Hello, world!"
    if not success:
        out = "Ошибка перевода: boom"
    return {
        "id": id_, "direction": direction, "category": "basic", "source": src,
        "reference": out if success else None, "exact": False,
        "success": success, "output": out,
        "error": None if success else out, "latency_ms": 12.5,
        "source_length_chars": len(src), "output_length_chars": len(out),
        "source_token_count": 3, "units": 1, "chunks": 1,
        "structural": m.structural_checks(src, out, direction),
    }


def fake_backend(name, samples):
    return {
        "backend": name,
        "env": {"os": "Linux", "os_release": "Test", "python": "3.11",
                "cpu": "TestCPU", "cpu_count": 8, "ram_total_mb": 16384,
                "versions": {"llama_cpp": None, "transformers": "5.17.0",
                              "torch": "2.14.0"}},
        "model": {"backend": name, "runtime": "test", "device": "cpu",
                  "max_source_tokens": 480},
        "model_load_time_ms": 1234.0, "warmup_ms": 56.0,
        "memory": {"before_rss_kb": 100000, "after_rss_kb": 300000,
                   "peak_rss_kb": 400000, "notes": ["test"]},
        "samples": samples,
        "aggregates": m.per_direction_aggregates(samples),
        "quality": m.compute_quality(samples),
        "structural": m.structural_summary(samples),
        "determinism": {"subset": ["en001"], "checked": 1, "mismatches": [],
                        "all_match": True},
        "streaming": {"subset": ["en011"], "checked": 1, "ok": 1, "failed": 0,
                      "all_ok": True, "failures": []},
    }


marian_fb = fake_backend("marian", [fake_sample_full("en001", "en-ru"),
                                    fake_sample_full("ru001", "ru-en")])
llama_fb = fake_backend("llama_cpp", [fake_sample_full("en001", "en-ru",
                                                       success=False),
                                      fake_sample_full("ru001", "ru-en")])
ds_sum = bdataset.summary(EXAMPLES)
merged = report.build_merged(
    {"os": "Linux", "os_release": "Test", "python": "3.11", "cpu": "CPU",
     "cpu_count": 8, "ram_total_mb": 16384,
     "versions": {"llama_cpp": "0.3.35", "transformers": "5.17.0",
                  "torch": "2.14.0"}},
    "benchmarks/data/dataset.jsonl", ds_sum,
    {"marian": marian_fb, "llama_cpp": llama_fb})
check("merged_examples_rows", len(merged["examples"]) == len(report.EXAMPLE_IDS))
check("merged_example_data",
      merged["examples"][0]["marian"]["output"] == "Привет, мир!"
      and merged["examples"][0]["llama_cpp"]["success"] is False)

json_path = os.path.join(TMP, "merged.json")
report.write_json(merged, json_path)
with open(json_path, "r", encoding="utf-8") as f:
    reloaded = json.load(f)
check("json_roundtrip", reloaded == merged)

csv_path = os.path.join(TMP, "merged.csv")
n_rows = report.write_csv(merged, csv_path)
csv_lines = [l for l in open(csv_path, encoding="utf-8").read().splitlines()
             if l.strip()]
check("csv_rows_count", n_rows == 4 and len(csv_lines) == 5,
      (n_rows, len(csv_lines)))
check("csv_header", csv_lines[0].split(",")[:4]
      == ["backend", "id", "direction", "category"], csv_lines[0])
check("csv_backend_column", all(l.startswith("marian,") or l.startswith("llama_cpp,")
                                for l in csv_lines[1:]))



md_path = os.path.join(TMP, "merged.md")
report.write_markdown(merged, md_path)
md = open(md_path, encoding="utf-8").read()
for section in ("## Environment", "## Models", "## Dataset", "## Performance",
                "## Memory", "## Quality metrics", "## Structural checks",
                "## Determinism", "## Streaming", "## Examples",
                "## Limitations"):
    check("md_section_" + section.replace("## ", "").replace(" ", "_"),
          section in md, section)

merged_unavail = report.build_merged(
    merged["env"], "x.jsonl", ds_sum,
    {"marian": marian_fb,
     "llama_cpp": {"available": False, "reason": "не задана GGUF"}})
md2_path = os.path.join(TMP, "merged_unavail.md")
report.write_markdown(merged_unavail, md2_path)
md2 = open(md2_path, encoding="utf-8").read()
check("md_unavailable_backend", "unavailable" in md2
      and "не задана GGUF" in md2)
csv2_rows = report.write_csv(merged_unavail, os.path.join(TMP, "u.csv"))
check("csv_unavailable_skipped", csv2_rows == 2, csv2_rows)

# --------------------------------------------------------------------- #
#  Missing GGUF handling (без реальной модели)                           #
# --------------------------------------------------------------------- #
_saved_env = os.environ.pop("OFFLINE_TRANSLATOR_GGUF", None)
try:
    check("gguf_resolve_missing", runner.resolve_gguf_path() is None)
    ok, reason = runner.gguf_availability()
    check("gguf_availability_missing", ok is False
          and "OFFLINE_TRANSLATOR_GGUF" in reason, reason)
    os.environ["OFFLINE_TRANSLATOR_GGUF"] = "/nonexistent/model.gguf"
    ok, reason = runner.gguf_availability()
    check("gguf_availability_bad_path", ok is False
          and "не найден" in reason, reason)
    try:
        runner.create_translator("llama_cpp", gguf_path="/nonexistent/x.gguf")
        check("gguf_facade_missing_file", False, "ожидался FileNotFoundError")
    except FileNotFoundError:
        check("gguf_facade_missing_file", True)
    try:
        runner.create_translator("llama_cpp", gguf_path=None)
        check("gguf_facade_no_path", False, "ожидался RuntimeError")
    except RuntimeError:
        check("gguf_facade_no_path", True)
finally:
    os.environ.pop("OFFLINE_TRANSLATOR_GGUF", None)
    if _saved_env is not None:
        os.environ["OFFLINE_TRANSLATOR_GGUF"] = _saved_env

# --------------------------------------------------------------------- #
print("")
print("✓ test_benchmark.py: ПРОШЛО ПРОВЕРОК: %d" % len(PASS))
for name in PASS:
    print("  -", name)

